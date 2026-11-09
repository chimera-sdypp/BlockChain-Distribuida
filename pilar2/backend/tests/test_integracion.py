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
    backend.agregar_rutas(app, redis, publicador, cola=cola)
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
    respuesta = cliente.post("/api/transacciones", json={"origen": "alice", "destino": "bob", "monto": 7})

    assert respuesta.status_code == 202
    id_ = respuesta.json()["id"]
    [mensaje] = recibir(cola, 1)
    assert mensaje["id"] == id_
    assert (mensaje["origen"], mensaje["destino"], mensaje["monto"]) == ("alice", "bob", 7)
    assert redis.hget(f"tx:{id_}", "estado") == "recibida"


# ── Ticket 02: consultar una transacción y los saldos ──

def test_get_de_una_transaccion_recien_recibida(entorno):
    cliente, _, _ = entorno
    id_ = cliente.post("/api/transacciones", json={"origen": "alice", "destino": "bob", "monto": 7}).json()["id"]

    respuesta = cliente.get(f"/api/transacciones/{id_}")

    assert respuesta.status_code == 200
    tx = respuesta.json()
    assert {k: tx[k] for k in ("id", "origen", "destino", "monto", "estado")} == {
        "id": id_, "origen": "alice", "destino": "bob", "monto": 7, "estado": "recibida"}
    assert isinstance(tx["timestamp"], int)
    assert "motivo" not in tx and "bloque" not in tx


def test_get_muestra_el_motivo_de_una_rechazada_y_el_bloque_de_una_confirmada(entorno):
    cliente, _, redis = entorno
    # Lo que escribe el NCT (BC-24 y BC-26).
    redis.hset("tx:r", mapping={"origen": "ana", "destino": "bob", "monto": 99, "timestamp": 1,
                                "estado": "rechazada", "motivo": "saldo insuficiente"})
    redis.hset("tx:c", mapping={"origen": "ana", "destino": "bob", "monto": 5, "timestamp": 2,
                                "estado": "confirmada", "bloque": "0000abc"})

    rechazada = cliente.get("/api/transacciones/r").json()
    confirmada = cliente.get("/api/transacciones/c").json()

    assert (rechazada["estado"], rechazada["motivo"]) == ("rechazada", "saldo insuficiente")
    assert (confirmada["estado"], confirmada["bloque"]) == ("confirmada", "0000abc")


def test_get_de_una_transaccion_que_no_existe_da_404(entorno):
    cliente, _, _ = entorno
    assert cliente.get("/api/transacciones/nada").status_code == 404


def test_saldos(entorno):
    cliente, _, redis = entorno
    redis.hset("saldos", mapping={"alice": 750, "bob": 1250})

    assert cliente.get("/api/saldos").json() == {"alice": 750, "bob": 1250}
    assert cliente.get("/api/saldos/bob").json() == {"usuario": "bob", "saldo": 1250}
    assert cliente.get("/api/saldos/zoe").json() == {"usuario": "zoe", "saldo": 0}


# ── Ticket 03: recorrer la cadena ──

def guardar_bloque(redis, bloque):
    """Como lo guarda el NCT (NCT.4): bloque:<hash> con `transactions` en JSON, y el hash al final de `cadena`."""
    redis.hset(f"bloque:{bloque['block_hash']}", mapping={**bloque, "transactions": json.dumps(bloque["transactions"])})
    redis.rpush("cadena", bloque["block_hash"])


def armar_cadena(redis, largo):
    """Génesis más largo−1 bloques de prueba; devuelve los hashes en orden de altura."""
    genesis = contrato.bloque_genesis({"alice": 1000, "bob": 1000}, 1793836800)
    guardar_bloque(redis, genesis)
    hashes = [genesis["block_hash"]]
    for altura in range(1, largo):
        tx = {"id": f"tx-{altura}", "origen": "alice", "destino": "bob", "monto": altura, "timestamp": 1793836800 + altura}
        texto = contrato.texto_bloque(hashes[-1], 1793836800 + altura, [tx])
        bloque = {"previous_hash": hashes[-1], "timestamp": 1793836800 + altura, "transactions": [tx],
                  "nonce": altura, "block_hash": contrato.hash_bloque(texto, altura),
                  "prefijo": "0", "minero": "gpu-1", "recompensa": 10, "altura": altura}
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
    assert (punta["nonce"], punta["timestamp"], punta["recompensa"]) == (2, 1793836802, 10)
    assert punta["transactions"] == [{"id": "tx-2", "origen": "alice", "destino": "bob",
                                      "monto": 2, "timestamp": 1793836802}]


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
    cuerpo = [{"origen": "alice", "destino": "bob", "monto": m} for m in range(1, 51)]

    respuesta = cliente.post("/api/transacciones/lote", json=cuerpo)

    assert respuesta.status_code == 202
    ids = respuesta.json()["ids"]
    mensajes = recibir(cola, 50)
    assert [m["id"] for m in mensajes] == ids
    assert [m["monto"] for m in mensajes] == list(range(1, 51))
    assert all(redis.hget(f"tx:{id_}", "estado") == "recibida" for id_ in ids)
