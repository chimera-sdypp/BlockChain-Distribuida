"""BC-58: pedidos de alta y panel del organizador, contra Redis y RabbitMQ de verdad (INTEGRACION=1)."""

import json
import os
import threading
import time
import uuid

import pytest
import redis as redislib
from fastapi.testclient import TestClient

from backend import app as backend
from comun.altas import Altas
from comun import Rabbit, config, contrato, web
from comun.tests.claves import CUENTA, PRIVADA, carga, cadena_actual, sembrar_genesis

pytestmark = pytest.mark.skipif(os.environ.get("INTEGRACION") != "1",
                                reason="necesita RabbitMQ y Redis (INTEGRACION=1)")

BASE_PRUEBAS = 15


@pytest.fixture
def entorno():
    """(armar, cola, redis): `armar(**opciones)` da un TestClient del backend con una cola temporal."""
    c = config.cargar("prueba")
    cola = f"prueba-{uuid.uuid4().hex[:8]}"
    publicador = Rabbit(c, [cola])
    publicador.conectar()
    redis = redislib.Redis(host=c.redis_host, port=c.redis_puerto, password=c.redis_clave or None,
                           db=BASE_PRUEBAS, decode_responses=True)
    redis.flushdb()
    sembrar_genesis(redis)

    def armar(**opciones):
        app = web.crear_app("backend", {})
        backend.agregar_rutas(app, redis, publicador, cola=cola,
                              **{"organizador": CUENTA["organizador"], **opciones})
        return TestClient(app)

    yield armar, cola, redis
    redis.flushdb()
    publicador._canal.queue_delete(cola)
    publicador.detener()


def recibir(cola, cantidad, segundos=10):
    consumidor, recibidos = Rabbit(config.cargar("prueba"), [cola]), []
    threading.Thread(target=consumidor.consumir, args=(cola, recibidos.append), daemon=True).start()
    limite = time.monotonic() + segundos
    while len(recibidos) < cantidad and time.monotonic() < limite:
        time.sleep(0.1)
    consumidor.detener()
    return recibidos


def panel(cliente, metodo, ruta, datos=None, firmante="organizador", tiempo=None):
    """Un pedido al panel firmado como lo hace la página: X-Organizador, X-Tiempo y X-Firma."""
    cuerpo = json.dumps(datos).encode() if datos is not None else b""
    encabezados = contrato.firmar_pedido(PRIVADA[firmante], metodo, ruta, cuerpo, tiempo=tiempo,
                                         quien=contrato.ORGANIZADOR, cadena=cadena_actual())
    return cliente.request(metodo, ruta, content=cuerpo, headers={"content-type": "application/json", **encabezados})


def pedir(cliente, nombre, tipo="usuario"):
    return cliente.post("/api/altas", json=contrato.pedido_alta(PRIVADA[nombre], tipo, nombre, cadena=cadena_actual()))


# ── Pedido de alta (público) ──

def test_un_pedido_de_alta_queda_pendiente(entorno):
    armar, _, redis = entorno
    cliente = armar()

    respuesta = pedir(cliente, "zoe")

    assert respuesta.status_code == 202
    assert respuesta.json() == {"cuenta": CUENTA["zoe"], "estado": "pendiente"}
    pedido = json.loads(redis.hget("altas:pendientes", CUENTA["zoe"]))
    assert pedido["tipo"] == "usuario" and pedido["nombre"] == "zoe" and pedido["pedida"] > 0
    assert cliente.get(f"/api/cuentas/{CUENTA['zoe']}").json()["alta_pedida"] is True
    assert pedir(cliente, "zoe").status_code == 202  # pedirlo de nuevo no lo duplica
    assert redis.hlen("altas:pendientes") == 1


def test_un_pedido_con_una_firma_que_no_corresponde_a_la_clave_se_rechaza(entorno):
    armar, _, redis = entorno
    pedido = {**contrato.pedido_alta(PRIVADA["zoe"], "usuario", "zoe", cadena=cadena_actual()), "clave_publica": CUENTA["yago"]}

    respuesta = armar().post("/api/altas", json=pedido)

    assert respuesta.status_code == 400 and "firma" in respuesta.json()["errores"][0]
    assert not redis.exists("altas:pendientes")


