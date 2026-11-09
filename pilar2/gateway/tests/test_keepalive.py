"""Ticket 04: el keep-alive renueva el préstamo propio y avisa «cortá» si la tarea se cerró."""

import time

import pytest

from gateway.tests.test_rangos import publicar_rangos, rango


def prestado(cliente, redis, colas, minero="cpu-1", tarea="t1", numero=0):
    publicar_rangos(redis, colas, rango(tarea, numero))
    cliente.get("/tasks/next", params={"minero": minero})
    redis.zadd("prestamos", {f"{tarea}:{numero}": 1})  # como si estuviera por vencer


def aviso(minero="cpu-1", tarea="t1", numero=0, hps=1_000_000):
    return {"minero": minero, "hashes_por_segundo": hps,
            "rango": None if tarea is None else {"tarea": tarea, "rango": numero}}


def test_renueva_el_prestamo_del_minero_y_actualiza_su_nodo(cliente, redis, colas):
    prestado(cliente, redis, colas)

    respuesta = cliente.post("/keepalive", json=aviso(hps=1_234_567))

    assert respuesta.status_code == 200 and respuesta.json() == {"cortar": False}
    assert redis.zscore("prestamos", "t1:0") > time.time() + 25
    assert redis.hget("nodo:cpu-1", "hashes_por_segundo") == "1234567"
    assert abs(int(redis.hget("nodo:cpu-1", "ultimo_aviso")) - time.time()) < 5


def test_no_renueva_el_prestamo_de_otro_minero(cliente, redis, colas):
    prestado(cliente, redis, colas, minero="cpu-1")

    respuesta = cliente.post("/keepalive", json=aviso(minero="cpu-lento"))

    assert respuesta.json() == {"cortar": False}
    assert redis.zscore("prestamos", "t1:0") == 1


def test_si_la_tarea_se_cerro_contesta_cortar_y_borra_el_prestamo(cliente, redis, colas):
    prestado(cliente, redis, colas)
    redis.hset("tarea:t1", "estado", "confirmada")

    respuesta = cliente.post("/keepalive", json=aviso())

    assert respuesta.json() == {"cortar": True}
    assert redis.zscore("prestamos", "t1:0") is None and not redis.exists("prestamo:t1:0")


def test_sin_rango_solo_dice_que_esta_vivo(cliente, redis):
    respuesta = cliente.post("/keepalive", json=aviso(tarea=None))
    assert respuesta.json() == {"cortar": False}
    assert redis.hexists("nodo:cpu-1", "ultimo_aviso")


@pytest.mark.parametrize("malo", [
    {"minero": "c:1", "hashes_por_segundo": 1, "rango": None},
    {"minero": "cpu-1", "hashes_por_segundo": 1, "rango": 3},
    {"minero": "cpu-1", "hashes_por_segundo": 1, "rango": {"tarea": "t1"}},
])
def test_un_keepalive_mal_formado_da_400(cliente, malo):
    assert cliente.post("/keepalive", json=malo).status_code == 400
