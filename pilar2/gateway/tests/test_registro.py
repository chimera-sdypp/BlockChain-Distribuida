"""Ticket 02: token, /hello, /bench y las métricas de mineros."""

import time

import pytest
from prometheus_client import REGISTRY

from comun import contrato


def test_sin_token_o_con_uno_malo_da_401(cliente):
    hola = {"minero": "gpu-1", "tipo": "gpu", "placa": "RTX 4060"}
    assert cliente.post("/hello", json=hola, headers={"Authorization": ""}).status_code == 401
    assert cliente.post("/hello", json=hola, headers={"Authorization": "Bearer otro"}).status_code == 401


def test_health_y_metrics_no_piden_token(cliente):
    assert cliente.get("/health", headers={"Authorization": ""}).status_code == 200
    assert cliente.get("/metrics", headers={"Authorization": ""}).status_code == 200


def test_un_minero_nuevo_recibe_la_tarea_modelo(cliente, redis):
    respuesta = cliente.post("/hello", json={"minero": "gpu-1", "tipo": "gpu", "placa": "RTX 4060"})

    assert respuesta.status_code == 200
    assert respuesta.json() == contrato.BENCH
    nodo = redis.hgetall("nodo:gpu-1")
    assert (nodo["tipo"], nodo["placa"]) == ("gpu", "RTX 4060")
    assert abs(int(nodo["ultimo_aviso"]) - time.time()) < 5


def test_despues_del_bench_queda_registrado(cliente, redis):
    cliente.post("/hello", json={"minero": "cpu-1", "tipo": "cpu", "placa": "Ryzen 5"})

    respuesta = cliente.post("/bench", json={"minero": "cpu-1", "segundos": 15.2, "hashes_por_segundo": 1100000})

    assert respuesta.status_code == 204
    assert redis.hget("nodo:cpu-1", "bench") == "1100000"
    assert cliente.post("/hello", json={"minero": "cpu-1", "tipo": "cpu", "placa": "Ryzen 5"}).json() == {"registrado": True}


@pytest.mark.parametrize("hola", [
    {"minero": "gpu:1", "tipo": "gpu", "placa": "x"},
    {"minero": "gpu-1", "tipo": "tpu", "placa": "x"},
    {"tipo": "gpu", "placa": "x"},
    ["no", "es", "un", "objeto"],
])
def test_hello_invalido_da_400(cliente, hola):
    assert cliente.post("/hello", json=hola).status_code == 400


def test_bench_invalido_da_400(cliente):
    assert cliente.post("/bench", json={"minero": "gpu-1", "hashes_por_segundo": -1}).status_code == 400


def test_las_metricas_cuentan_los_mineros_vivos_por_tipo(armar, redis):
    armar()
    ahora = int(time.time())
    redis.hset("nodo:gpu-1", mapping={"tipo": "gpu", "bench": 2_000_000_000, "hashes_por_segundo": 1_900_000_000,
                                      "ultimo_aviso": ahora})
    redis.hset("nodo:gpu-2", mapping={"tipo": "gpu", "bench": 2_000_000_000, "ultimo_aviso": ahora - 60})  # caído
    redis.hset("nodo:cpu-1", mapping={"tipo": "cpu", "bench": 1_000_000, "ultimo_aviso": ahora})  # sin aviso todavía

    assert REGISTRY.get_sample_value("blockchain_mineros", {"tipo": "gpu"}) == 1
    assert REGISTRY.get_sample_value("blockchain_mineros", {"tipo": "cpu"}) == 1
    assert REGISTRY.get_sample_value("blockchain_hashes_por_segundo", {"tipo": "gpu"}) == 1_900_000_000
    assert REGISTRY.get_sample_value("blockchain_hashes_por_segundo", {"tipo": "cpu"}) == 1_000_000