def test_una_cuenta_aprobada_no_vuelve_a_pedir(entorno):
    armar, _, redis = entorno
    redis.hset("cuentas", CUENTA["zoe"], json.dumps({"tipo": "usuario", "nombre": "zoe", "alta": 1}))

    assert pedir(armar(), "zoe").status_code == 409
    assert not redis.exists("altas:pendientes")


def test_con_demasiados_pedidos_pendientes_da_429(entorno):
    armar, _, _ = entorno
    cliente = armar(altas_maximas=2)
    assert [pedir(cliente, n).status_code for n in ("zoe", "yago", "ximena")] == [202, 202, 429]


# ── El panel sólo atiende al organizador (v3: sólo lo que no va a la cadena) ──

RUTAS_PANEL = [("GET", "/api/organizador/altas"), ("GET", "/api/organizador/cuentas"),
               ("POST", "/api/organizador/rechazar"), ("GET", "/api/organizador/suspendidos"),
               ("POST", "/api/organizador/reactivar")]


@pytest.mark.parametrize("metodo, ruta", RUTAS_PANEL)
def test_una_accion_del_panel_sin_firma_da_403(entorno, metodo, ruta):
    armar, _, _ = entorno
    respuesta = armar().request(metodo, ruta, json={"cuenta": CUENTA["zoe"]} if metodo == "POST" else None)
    assert respuesta.status_code == 403


@pytest.mark.parametrize("metodo, ruta", RUTAS_PANEL)
def test_una_accion_firmada_por_otra_clave_da_403(entorno, metodo, ruta):
    armar, _, redis = entorno
    pedir(armar(), "zoe")
    respuesta = panel(armar(), metodo, ruta, {"cuenta": CUENTA["zoe"]} if metodo == "POST" else None, firmante="alice")
    assert respuesta.status_code == 403 and "organizador" in respuesta.json()["error"]
    assert redis.hexists("altas:pendientes", CUENTA["zoe"])


def test_una_accion_vieja_o_alterada_da_403(entorno):
    armar, _, redis = entorno
    cliente = armar()
    pedir(cliente, "zoe")
    vieja = panel(cliente, "POST", "/api/organizador/rechazar", {"cuenta": CUENTA["zoe"]}, tiempo=int(time.time()) - 60)
    assert vieja.status_code == 403 and "X-Tiempo" in vieja.json()["error"]
    # La firma es sobre el cuerpo: cambiar la cuenta después de firmar no sirve.
    encabezados = contrato.firmar_pedido(PRIVADA["organizador"], "POST", "/api/organizador/rechazar",
                                         json.dumps({"cuenta": CUENTA["yago"]}).encode(), quien=contrato.ORGANIZADOR, cadena=cadena_actual())
    alterada = cliente.post("/api/organizador/rechazar", content=json.dumps({"cuenta": CUENTA["zoe"]}).encode(),
                            headers=encabezados)
    assert alterada.status_code == 403
    assert redis.hexists("altas:pendientes", CUENTA["zoe"])


def test_sin_organizador_clave_el_panel_esta_apagado(entorno):
    armar, _, _ = entorno
    respuesta = panel(armar(organizador=""), "GET", "/api/organizador/altas")
    assert respuesta.status_code == 403 and "ORGANIZADOR_CLAVE" in respuesta.json()["error"]


def test_el_panel_ya_no_aprueba_ni_da_de_baja(entorno):
    """v3: eso escribía `cuentas` sin firma. Ahora son actas que entran en la cadena."""
    armar, _, _ = entorno
    cliente = armar()
    for ruta in ("/api/organizador/aprobar", "/api/organizador/baja"):
        assert panel(cliente, "POST", ruta, {"cuenta": CUENTA["zoe"]}).status_code in (404, 405)


# ── Las cuentas son públicas (están en la cadena); los pendientes y los suspendidos, firmados (BC-88) ──

