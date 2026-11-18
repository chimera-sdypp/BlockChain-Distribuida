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
from comun import Rabbit, config, contrato, web
from comun.tests.claves import CUENTA, Billetera, carga, firmada

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
    """Génesis más largo−1 bloques de prueba, como los arma el NCT; devuelve los hashes en orden de altura."""
    genesis = contrato.bloque_genesis({CUENTA["alice"]: 1000, CUENTA["bob"]: 1000}, 1793836800)
    guardar_bloque(redis, genesis)
    hashes, billetera = [genesis["block_hash"]], Billetera()
    for altura in range(1, largo):
        tarea = {"previous_hash": hashes[-1], "timestamp": 1793836800 + altura, "altura": altura, "prefijo": "0",
                 "transacciones": [billetera("alice", "bob", altura, timestamp=1793836800 + altura)]}
        transacciones = contrato.transacciones_del_bloque(tarea, CUENTA["minero"])
        bloque = {"previous_hash": hashes[-1], "timestamp": tarea["timestamp"], "transactions": transacciones,
                  "nonce": altura, "block_hash": contrato.hash_bloque(contrato.texto_para(tarea, CUENTA["minero"]), altura),
                  "prefijo": "0", "altura": altura}
        guardar_bloque(redis, bloque)
        hashes.append(bloque["block_hash"])
    return hashes


def test_bloques_del_mas_nuevo_al_mas_viejo_con_altura(entorno):
    cliente, _, redis = entorno
    hashes = armar_cadena(redis, 3)

    bloques = cliente.get("/api/bloques").json()

    assert [b["altura"] for b in bloques] == [2, 1, 0]
    assert [b["block_hash"] for b in bloques] == hashes[::-1]
    punta = bloques[0]
    assert punta["previous_hash"] == hashes[1]
    assert (punta["nonce"], punta["timestamp"]) == (2, 1793836802)
    coinbase, transferencia = punta["transactions"]
    assert coinbase == contrato.coinbase(2, CUENTA["minero"], 10, 1793836802)
    assert (transferencia["monto"], transferencia["nonce"]) == (2, 2)
    # El minero y la recompensa no se guardan aparte: los saca de la coinbase.
    assert (punta["minero"], punta["recompensa"]) == (CUENTA["minero"], 10)
    assert (bloques[2]["minero"], bloques[2]["recompensa"]) == ("", 0)  # el génesis


def test_bloques_paginados(entorno):
    cliente, _, redis = entorno
    hashes = armar_cadena(redis, 5)

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
    hashes = armar_cadena(redis, 3)

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

def test_mineros_con_si_estan_vivos_y_su_saldo(entorno):
    cliente, _, redis = entorno
    ahora = int(time.time())
    # Lo que escribe el gateway (BC-43).
    redis.hset("nodo:gpu-1", mapping={"tipo": "gpu", "placa": "RTX 4060", "bench": 2400000000,
                                      "hashes_por_segundo": 2350000000.5, "ultimo_aviso": ahora - 2})
    redis.hset("nodo:cpu-1", mapping={"tipo": "cpu", "placa": "Ryzen 7", "bench": 1100000,
                                      "hashes_por_segundo": 1000000, "ultimo_aviso": ahora - 60})
    redis.hset("saldos", mapping={"gpu-1": 40, "alice": 1000})

    mineros = {m["id"]: m for m in cliente.get("/api/mineros").json()}

    assert set(mineros) == {"gpu-1", "cpu-1"}
    gpu = mineros["gpu-1"]
    assert (gpu["tipo"], gpu["placa"], gpu["vivo"], gpu["saldo"]) == ("gpu", "RTX 4060", True, 40)
    assert (gpu["bench"], gpu["hashes_por_segundo"], gpu["ultimo_aviso"]) == (2400000000, 2350000000.5, ahora - 2)
    assert (mineros["cpu-1"]["vivo"], mineros["cpu-1"]["saldo"]) == (False, 0)


def test_mineros_sin_mineros(entorno):
    cliente, _, _ = entorno
    assert cliente.get("/api/mineros").json() == []
