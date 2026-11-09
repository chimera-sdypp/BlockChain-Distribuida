"""El backend contra RabbitMQ y Redis de verdad. Corren con INTEGRACION=1 (en el CI, con services)."""

import os
import threading
import time
import uuid

import pytest
import redis as redislib
from fastapi.testclient import TestClient

from backend import app as backend
from comun import Rabbit, config, web

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
