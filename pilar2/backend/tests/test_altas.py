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
from comun import Rabbit, config, contrato, web
from comun.tests.claves import CUENTA, PRIVADA, carga

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
                                         quien=contrato.ORGANIZADOR)
    return cliente.request(metodo, ruta, content=cuerpo, headers={"content-type": "application/json", **encabezados})


def pedir(cliente, nombre, tipo="usuario"):
    return cliente.post("/api/altas", json=contrato.pedido_alta(PRIVADA[nombre], tipo, nombre))


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
    pedido = {**contrato.pedido_alta(PRIVADA["zoe"], "usuario", "zoe"), "clave_publica": CUENTA["yago"]}

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


# ── El panel sólo atiende al organizador ──

RUTAS_PANEL = [("GET", "/api/organizador/altas"), ("GET", "/api/organizador/cuentas"),
               ("POST", "/api/organizador/aprobar"), ("POST", "/api/organizador/rechazar"),
               ("POST", "/api/organizador/baja")]


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
    vieja = panel(cliente, "POST", "/api/organizador/aprobar", {"cuenta": CUENTA["zoe"]}, tiempo=int(time.time()) - 60)
    assert vieja.status_code == 403 and "X-Tiempo" in vieja.json()["error"]
    # La firma es sobre el cuerpo: cambiar la cuenta después de firmar no sirve.
    encabezados = contrato.firmar_pedido(PRIVADA["organizador"], "POST", "/api/organizador/aprobar",
                                         json.dumps({"cuenta": CUENTA["yago"]}).encode(), quien=contrato.ORGANIZADOR)
    alterada = cliente.post("/api/organizador/aprobar", content=json.dumps({"cuenta": CUENTA["zoe"]}).encode(),
                            headers=encabezados)
    assert alterada.status_code == 403
    assert not redis.hexists("cuentas", CUENTA["zoe"])


def test_sin_organizador_clave_el_panel_esta_apagado(entorno):
    armar, _, _ = entorno
    respuesta = panel(armar(organizador=""), "GET", "/api/organizador/altas")
    assert respuesta.status_code == 403 and "ORGANIZADOR_CLAVE" in respuesta.json()["error"]


# ── Aprobar, rechazar y dar de baja ──

def test_aprobar_un_usuario_con_saldo_inicial_encola_la_carga(entorno):
    armar, cola, redis = entorno
    cliente = armar()
    pedir(cliente, "zoe")
    assert panel(cliente, "GET", "/api/organizador/altas").json()["pendientes"][0]["cuenta"] == CUENTA["zoe"]

    respuesta = panel(cliente, "POST", "/api/organizador/aprobar",
                      {"cuenta": CUENTA["zoe"], "carga": carga(1, "zoe", 500)})

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["carga"] == "carga-1" and respuesta.json()["tipo"] == "usuario"
    cuenta = json.loads(redis.hget("cuentas", CUENTA["zoe"]))
    assert cuenta["tipo"] == "usuario" and cuenta["nombre"] == "zoe" and cuenta["alta"] > 0
    assert not redis.exists("altas:pendientes")
    assert redis.hget("tx:carga-1", "estado") == "recibida"
    [mensaje] = recibir(cola, 1)
    assert mensaje == carga(1, "zoe", 500)
    [aprobada] = panel(cliente, "GET", "/api/organizador/cuentas").json()["cuentas"]
    assert aprobada["cuenta"] == CUENTA["zoe"] and aprobada["saldo"] == 0  # la carga todavía no está en un bloque


def test_aprobar_un_minero_sin_carga(entorno):
    armar, _, redis = entorno
    cliente = armar()
    pedir(cliente, "gpu-zoe", tipo="minero")

    respuesta = panel(cliente, "POST", "/api/organizador/aprobar", {"cuenta": CUENTA["gpu-zoe"], "nombre": "gpu-1"})

    assert respuesta.status_code == 200
    assert json.loads(redis.hget("cuentas", CUENTA["gpu-zoe"]))["tipo"] == "minero"
    assert json.loads(redis.hget("cuentas", CUENTA["gpu-zoe"]))["nombre"] == "gpu-1"  # el organizador lo renombró


@pytest.mark.parametrize("nombre, tx, error", [
    ("zoe", contrato.carga(PRIVADA["alice"], 1, CUENTA["zoe"], 500, 1793836800), "firma: no es la del organizador"),
    ("zoe", carga(1, "yago", 500), "destino"),
    ("gpu-zoe", carga(1, "gpu-zoe", 500), "sólo a un usuario"),
])
def test_una_carga_mala_no_aprueba_ni_encola(entorno, nombre, tx, error):
    armar, cola, redis = entorno
    cliente = armar()
    pedir(cliente, nombre, tipo="minero" if nombre.startswith("gpu") else "usuario")

    respuesta = panel(cliente, "POST", "/api/organizador/aprobar", {"cuenta": CUENTA[nombre], "carga": tx})

    assert respuesta.status_code == 400 and error in respuesta.json()["errores"][0]
    assert not redis.exists("cuentas") and redis.hexists("altas:pendientes", CUENTA[nombre])
    assert recibir(cola, 1, segundos=1) == []


def test_una_carga_firmada_por_otra_clave_se_rechaza_tambien_suelta(entorno):
    armar, _, _ = entorno
    trucha = contrato.carga(PRIVADA["alice"], 1, CUENTA["zoe"], 500, 1793836800)
    respuesta = armar().post("/api/transacciones", json=trucha)
    assert respuesta.status_code == 400 and "organizador" in respuesta.json()["errores"][0]


def test_aprobar_sin_pedido_da_404(entorno):
    armar, _, _ = entorno
    assert panel(armar(), "POST", "/api/organizador/aprobar", {"cuenta": CUENTA["zoe"]}).status_code == 404


def test_rechazar_borra_el_pedido(entorno):
    armar, _, redis = entorno
    cliente = armar()
    pedir(cliente, "zoe")

    assert panel(cliente, "POST", "/api/organizador/rechazar", {"cuenta": CUENTA["zoe"]}).status_code == 200
    assert not redis.exists("altas:pendientes") and not redis.exists("cuentas")
    assert panel(cliente, "POST", "/api/organizador/rechazar", {"cuenta": CUENTA["zoe"]}).status_code == 404


def test_dar_de_baja_saca_la_cuenta_y_deja_el_saldo(entorno):
    armar, _, redis = entorno
    cliente = armar()
    redis.hset("cuentas", CUENTA["zoe"], json.dumps({"tipo": "usuario", "nombre": "zoe", "alta": 1}))
    redis.hset("saldos", CUENTA["zoe"], 40)

    assert panel(cliente, "POST", "/api/organizador/baja", {"cuenta": CUENTA["zoe"]}).status_code == 200
    assert not redis.hexists("cuentas", CUENTA["zoe"]) and redis.hget("saldos", CUENTA["zoe"]) == "40"
    assert panel(cliente, "POST", "/api/organizador/baja", {"cuenta": CUENTA["zoe"]}).status_code == 404
    assert panel(cliente, "POST", "/api/organizador/baja", {"cuenta": CUENTA["organizador"]}).status_code == 400
