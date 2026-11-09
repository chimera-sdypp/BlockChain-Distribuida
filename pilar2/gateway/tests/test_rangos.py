"""Ticket 03: GET /tasks/next y POST /results, con el préstamo de punta a punta."""

import time

import redis as redislib
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from comun import web
from gateway import app as gateway
from gateway.tests.conftest import TOKEN


def rango(tarea, numero, desde=0, hasta=999):
    return {"tarea": tarea, "rango": numero, "bloque": "abc;123;def;", "desde": desde, "hasta": hasta, "prefijo": "00"}


def publicar_rangos(redis, colas, *rangos, estado="en_curso"):
    rabbit, nombres = colas
    for r in rangos:
        redis.hset(f"tarea:{r['tarea']}", "estado", estado)
        rabbit.publicar(nombres["rangos"], r)


def sacar(colas, cola):
    rabbit, nombres = colas
    return rabbit.tomar(nombres[cola], lambda m: m)


def test_sin_minero_da_400(cliente):
    assert cliente.get("/tasks/next").status_code == 400


def test_sin_trabajo_da_204(cliente):
    assert cliente.get("/tasks/next", params={"minero": "cpu-1"}).status_code == 204


def test_entrega_el_rango_y_lo_anota_como_prestamo(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0))
    antes = time.time()

    respuesta = cliente.get("/tasks/next", params={"minero": "cpu-1"})

    assert respuesta.status_code == 200 and respuesta.json() == rango("t1", 0)
    vence = redis.zscore("prestamos", "t1:0")
    assert antes + 29 <= vence <= time.time() + 31
    assert redis.hget("prestamo:t1:0", "minero") == "cpu-1"
    assert sacar(colas, "rangos") is None  # se le hizo ack: no queda en la cola


def test_descarta_los_rangos_de_tareas_cerradas(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("vieja", 0), rango("vieja", 1), estado="confirmada")
    publicar_rangos(redis, colas, rango("nueva", 0))

    respuesta = cliente.get("/tasks/next", params={"minero": "cpu-1"})

    assert respuesta.json()["tarea"] == "nueva"
    assert not redis.exists("prestamo:vieja:0") and redis.zcard("prestamos") == 1


def test_cuenta_los_rangos_entregados(cliente, redis, colas):
    antes = REGISTRY.get_sample_value("blockchain_tareas_entregadas_total") or 0
    publicar_rangos(redis, colas, rango("t1", 0))
    cliente.get("/tasks/next", params={"minero": "cpu-1"})
    assert REGISTRY.get_sample_value("blockchain_tareas_entregadas_total") == antes + 1


def test_si_redis_no_responde_el_rango_vuelve_a_la_cola(redis, colas):
    rabbit, nombres = colas
    publicar_rangos(redis, colas, rango("t1", 0))
    caido = redislib.Redis(port=1, socket_connect_timeout=0.2, decode_responses=True)
    g = gateway.Gateway(caido, rabbit, TOKEN, cola_rangos=nombres["rangos"], cola_resultados=nombres["resultados"])
    app = web.crear_app("gateway", {})
    gateway.agregar_rutas(app, g)

    respuesta = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}).get("/tasks/next", params={"minero": "c"})

    assert respuesta.status_code == 503
    assert sacar(colas, "rangos") == rango("t1", 0)


def test_un_resultado_llega_a_resultados_y_libera_el_prestamo(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 3))
    cliente.get("/tasks/next", params={"minero": "cpu-1"})
    resultado = {"tarea": "t1", "rango": 3, "minero": "cpu-1", "nonce": 42, "hash": "00ab" + "0" * 28}

    respuesta = cliente.post("/results", json=resultado)

    assert respuesta.status_code == 202
    assert sacar(colas, "resultados") == resultado
    assert not redis.exists("prestamo:t1:3") and redis.zscore("prestamos", "t1:3") is None


def test_un_rango_sin_solucion_tambien_llega(cliente, redis, colas):
    resultado = {"tarea": "t1", "rango": 0, "minero": "cpu-1", "sin_solucion": True}
    assert cliente.post("/results", json=resultado).status_code == 202
    assert sacar(colas, "resultados") == resultado


def test_un_resultado_mal_formado_da_400(cliente, colas):
    for malo in ({"tarea": "t1", "rango": 0, "minero": "cpu-1"},
                 {"tarea": "t1", "rango": 0, "minero": "c:1", "nonce": 1, "hash": "x"},
                 {"tarea": "t1", "rango": "0", "minero": "cpu-1", "sin_solucion": True}):
        assert cliente.post("/results", json=malo).status_code == 400, malo
    assert sacar(colas, "resultados") is None