def test_las_cuentas_son_publicas_y_los_pendientes_y_suspendidos_piden_la_firma(entorno):
    armar, _, redis = entorno
    cliente = armar()
    pedir(cliente, "zoe")
    redis.hset("cuentas", CUENTA["yago"], json.dumps({"tipo": "usuario", "nombre": "yago", "alta": 1}))
    redis.hset("saldos", CUENTA["yago"], 7)
    assert cliente.get("/api/cuentas").json()["cuentas"] == [{"cuenta": CUENTA["yago"], "tipo": "usuario",
                                                              "nombre": "yago", "alta": 1, "saldo": 7}]
    for publica in ("/api/altas/pendientes", "/api/suspendidos"):
        assert cliente.get(publica).status_code == 404
    altas = panel(cliente, "GET", "/api/organizador/altas")
    assert [p["cuenta"] for p in altas.json()["pendientes"]] == [CUENTA["zoe"]]
    assert panel(cliente, "GET", "/api/organizador/suspendidos").json() == {"suspendidos": []}
    for ruta in ("/api/organizador/altas", "/api/organizador/suspendidos"):
        assert cliente.get(ruta).status_code == 403
        assert panel(cliente, "GET", ruta, firmante="alice").status_code == 403


def test_un_pedido_con_el_alta_en_camino_no_se_lista_para_no_aprobarlo_dos_veces(entorno):
    """BC-93: el pedido se borra recién cuando el alta entra en un bloque; mientras está en camino no se muestra."""
    armar, _, redis = entorno
    cliente = armar()
    pedir(cliente, "zoe")
    pedir(cliente, "yago")
    redis.hset("altas:en_camino", CUENTA["zoe"], json.dumps({"tipo": "usuario", "nombre": "zoe", "tx": "alta-1"}))
    assert [p["cuenta"] for p in panel(cliente, "GET", "/api/organizador/altas").json()["pendientes"]] == [CUENTA["yago"]]
    assert cliente.get(f"/api/cuentas/{CUENTA['zoe']}").json()["alta_en_camino"] is True
    assert cliente.get(f"/api/cuentas/{CUENTA['yago']}").json()["alta_en_camino"] is False


# ── Aprobar y dar de baja: actas firmadas por el organizador, por /api/transacciones ──

def test_un_alta_y_una_carga_del_organizador_se_encolan_como_cualquier_transaccion(entorno):
    armar, cola, redis = entorno
    cliente = armar()
    alta = contrato.alta(PRIVADA["organizador"], 1, CUENTA["zoe"], "usuario", "zoe", 1793836800, cadena=cadena_actual())

    assert cliente.post("/api/transacciones", json=alta).json() == {"id": "alta-1"}
    assert cliente.post("/api/transacciones", json=carga(2, "zoe", 500)).status_code == 202

    assert redis.hgetall("tx:alta-1")["datos"] == "usuario:zoe" and redis.hget("tx:alta-1", "estado") == "recibida"
    assert not redis.exists("cuentas")  # queda aprobada cuando entra en un bloque, no antes
    assert recibir(cola, 2) == [alta, carga(2, "zoe", 500)]
    assert cliente.post("/api/transacciones", json=alta).status_code == 409


@pytest.mark.parametrize("tx, error", [
    (contrato.alta(PRIVADA["alice"], 1, CUENTA["zoe"], "usuario", "zoe", 1793836800, cadena=cadena_actual()), "firma: no es la del organizador"),
    ({**contrato.alta(PRIVADA["organizador"], 1, CUENTA["zoe"], "usuario", "zoe", 1793836800, cadena=cadena_actual()), "datos": "usuario:yo"},
     "firma"),
    (contrato.alta(PRIVADA["organizador"], 1, CUENTA["zoe"], "organizador", "yo", 1793836800, cadena=cadena_actual()), "datos"),
    (contrato.baja(PRIVADA["organizador"], 1, CUENTA["zoe"], "a|b", 1793836800, cadena=cadena_actual()), "datos"),
])
def test_un_acta_mala_no_entra(entorno, tx, error):
    armar, cola, redis = entorno
    respuesta = armar().post("/api/transacciones", json=tx)
    assert respuesta.status_code == 400 and error in respuesta.json()["errores"][0]
    assert not redis.exists(f"tx:{tx['id']}") and recibir(cola, 1, segundos=1) == []


