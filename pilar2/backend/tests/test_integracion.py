"""El backend contra RabbitMQ y Redis de verdad. Corren con INTEGRACION=1 (en el CI, con services)."""

import os
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from backend import app as backend
from comun import Rabbit, config, redisdb, web

pytestmark = pytest.mark.skipif(os.environ.get("INTEGRACION") != "1",
                                reason="necesita RabbitMQ y Redis (INTEGRACION=1)")


@pytest.fixture
def entorno():
    """Una cola temporal (para no robarle mensajes a un NCT que esté corriendo) y Redis real."""
    c = config.cargar("prueba")
    cola = f"prueba-{uuid.uuid4().hex[:8]}"
    publicador = Rabbit(c, [cola])
    publicador.conectar()
    redis = redisdb.conectar(c)
    app = web.crear_app("backend", {})
    backend.agregar_rutas(app, redis, publicador, cola=cola)
    creadas = []
    yield TestClient(app), cola, redis, creadas
    if creadas:
        redis.delete(*creadas)
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
    cliente, cola, redis, creadas = entorno
    respuesta = cliente.post("/api/transacciones", json={"origen": "alice", "destino": "bob", "monto": 7})

    assert respuesta.status_code == 202
    id_ = respuesta.json()["id"]
    creadas.append(f"tx:{id_}")
    [mensaje] = recibir(cola, 1)
    assert mensaje["id"] == id_
    assert (mensaje["origen"], mensaje["destino"], mensaje["monto"]) == ("alice", "bob", 7)
    assert redis.hget(f"tx:{id_}", "estado") == "recibida"
