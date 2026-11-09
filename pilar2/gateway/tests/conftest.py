"""Lo común de los tests del gateway: Redis real (base 15), colas temporales y un cliente HTTP."""

import os
import uuid

import pytest
import redis as redislib
from fastapi.testclient import TestClient

from comun import Rabbit, config, web
from gateway import app as gateway

BASE_PRUEBAS = 15
TOKEN = "token-de-prueba"


@pytest.fixture
def redis():
    if os.environ.get("INTEGRACION") != "1":
        pytest.skip("necesita Redis y RabbitMQ (INTEGRACION=1)")
    c = config.cargar("prueba")
    cliente = redislib.Redis(host=c.redis_host, port=c.redis_puerto, password=c.redis_clave or None,
                             db=BASE_PRUEBAS, decode_responses=True)
    cliente.flushdb()
    yield cliente
    cliente.flushdb()


@pytest.fixture
def colas(redis):
    """`rangos` y `resultados` temporales: así no se mezclan con un sistema corriendo."""
    sufijo = uuid.uuid4().hex[:8]
    nombres = {"rangos": f"rangos-{sufijo}", "resultados": f"resultados-{sufijo}"}
    rabbit = Rabbit(config.cargar("prueba"), nombres.values())
    rabbit.conectar()
    yield rabbit, nombres
    for cola in nombres.values():
        rabbit._canal.queue_delete(cola)
    rabbit.detener()


@pytest.fixture
def armar(redis, colas):
    """Un gateway (un «pod») sobre la misma base y las mismas colas; se pueden armar varios."""
    rabbit, nombres = colas

    def armar_gateway(**ajustes):
        opciones = dict(token=TOKEN, cola_rangos=nombres["rangos"], cola_resultados=nombres["resultados"])
        opciones.update(ajustes)
        g = gateway.Gateway(redis, rabbit, **opciones)
        app = web.crear_app("gateway", {})
        gateway.agregar_rutas(app, g)
        return g, TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
    return armar_gateway


@pytest.fixture
def cliente(armar):
    return armar()[1]
