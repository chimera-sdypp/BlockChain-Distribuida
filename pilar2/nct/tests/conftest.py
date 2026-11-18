"""Lo común de los tests del NCT: Redis real (base 15, vacía) y un publicador que anota."""

import os

import pytest
import redis as redislib

from comun import config
from comun.tests.claves import CUENTA
from nct import app as nct
from nct.tests import escenario

BASE_PRUEBAS = 15  # una base de Redis aparte: así no se pisa la cadena de un sistema corriendo

SALDOS = {CUENTA["alice"]: 100, CUENTA["bob"]: 50, CUENTA["carol"]: 0}


class PublicadorFalso:
    """Anota lo que el NCT publica en las colas (`tareas`) y en `eventos`."""

    def __init__(self):
        self.publicados, self.avisos = [], []

    def publicar(self, cola, mensaje):
        self.publicados.append((cola, mensaje))

    def avisar(self, exchange, clave, mensaje):
        self.avisos.append((clave, mensaje))

    def tareas(self):
        return [m for cola, m in self.publicados if cola == "tareas"]


@pytest.fixture
def redis():
    if os.environ.get("INTEGRACION") != "1":
        pytest.skip("necesita Redis (INTEGRACION=1)")
    c = config.cargar("prueba")
    cliente = redislib.Redis(host=c.redis_host, port=c.redis_puerto, password=c.redis_clave or None,
                             db=BASE_PRUEBAS, decode_responses=True)
    cliente.flushdb()
    yield cliente
    cliente.flushdb()


@pytest.fixture
def publicador():
    return PublicadorFalso()


@pytest.fixture(autouse=True)
def billetera_nueva():
    """Cada test arranca con los nonces de las cuentas de prueba en 0."""
    escenario.BILLETERA.nonces.clear()


@pytest.fixture
def armar(redis, publicador):
    """Un NCT (una «réplica») sobre la misma base; se pueden armar varios."""
    def armar_nct(**ajustes):
        opciones = dict(saldos_iniciales=SALDOS, organizador=CUENTA["organizador"], wait_replicas=0)
        opciones.update(ajustes)
        return nct.Nct(redis, publicador, **opciones)
    return armar_nct