def test_un_alta_delegada_entra_con_la_firma_de_su_clave(entorno):
    """El backend verifica la firma y que la clave tenga permiso (BC-94); el prefijo lo controla el NCT."""
    armar, _, redis = entorno
    redis.hset(contrato.PERMISOS, CUENTA["escalador"], "minero-cpu-")
    alta = contrato.alta(PRIVADA["escalador"], 1, CUENTA["vm0"], "minero", "minero-cpu-0", 1793836800, delegada=True, cadena=cadena_actual())
    assert armar().post("/api/transacciones", json=alta).json() == {"id": alta["id"]}
    assert redis.hget(f"tx:{alta['id']}", "origen") == CUENTA["escalador"]


def test_un_alta_delegada_de_una_clave_sin_permiso_da_403_y_no_queda_en_redis(entorno):
    armar, cola, redis = entorno
    alta = contrato.alta(PRIVADA["escalador"], 1, CUENTA["vm0"], "minero", "minero-cpu-0", 1793836800, delegada=True, cadena=cadena_actual())
    respuesta = armar().post("/api/transacciones", json=alta)
    assert (respuesta.status_code, respuesta.json()) == (403, {"error": "clave sin permiso"})
    assert not redis.exists(f"tx:{alta['id']}") and recibir(cola, 1, segundos=1) == []


def test_una_carga_firmada_por_otra_clave_se_rechaza_tambien_suelta(entorno):
    armar, _, _ = entorno
    trucha = contrato.carga(PRIVADA["alice"], 1, CUENTA["zoe"], 500, 1793836800, cadena=cadena_actual())
    respuesta = armar().post("/api/transacciones", json=trucha)
    assert respuesta.status_code == 400 and "organizador" in respuesta.json()["errores"][0]


def test_rechazar_borra_el_pedido(entorno):
    armar, _, redis = entorno
    cliente = armar()
    pedir(cliente, "zoe")

    assert panel(cliente, "POST", "/api/organizador/rechazar", {"cuenta": CUENTA["zoe"]}).status_code == 200
    assert not redis.exists("altas:pendientes") and not redis.exists("cuentas")
    assert panel(cliente, "POST", "/api/organizador/rechazar", {"cuenta": CUENTA["zoe"]}).status_code == 404


# ── BC-75: suspendidos ──

def suspendido(redis, nombre="zoe"):
    redis.hset("cuentas", CUENTA[nombre], json.dumps({"tipo": "minero", "nombre": nombre, "alta": 1}))
    Altas(redis).suspender(CUENTA[nombre], "estadistica", {"tarea": "t1", "rango": 0, "probabilidad": 1e-11}, ahora=50)


def test_el_panel_lista_los_suspendidos_con_su_evidencia(entorno):
    armar, _, redis = entorno
    suspendido(redis)
    respuesta = panel(armar(), "GET", "/api/organizador/suspendidos")
    assert respuesta.status_code == 200
    assert respuesta.json() == {"suspendidos": [{"cuenta": CUENTA["zoe"], "nombre": "zoe", "motivo": "estadistica",
                                                 "evidencia": {"tarea": "t1", "rango": 0, "probabilidad": 1e-11},
                                                 "cuando": 50}]}


def test_reactivar_lo_saca_de_suspendidos_y_sigue_aprobado(entorno):
    armar, _, redis = entorno
    suspendido(redis)
    cliente = armar()
    assert panel(cliente, "POST", "/api/organizador/reactivar", {"cuenta": CUENTA["zoe"]}).status_code == 200
    assert not redis.hexists("suspendidos", CUENTA["zoe"]) and redis.hexists("cuentas", CUENTA["zoe"])
    assert panel(cliente, "POST", "/api/organizador/reactivar", {"cuenta": CUENTA["zoe"]}).status_code == 404


def test_un_usuario_o_una_cuenta_sin_aprobar_no_se_suspenden(entorno):
    _, _, redis = entorno
    redis.hset("cuentas", CUENTA["zoe"], json.dumps({"tipo": "usuario", "nombre": "zoe", "alta": 1}))
    assert Altas(redis).suspender(CUENTA["zoe"], "estadistica", {}) is False
    assert Altas(redis).suspender(CUENTA["alice"], "estadistica", {}) is False
