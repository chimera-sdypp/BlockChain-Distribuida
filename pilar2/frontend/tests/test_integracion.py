"""El frontend contra RabbitMQ de verdad. Corren con INTEGRACION=1 (en el CI, con services)."""

import os
import time
import uuid

import pytest

from comun import Rabbit, config
from frontend import app as frontend
from frontend.tests.test_frontend import LectorSSE, servidor  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(os.environ.get("INTEGRACION") != "1",
                                reason="necesita RabbitMQ (INTEGRACION=1)")


@pytest.fixture
def exchange():
    """Un exchange propio: así el test no le manda avisos a un sistema corriendo."""
    nombre = f"eventos-prueba-{uuid.uuid4().hex[:8]}"
    yield nombre
    limpiador = Rabbit(config.cargar("prueba"))
    limpiador.conectar()
    limpiador._canal.exchange_delete(nombre)
    limpiador.detener()


def esperar(condicion, segundos=10):
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.05)
    return False


def test_un_bloque_confirmado_publicado_en_rabbitmq_llega_por_sse(servidor, exchange):
    puerto, difusor = servidor()
    c = config.cargar("prueba")
    suscriptor, nct = Rabbit(c), Rabbit(c)
    frontend.escuchar(suscriptor, difusor, exchange=exchange)
    assert esperar(suscriptor.suscripto)
    lector = LectorSSE(puerto)
    lector.siguiente()  # saludo

    nct.conectar()
    nct.avisar(exchange, "bloque.confirmado", {"tarea": "t9", "block_hash": "0000ff", "altura": 7})

    assert lector.siguiente() == ("evento", "bloque.confirmado", {"tarea": "t9", "block_hash": "0000ff", "altura": 7})
    lector.cerrar()
    suscriptor.detener()
    nct.detener()


def test_un_cambio_de_estado_de_una_transaccion_llega_por_sse(servidor, exchange):
    puerto, difusor = servidor()
    c = config.cargar("prueba")
    suscriptor, nct = Rabbit(c), Rabbit(c)
    frontend.escuchar(suscriptor, difusor, exchange=exchange)
    assert esperar(suscriptor.suscripto)
    lector = LectorSSE(puerto)
    lector.siguiente()  # saludo

    nct.conectar()
    nct.avisar(exchange, "transaccion.estado", {"id": "abc", "estado": "rechazada", "motivo": "saldo insuficiente"})

    assert lector.siguiente() == ("evento", "transaccion.estado",
                                  {"id": "abc", "estado": "rechazada", "motivo": "saldo insuficiente"})
    lector.cerrar()
    suscriptor.detener()
    nct.detener()
