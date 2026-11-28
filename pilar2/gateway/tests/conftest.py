"""Lo común de los tests del gateway: Redis real (base 15), colas temporales y mineros que firman sus pedidos."""

import json
import os
import uuid
from pathlib import Path

import pytest
import redis as redislib
from fastapi.testclient import TestClient

from comun import Rabbit, config, contrato, web
from comun.tests.claves import CUENTA, PRIVADA, cadena_actual, sembrar_genesis
from gateway import app as gateway

BASE_PRUEBAS = 15
VECTOR = json.loads((Path(__file__).parents[2] / "comun" / "vector_prueba.json").read_text())


class Minero:
    """Un cliente HTTP que firma cada pedido con la clave de prueba de `nombre`, como los mineros (BC-57)."""

    def __init__(self, cliente, nombre):
        self.cliente, self.nombre = cliente, nombre
        self.cuenta, self.privada = CUENTA[nombre], PRIVADA[nombre]

    def pedir(self, metodo, ruta, datos=None, tiempo=None, encabezados=None):
        cuerpo = json.dumps(datos).encode() if datos is not None else b""
        firma = contrato.firmar_pedido(self.privada, metodo, ruta, cuerpo, tiempo=tiempo, cadena=cadena_actual())
        return self.cliente.request(metodo, ruta, content=cuerpo,
                                    headers={"content-type": "application/json", **firma, **(encabezados or {})})

    def get(self, ruta, **opciones):
        return self.pedir("GET", ruta, **opciones)

    def post(self, ruta, datos=None, **opciones):
        return self.pedir("POST", ruta, datos, **opciones)


def aprobar(redis, nombre, tipo="minero"):
    redis.hset("cuentas", CUENTA[nombre], json.dumps({"tipo": tipo, "nombre": nombre, "alta": 1793836800}))


def presentado(redis, nombre):
    """Como si `nombre` ya hubiera hecho el /hello y el /bench."""
    redis.hset(f"nodo:{CUENTA[nombre]}", mapping={"tipo": "cpu", "placa": "prueba", "alias": nombre, "bench": 1_000_000})


@pytest.fixture
def redis():
    if os.environ.get("INTEGRACION") != "1":
        pytest.skip("necesita Redis y RabbitMQ (INTEGRACION=1)")
    c = config.cargar("prueba")
    cliente = redislib.Redis(host=c.redis_host, port=c.redis_puerto, password=c.redis_clave or None,
                             db=BASE_PRUEBAS, decode_responses=True)
    cliente.flushdb()
    sembrar_genesis(cliente)
    for nombre in ("minero", "minero2"):
        aprobar(cliente, nombre)
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
    """Un gateway (un «pod») sobre la misma base y las mismas colas, con el cliente de `minero`."""
    rabbit, nombres = colas

    def armar_gateway(nombre="minero", **ajustes):
        opciones = dict(cola_rangos=nombres["rangos"], cola_resultados=nombres["resultados"])
        opciones.update(ajustes)
        g = gateway.Gateway(redis, rabbit, **opciones)
        app = web.crear_app("gateway", {})
        gateway.agregar_rutas(app, g)
        return g, Minero(TestClient(app), nombre)
    return armar_gateway


@pytest.fixture
def cliente(armar):
    return armar()[1]


@pytest.fixture
def otro(armar):
    """Otro minero aprobado, contra el mismo gateway."""
    return armar("minero2")[1]
