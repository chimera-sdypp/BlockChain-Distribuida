"""Ticket 04: el keep-alive renueva el préstamo propio y avisa «cortá» si la tarea se cerró."""

import time

import pytest

from comun.tests.claves import CUENTA
from gateway.tests.test_rangos import publicar_rangos, rango


def prestado(cliente, redis, colas, tarea="t1", numero=0, **opciones):
    publicar_rangos(redis, colas, rango(tarea, numero, **opciones))
    cliente.get("/tasks/next")
    redis.zadd("prestamos", {f"{tarea}:{numero}": 1})  # como si estuviera por vencer


def aviso(nombre="minero", tarea="t1", numero=0, hps=1_000_000, **extra):
    return {"minero": CUENTA[nombre], "hashes_por_segundo": hps,
            "rango": None if tarea is None else {"tarea": tarea, "rango": numero}, **extra}


def test_renueva_el_prestamo_del_minero_y_actualiza_su_nodo(cliente, redis, colas):
    prestado(cliente, redis, colas)

    respuesta = cliente.post("/keepalive", aviso(hps=1_234_567))

    assert respuesta.status_code == 200 and respuesta.json() == {"cortar": False}
    assert redis.zscore("prestamos", "t1:0") > time.time() + 25
    assert redis.hget(f"nodo:{CUENTA['minero']}", "hashes_por_segundo") == "1234567"
    assert abs(int(redis.hget(f"nodo:{CUENTA['minero']}", "ultimo_aviso")) - time.time()) < 5


def test_no_renueva_el_prestamo_de_otro_minero(cliente, otro, redis, colas):
    prestado(cliente, redis, colas)

    respuesta = otro.post("/keepalive", aviso(nombre="minero2"))

    assert respuesta.json() == {"cortar": False}
    assert redis.zscore("prestamos", "t1:0") == 1


def test_si_la_tarea_se_cerro_contesta_cortar_y_borra_el_prestamo(cliente, redis, colas):
    prestado(cliente, redis, colas)
    redis.hset("tarea:t1", "estado", "confirmada")

    respuesta = cliente.post("/keepalive", aviso())

    assert respuesta.json() == {"cortar": True}
    assert redis.zscore("prestamos", "t1:0") is None and not redis.exists("prestamo:t1:0")


def test_sin_rango_solo_dice_que_esta_vivo(cliente, redis):
    respuesta = cliente.post("/keepalive", aviso(tarea=None))
    assert respuesta.json() == {"cortar": False}
    assert redis.hexists(f"nodo:{CUENTA['minero']}", "ultimo_aviso")


@pytest.mark.parametrize("malo", [
    {"minero": CUENTA["minero"], "hashes_por_segundo": 1, "rango": 3},
    {"minero": CUENTA["minero"], "hashes_por_segundo": 1, "rango": {"tarea": "t1"}},
    {"minero": CUENTA["minero2"], "hashes_por_segundo": 1, "rango": None},
    {"minero": CUENTA["minero"], "hashes_por_segundo": 1, "rango": None, "shares": [-1]},
])
def test_un_keepalive_mal_formado_da_400(cliente, malo):
    assert cliente.post("/keepalive", malo).status_code == 400
