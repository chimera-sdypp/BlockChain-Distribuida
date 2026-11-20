"""El backend contra RabbitMQ y Redis de verdad. Corren con INTEGRACION=1 (en el CI, con services)."""

import json
import os
import threading
import time
import uuid

import pytest
import redis as redislib
from fastapi.testclient import TestClient

from backend import app as backend
from comun import Rabbit, config, contrato, estado, web
from comun.tests.claves import CUENTA, PRIVADA, Billetera, carga, firmada

pytestmark = pytest.mark.skipif(os.environ.get("INTEGRACION") != "1",
                                reason="necesita RabbitMQ y Redis (INTEGRACION=1)")

BASE_PRUEBAS = 15  # una base de Redis aparte: así no se pisan `saldos` ni `cadena` de un sistema corriendo


@pytest.fixture
def entorno():
    """Una cola temporal (para no robarle mensajes a un NCT que esté corriendo) y Redis real vacío."""
    c = config.cargar("prueba")
    cola = f"prueba-{uuid.uuid4().hex[:8]}"
    publicador = Rabbit(c, [cola])
    publicador.conectar()
    redis = redislib.Redis(host=c.redis_host, port=c.redis_puerto, password=c.redis_clave or None,
                           db=BASE_PRUEBAS, decode_responses=True)
    redis.flushdb()
    app = web.crear_app("backend", {})
    backend.agregar_rutas(app, redis, publicador, cola=cola, organizador=CUENTA["organizador"])
    yield TestClient(app), cola, redis
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


def test_post_deja_el_mensaje_en_la_cola_y_la_tx_recibida_en_redis(entorno):
    cliente, cola, redis = entorno
    tx = firmada("alice", "bob", 7)
    respuesta = cliente.post("/api/transacciones", json=tx)

    assert respuesta.status_code == 202
    id_ = respuesta.json()["id"]
    [mensaje] = recibir(cola, 1)
    assert mensaje == tx and id_ == tx["id"]
    assert redis.hget(f"tx:{id_}", "estado") == "recibida"


