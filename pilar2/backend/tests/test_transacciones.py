"""POST /api/transacciones, con Redis y RabbitMQ falsos."""

import re

import pytest

import redis
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from backend import app as backend
from comun import web

COLA = "transacciones-prueba"


class RedisFalso:
    def __init__(self, falla=False):
        self.hashes, self.falla = {}, falla

    def hset(self, clave, mapping):
        if self.falla:
            raise redis.ConnectionError("Redis caído")
        self.hashes.setdefault(clave, {}).update({k: str(v) for k, v in mapping.items()})

    def hgetall(self, clave):
        if self.falla:
            raise redis.ConnectionError("Redis caído")
        return dict(self.hashes.get(clave, {}))

    def delete(self, *claves):
        for clave in claves:
            self.hashes.pop(clave, None)


class PublicadorFalso:
    def __init__(self, falla_desde=None):
        self.publicados, self.falla_desde = [], falla_desde

    def publicar(self, cola, mensaje):
        if self.falla_desde is not None and len(self.publicados) + 1 >= self.falla_desde:
            raise ConnectionError("RabbitMQ caído")
        self.publicados.append((cola, mensaje))


def cliente(redis=None, publicador=None):
    app = web.crear_app("backend", {})
    backend.agregar_rutas(app, redis or RedisFalso(), publicador or PublicadorFalso(), cola=COLA)
    return TestClient(app)


def test_post_valido_encola_y_queda_recibida():
    redis, publicador = RedisFalso(), PublicadorFalso()
    respuesta = cliente(redis, publicador).post(
        "/api/transacciones", json={"origen": "alice", "destino": "bob", "monto": 10})

    assert respuesta.status_code == 202
    id_ = respuesta.json()["id"]
    assert re.fullmatch(r"[0-9a-f]{32}", id_)
    [(cola, mensaje)] = publicador.publicados
    assert cola == COLA
    assert mensaje["id"] == id_
    assert (mensaje["origen"], mensaje["destino"], mensaje["monto"]) == ("alice", "bob", 10)
    assert isinstance(mensaje["timestamp"], int)
    tx = redis.hashes[f"tx:{id_}"]
    assert tx["estado"] == "recibida"
    assert (tx["origen"], tx["destino"], tx["monto"]) == ("alice", "bob", "10")


@pytest.mark.parametrize("cuerpo, motivo", [
    ({"destino": "bob", "monto": 10}, "origen"),
    ({"origen": "alice", "destino": "bob", "monto": 0}, "monto"),
    ({"origen": "alice", "destino": "bob", "monto": 1.5}, "monto"),
    ({"origen": "alice", "destino": "alice", "monto": 10}, "distintos"),
    ({"origen": "genesis", "destino": "bob", "monto": 10}, "reservado"),
    ([{"origen": "alice", "destino": "bob", "monto": 10}], "objeto"),
])
def test_formato_invalido_da_400_con_el_motivo_y_no_encola(cuerpo, motivo):
    redis, publicador = RedisFalso(), PublicadorFalso()
    respuesta = cliente(redis, publicador).post("/api/transacciones", json=cuerpo)

    assert respuesta.status_code == 400
    assert any(motivo in error for error in respuesta.json()["errores"])
    assert publicador.publicados == [] and redis.hashes == {}


def test_json_mal_formado_da_400():
    respuesta = cliente().post("/api/transacciones", content=b"{no es json",
                               headers={"content-type": "application/json"})
    assert respuesta.status_code == 400
    assert respuesta.json()["errores"]


def test_si_rabbitmq_falla_da_503_y_no_queda_recibida():
    redis, publicador = RedisFalso(), PublicadorFalso(falla_desde=1)
    respuesta = cliente(redis, publicador).post(
        "/api/transacciones", json={"origen": "alice", "destino": "bob", "monto": 10})

    assert respuesta.status_code == 503
    assert redis.hashes == {}


def test_si_redis_falla_da_503_y_no_encola():
    redis, publicador = RedisFalso(falla=True), PublicadorFalso()
    respuesta = cliente(redis, publicador).post(
        "/api/transacciones", json={"origen": "alice", "destino": "bob", "monto": 10})

    assert respuesta.status_code == 503
    assert publicador.publicados == []


def contadores(c):
    """{resultado: valor} de blockchain_tx_recibidas_total, leído por /metrics."""
    valores = {}
    for familia in text_string_to_metric_families(c.get("/metrics").text):
        for muestra in familia.samples:
            if muestra.name == "blockchain_tx_recibidas_total":
                valores[muestra.labels["resultado"]] = muestra.value
    return valores


def test_la_metrica_cuenta_aceptadas_invalidas_y_errores():
    publicador = PublicadorFalso()
    c = cliente(publicador=publicador)
    antes = contadores(c)
    c.post("/api/transacciones", json={"origen": "alice", "destino": "bob", "monto": 10})
    c.post("/api/transacciones", json={"origen": "alice", "destino": "bob", "monto": 0})
    publicador.falla_desde = 1
    c.post("/api/transacciones", json={"origen": "alice", "destino": "bob", "monto": 10})
    despues = contadores(c)

    for resultado in ("aceptada", "invalida", "error"):
        assert despues[resultado] - antes.get(resultado, 0) == 1, resultado


def test_si_redis_falla_una_consulta_da_503():
    respuesta = cliente(RedisFalso(falla=True)).get("/api/transacciones/abc")
    assert respuesta.status_code == 503
