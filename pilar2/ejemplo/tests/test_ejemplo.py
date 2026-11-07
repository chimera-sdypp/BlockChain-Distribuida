from fastapi import FastAPI
from fastapi.testclient import TestClient

from ejemplo import app as ejemplo


class RedisFalso:
    def __init__(self):
        self.valores = {}

    def incr(self, clave):
        self.valores[clave] = self.valores.get(clave, 0) + 1
        return self.valores[clave]


class PublicadorFalso:
    def __init__(self, falla=False):
        self.publicados, self.falla = [], falla

    def publicar(self, cola, mensaje):
        if self.falla:
            raise ConnectionError("RabbitMQ caído")
        self.publicados.append((cola, mensaje))


def cliente(publicador):
    app = FastAPI()
    ejemplo.agregar_rutas(app, publicador)
    return TestClient(app)


def test_procesar_cuenta_en_redis():
    redis = RedisFalso()
    assert ejemplo.procesar(redis, {"texto": "a"}) == 1
    assert ejemplo.procesar(redis, {"texto": "b"}) == 2
    assert redis.valores == {"ejemplo:procesados": 2}


def test_post_publica_en_la_cola():
    publicador = PublicadorFalso()
    respuesta = cliente(publicador).post("/mensajes", json={"texto": "hola"})
    assert respuesta.status_code == 202
    assert publicador.publicados == [("ejemplo", {"texto": "hola"})]


def test_post_invalido_da_422():
    publicador = PublicadorFalso()
    assert cliente(publicador).post("/mensajes", json={"texto": ""}).status_code == 422
    assert cliente(publicador).post("/mensajes", json={}).status_code == 422
    assert publicador.publicados == []


def test_sin_rabbitmq_da_503():
    respuesta = cliente(PublicadorFalso(falla=True)).post("/mensajes", json={"texto": "hola"})
    assert respuesta.status_code == 503