def test_dos_backends_con_la_misma_transaccion_a_la_vez_la_encolan_una_vez(entorno):
    cliente, cola, _ = entorno
    tx, codigos, barrera = firmada("alice", "bob", 7), [], threading.Barrier(8)

    def mandar():
        barrera.wait()
        codigos.append(cliente.post("/api/transacciones", json=tx).status_code)

    hilos = [threading.Thread(target=mandar) for _ in range(8)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    assert sorted(codigos) == [202] + [409] * 7
    assert len(recibir(cola, 2, segundos=2)) == 1


# ── Ticket 02: consultar una transacción y los saldos ──

def test_get_de_una_transaccion_recien_recibida(entorno):
    cliente, _, _ = entorno
    tx = firmada("alice", "bob", 7)
    id_ = cliente.post("/api/transacciones", json=tx).json()["id"]

    respuesta = cliente.get(f"/api/transacciones/{id_}")

    assert respuesta.status_code == 200
    assert respuesta.json() == {**tx, "estado": "recibida"}


def test_get_muestra_el_motivo_de_una_rechazada_y_el_bloque_de_una_confirmada(entorno):
    cliente, _, redis = entorno
    # Lo que escribe el NCT (BC-24 y BC-26).
    redis.hset("tx:r", mapping={"origen": "ana", "destino": "bob", "monto": 99, "nonce": 1, "timestamp": 1,
                                "estado": "rechazada", "motivo": "saldo insuficiente"})
    redis.hset("tx:c", mapping={"origen": "ana", "destino": "bob", "monto": 5, "nonce": 2, "timestamp": 2,
                                "estado": "confirmada", "bloque": "0000abc"})

    rechazada = cliente.get("/api/transacciones/r").json()
    confirmada = cliente.get("/api/transacciones/c").json()

    assert (rechazada["estado"], rechazada["motivo"]) == ("rechazada", "saldo insuficiente")
    assert (confirmada["estado"], confirmada["bloque"], confirmada["nonce"]) == ("confirmada", "0000abc", 2)


def test_get_de_una_transaccion_que_no_existe_da_404(entorno):
    cliente, _, _ = entorno
    assert cliente.get("/api/transacciones/nada").status_code == 404


def test_saldos(entorno):
    cliente, _, redis = entorno
    redis.hset("saldos", mapping={CUENTA["alice"]: 750, CUENTA["bob"]: 1250})

    assert cliente.get("/api/saldos").json() == {CUENTA["alice"]: 750, CUENTA["bob"]: 1250}
    assert cliente.get(f"/api/saldos/{CUENTA['bob']}").json() == {"usuario": CUENTA["bob"], "saldo": 1250}
    assert cliente.get(f"/api/saldos/{CUENTA['zoe']}").json() == {"usuario": CUENTA["zoe"], "saldo": 0}


def test_cuenta_con_su_saldo_su_nonce_y_si_esta_aprobada(entorno):
    cliente, _, redis = entorno
    alice = CUENTA["alice"]
    redis.hset("saldos", alice, 750)
    redis.hset("comprometido", alice, 50)
    redis.hset("nonces", mapping={alice: 3, "emision": 7})
    redis.hset("cuentas", alice, json.dumps({"tipo": "usuario", "nombre": "alice", "alta": 1793836800}))

    assert cliente.get(f"/api/cuentas/{alice}").json() == {
        "cuenta": alice, "saldo": 750, "comprometido": 50, "nonce": 3, "aprobada": True,
        "tipo": "usuario", "nombre": "alice", "alta_pedida": False}
    assert cliente.get(f"/api/cuentas/{CUENTA['zoe']}").json() == {
        "cuenta": CUENTA["zoe"], "saldo": 0, "comprometido": 0, "nonce": 0, "aprobada": False,
        "tipo": None, "nombre": None, "alta_pedida": False}
    assert cliente.get("/api/cuentas/emision").json()["nonce"] == 7
    assert cliente.get("/api/cuentas/alice").status_code == 400


# ── Ticket 03: recorrer la cadena ──

def guardar_bloque(redis, bloque):
    """Como lo guarda el NCT (NCT.4): bloque:<hash> con `transactions` en JSON, y el hash al final de `cadena`."""
    redis.hset(f"bloque:{bloque['block_hash']}", mapping={**bloque, "transactions": json.dumps(bloque["transactions"])})
    redis.rpush("cadena", bloque["block_hash"])


def armar_cadena(redis, largo):
    """Génesis más largo−1 bloques válidos (v3, con su raíz de estado), como los arma el NCT, y el caché de los
    saldos como lo deja el consenso. Devuelve los hashes en orden de altura."""
    genesis = contrato.bloque_genesis({CUENTA["alice"]: 1000, CUENTA["bob"]: 1000}, 1793836800)
    actual = estado.Estado()
    estado.aplicar_bloque(actual, genesis)
    guardar_bloque(redis, genesis)
    hashes, billetera = [genesis["block_hash"]], Billetera()
    for altura in range(1, largo):
        tarea = {"previous_hash": hashes[-1], "timestamp": 1793836800 + altura, "altura": altura, "prefijo": "0",
                 "transacciones": [billetera("alice", "bob", altura, timestamp=1793836800 + altura)],
                 "raiz_estado": actual.raiz()}
        transacciones = contrato.transacciones_del_bloque(tarea, CUENTA["minero"])
        texto = contrato.texto_para(tarea, CUENTA["minero"])
        nonce = next(n for n in range(10**6) if contrato.hash_bloque(texto, n).startswith("0"))
        bloque = {"previous_hash": hashes[-1], "timestamp": tarea["timestamp"], "transactions": transacciones,
                  "nonce": nonce, "block_hash": contrato.hash_bloque(texto, nonce), "prefijo": "0", "altura": altura,
                  "raiz_estado": tarea["raiz_estado"]}
        assert estado.aplicar_bloque(actual, bloque)[0] == []
        guardar_bloque(redis, bloque)
        hashes.append(bloque["block_hash"])
    redis.hset("saldos", mapping=actual.saldos)
    redis.hset("nonces:confirmados", mapping=actual.nonces)
    redis.hset("cuentas", mapping={c: json.dumps(d) for c, d in actual.cuentas.items()})
    return hashes, actual


def test_bloques_del_mas_nuevo_al_mas_viejo_con_altura(entorno):
    cliente, _, redis = entorno
    hashes, _ = armar_cadena(redis, 3)

    bloques = cliente.get("/api/bloques").json()

    assert [b["altura"] for b in bloques] == [2, 1, 0]
    assert [b["block_hash"] for b in bloques] == hashes[::-1]
    punta = bloques[0]
    assert punta["previous_hash"] == hashes[1]
    assert punta["timestamp"] == 1793836802
    coinbase, transferencia = punta["transactions"]
    assert coinbase == contrato.coinbase(2, CUENTA["minero"], 10, 1793836802)
    assert (transferencia["monto"], transferencia["nonce"]) == (2, 2)
    # El minero y la recompensa no se guardan aparte: los saca de la coinbase.
    assert (punta["minero"], punta["recompensa"]) == (CUENTA["minero"], 10)
    assert (bloques[2]["minero"], bloques[2]["recompensa"]) == ("", 0)  # el génesis


def test_bloques_paginados(entorno):
    cliente, _, redis = entorno
    hashes, _ = armar_cadena(redis, 5)

    primera = cliente.get("/api/bloques", params={"limite": 2}).json()
    segunda = cliente.get("/api/bloques", params={"limite": 2, "desde": 2}).json()
    ultima = cliente.get("/api/bloques", params={"limite": 2, "desde": 0}).json()

    assert [b["altura"] for b in primera] == [4, 3]
    assert [b["altura"] for b in segunda] == [2, 1]
    assert [b["block_hash"] for b in ultima] == [hashes[0]]


def test_bloques_de_una_cadena_vacia(entorno):
    cliente, _, _ = entorno
    assert cliente.get("/api/bloques").json() == []


@pytest.mark.parametrize("params", [{"limite": 0}, {"limite": 101}, {"desde": -1}, {"limite": "x"}])
def test_bloques_con_parametros_invalidos_da_400(entorno, params):
    cliente, _, _ = entorno
    assert cliente.get("/api/bloques", params=params).status_code == 400


def test_bloque_por_hash_con_su_altura(entorno):
    cliente, _, redis = entorno
    hashes, _ = armar_cadena(redis, 3)

    bloque = cliente.get(f"/api/bloques/{hashes[1]}").json()

    assert (bloque["altura"], bloque["block_hash"], bloque["previous_hash"]) == (1, hashes[1], hashes[0])
    assert isinstance(bloque["altura"], int)
    assert cliente.get("/api/bloques/noexiste").status_code == 404


# ── Ticket 04: lote ──

def test_lote_deja_todas_en_la_cola_en_orden_y_recibidas_en_redis(entorno):
    cliente, cola, redis = entorno
    billetera = Billetera()
    cuerpo = [billetera("alice", "bob", m) for m in range(1, 51)] + [carga(1, "carol", 100)]

    respuesta = cliente.post("/api/transacciones/lote", json=cuerpo)

    assert respuesta.status_code == 202
    ids = respuesta.json()["ids"]
    assert ids[-1] == "carga-1"
    mensajes = recibir(cola, 51)
    assert [m["id"] for m in mensajes] == ids
    assert [m["nonce"] for m in mensajes] == list(range(1, 51)) + [1]
    assert all(redis.hget(f"tx:{id_}", "estado") == "recibida" for id_ in ids)


def test_lote_con_una_ya_recibida_no_crea_ninguna(entorno):
    cliente, cola, redis = entorno
    billetera = Billetera()
    cuerpo = [billetera("alice", "bob", m) for m in range(1, 4)]
    assert cliente.post("/api/transacciones", json=cuerpo[2]).status_code == 202

    respuesta = cliente.post("/api/transacciones/lote", json=cuerpo)

    assert respuesta.status_code == 409 and set(respuesta.json()["errores"]) == {"2"}
    assert not redis.exists(f"tx:{cuerpo[0]['id']}", f"tx:{cuerpo[1]['id']}")
    assert len(recibir(cola, 2, segundos=2)) == 1


# ── Pendientes y cajas (BC-28) ──

def guardar_tx(redis, id_, estado, monto=1):
    redis.hset(f"tx:{id_}", mapping={"origen": CUENTA["alice"], "destino": CUENTA["bob"], "monto": monto,
                                     "nonce": 1, "timestamp": 1793836800, "estado": estado})


def test_cajas_con_las_pendientes_y_lo_apilado(entorno):
    cliente, _, redis = entorno
    for i in range(3):
        guardar_tx(redis, f"p{i}", "pendiente", monto=i + 1)
        redis.rpush("tx:pendientes", f"p{i}")
    redis.rpush("cajas", json.dumps(["a", "b"]), json.dumps(["c"]))

    respuesta = cliente.get("/api/cajas").json()

    assert respuesta["total_pendientes"] == 3
    assert [tx["id"] for tx in respuesta["pendientes"]] == ["p0", "p1", "p2"]
    assert respuesta["pendientes"][2]["monto"] == 3 and respuesta["pendientes"][2]["estado"] == "pendiente"
    assert respuesta["cajas"] == [2, 1]


def test_cajas_muestra_como_mucho_50_pendientes(entorno):
    cliente, _, redis = entorno
    for i in range(60):
        guardar_tx(redis, f"p{i}", "pendiente")
        redis.rpush("tx:pendientes", f"p{i}")

    respuesta = cliente.get("/api/cajas").json()

    assert respuesta["total_pendientes"] == 60
    assert len(respuesta["pendientes"]) == 50


def test_cajas_vacias(entorno):
    cliente, _, _ = entorno
    assert cliente.get("/api/cajas").json() == {"pendientes": [], "total_pendientes": 0, "cajas": []}


# ── Mineros (BC-28) ──




def test_bloques_en_orden_ascendente_para_bajar_solo_lo_nuevo(entorno):
    cliente, _, redis = entorno
    hashes, _ = armar_cadena(redis, 5)
    assert [b["altura"] for b in cliente.get("/api/bloques", params={"orden": "asc"}).json()] == [0, 1, 2, 3, 4]
    assert [b["block_hash"] for b in cliente.get("/api/bloques", params={"orden": "asc", "desde": 3, "limite": 1}).json()] \
        == [hashes[3]]
    assert cliente.get("/api/bloques", params={"orden": "asc", "desde": 9}).json() == []
    assert cliente.get("/api/bloques", params={"orden": "otro"}).status_code == 400


def test_la_prueba_de_merkle_da_la_raiz_que_va_a_llevar_el_bloque_siguiente(entorno):
    cliente, _, redis = entorno
    _, actual = armar_cadena(redis, 4)
    respuesta = cliente.get(f"/api/prueba/{CUENTA['alice']}").json()
    assert respuesta["altura"] == 3 and respuesta["raiz"] == actual.raiz()
    assert respuesta["hoja"] == f"{CUENTA['alice']}:usuario:{actual.saldos[CUENTA['alice']]}:3"
    assert estado.raiz_de_rama(respuesta["hoja"], respuesta["rama"]) == actual.raiz()
    redis.hincrby("saldos", CUENTA["alice"], 1000)  # alguien tocó el caché: la raíz ya no es la de la cadena
    assert cliente.get(f"/api/prueba/{CUENTA['alice']}").json()["raiz"] == actual.raiz()  # mismo bloque: en memoria
    assert cliente.get(f"/api/prueba/{CUENTA['zoe']}").json()["hoja"] is None
    assert cliente.get("/api/prueba/alice").status_code == 400


def test_la_lista_de_shares_de_una_epoca_da_su_raiz(entorno):
    cliente, _, redis = entorno
    detalle = {CUENTA["minero"]: {"shares": 7, "racha": 2, "monto": 100}}
    redis.hset("epoca:3", mapping={"fondo": 100, "detalle": json.dumps(detalle)})
    respuesta = cliente.get("/api/epocas/3").json()
    assert respuesta == {"epoca": 3, "fondo": 100, "raiz": contrato.raiz_shares(detalle), "detalle": detalle,
                         "lista": [f"{CUENTA['minero']}:7:2"]}
    assert cliente.get("/api/epocas/4").status_code == 404 and cliente.get("/api/epocas/x").status_code == 400


# ── BC-86: firmas de los validadores y finalidad ──

def con_validadores(redis, *nombres):
    for nombre in nombres:
        redis.hset("cuentas", CUENTA[nombre], json.dumps({"tipo": "validador", "nombre": nombre, "alta": 1}))


def test_dos_de_tres_validadores_finalizan_el_bloque_y_se_avisa(entorno):
    cliente, _, redis = entorno
    hashes, _ = armar_cadena(redis, 4)
    con_validadores(redis, "v1", "v2", "v3")
    assert cliente.get("/api/finalidad").json()["altura"] == -1

    uno = cliente.post("/api/firmas", json=contrato.firma_bloque(PRIVADA["v1"], 3, hashes[3]))
    assert uno.json() == {"resultado": "aceptada", "finalizada": -1}
    dos = cliente.post("/api/firmas", json=contrato.firma_bloque(PRIVADA["v2"], 2, hashes[2]))
    assert dos.json()["finalizada"] == 2  # v1 firmó la 3 (que incluye la 2) y v2 la 2

    final = cliente.get("/api/finalidad").json()
    assert (final["altura"], final["block_hash"], final["umbral"]) == (2, hashes[2], 2)
    bloques = {b["altura"]: b for b in cliente.get("/api/bloques").json()}
    assert bloques[2]["finalizado"] and not bloques[3]["finalizado"]
    assert [f["validador"] for f in bloques[3]["firmas"]] == [CUENTA["v1"]]
    assert cliente.post("/api/firmas", json=contrato.firma_bloque(PRIVADA["v1"], 3, hashes[3])).json()["resultado"] \
        == "repetida"


def test_una_firma_de_quien_no_es_validador_o_de_otro_bloque_no_cuenta(entorno):
    cliente, _, redis = entorno
    hashes, _ = armar_cadena(redis, 3)
    con_validadores(redis, "v1")
    assert cliente.post("/api/firmas", json=contrato.firma_bloque(PRIVADA["alice"], 1, hashes[1])).status_code == 403
    assert cliente.post("/api/firmas", json=contrato.firma_bloque(PRIVADA["v1"], 1, "f" * 32)).status_code == 409
    trucha = {**contrato.firma_bloque(PRIVADA["v1"], 1, hashes[1]), "altura": 2}
    assert cliente.post("/api/firmas", json=trucha).status_code == 400


def test_una_doble_firma_deja_la_prueba(entorno):
    cliente, _, redis = entorno
    hashes, _ = armar_cadena(redis, 3)
    con_validadores(redis, "v1")
    assert cliente.post("/api/firmas", json=contrato.firma_bloque(PRIVADA["v1"], 2, hashes[2])).status_code == 200
    redis.lset("cadena", 2, "e" * 32)  # el operador reescribe la altura 2 y el validador firma lo nuevo
    otra = contrato.firma_bloque(PRIVADA["v1"], 2, "e" * 32)
    assert cliente.post("/api/firmas", json=otra).status_code == 409
    [prueba] = cliente.get("/api/finalidad").json()["dobles"]
    assert prueba["primera"]["block_hash"] == hashes[2] and prueba["segunda"]["block_hash"] == "e" * 32


# ── BC-78: CORS para la billetera descargada (origen «null») ──

def test_las_rutas_publicas_tienen_cors_y_el_panel_no(entorno):
    cliente, _, _ = entorno
    origen = {"Origin": "null"}
    assert cliente.get("/api/bloques", headers=origen).headers["access-control-allow-origin"] == "*"
    previo = cliente.options("/api/transacciones", headers={**origen, "Access-Control-Request-Method": "POST",
                                                            "Access-Control-Request-Headers": "content-type"})
    assert previo.status_code == 204 and "POST" in previo.headers["access-control-allow-methods"]
    assert "access-control-allow-origin" not in cliente.get("/api/organizador/cuentas", headers=origen).headers
    firmas = cliente.options("/api/firmas", headers={**origen, "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in firmas.headers


def test_el_modo_organizador_de_la_billetera_lee_rechaza_y_reactiva(entorno):
    """BC-87 y BC-88: los pendientes y los suspendidos, rechazar y reactivar van firmados con X-Organizador; la
    billetera los pide desde el disco."""
    cliente, _, _ = entorno
    encabezados = "content-type, x-organizador, x-tiempo, x-firma"
    for metodo, ruta in (("GET", "/api/organizador/altas"), ("GET", "/api/organizador/suspendidos"),
                         ("POST", "/api/organizador/rechazar"), ("POST", "/api/organizador/reactivar")):
        previo = cliente.options(ruta, headers={"Origin": "null", "Access-Control-Request-Method": metodo,
                                                "Access-Control-Request-Headers": encabezados})
        assert previo.status_code == 204 and "x-organizador" in previo.headers["access-control-allow-headers"]
    assert "access-control-allow-origin" not in cliente.options("/api/organizador/cuentas", headers={
        "Origin": "null", "Access-Control-Request-Method": "GET"}).headers
