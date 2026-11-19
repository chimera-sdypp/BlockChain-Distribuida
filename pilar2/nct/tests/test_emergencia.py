"""BC-73: la `duracion` cuenta los rearmados y la tarea sin bloque vence a los EMERGENCIA_SEGUNDOS."""

import time

from comun.tests.claves import CUENTA
from nct.tests.escenario import minar, tarea_en_curso
from trp.app import Ajustes, particionar


def test_la_tarea_actual_vence_a_los_segundos_de_emergencia(armar, redis):
    nct = armar(tarea_vence=600, emergencia=240)
    tarea_en_curso(redis, nct)
    assert 230 < redis.ttl("tarea:actual") <= 240


def test_sin_emergencia_vence_a_los_segundos_de_siempre(armar, redis):
    nct = armar(tarea_vence=600, emergencia=0)
    tarea_en_curso(redis, nct)
    assert 590 < redis.ttl("tarea:actual") <= 600


def test_al_rearmar_tambien_vence_a_los_segundos_de_emergencia(armar, redis, publicador):
    nct = armar(tarea_vence=600, emergencia=240)
    id_tarea = tarea_en_curso(redis, nct)
    nct.procesar_solucion({"tarea": id_tarea, "sin_solucion": True})
    assert 230 < redis.ttl("tarea:actual") <= 240


def test_la_tarea_lleva_cuando_se_armo_la_caja_y_al_rearmar_se_conserva(armar, redis, publicador):
    nct = armar()
    antes = time.time()
    id_tarea = tarea_en_curso(redis, nct)
    [primera] = publicador.tareas()
    assert antes - 1 <= primera["armada"] <= time.time()
    assert float(redis.hget(f"tarea:{id_tarea}", "armada")) == primera["armada"]

    nct.procesar_solucion({"tarea": id_tarea, "sin_solucion": True})  # la misma caja, otra tarea

    [_, segunda] = publicador.tareas()
    assert segunda["tarea"] != id_tarea and segunda["armada"] == primera["armada"]


def test_una_caja_nueva_tiene_su_propia_hora(armar, redis, publicador):
    nct = armar(tx_por_caja=1)
    id_tarea = tarea_en_curso(redis, nct)  # dos transacciones: dos cajas
    redis.hset("caja:armada", "desde", time.time() - 500)  # la primera caja lleva 500 s
    nct.procesar_solucion(minar(redis, id_tarea))

    siguiente = publicador.tareas()[-1]
    assert time.time() - siguiente["armada"] < 5


def test_la_duracion_del_bloque_cuenta_los_rearmados(armar, redis, publicador):
    nct = armar()
    id_tarea = tarea_en_curso(redis, nct, prefijo="0")
    redis.hset("caja:armada", "desde", time.time() - 100)  # la caja se armó hace 100 s
    for _ in range(3):  # tres vueltas sin solución: la misma caja, tareas nuevas
        nct.procesar_solucion({"tarea": id_tarea, "sin_solucion": True})
        id_tarea = redis.get("tarea:actual")
        redis.hset(f"tarea:{id_tarea}", "prefijo", "0")
    solucion = minar(redis, id_tarea)

    nct.procesar_solucion(solucion)

    bloque = redis.hgetall(f"bloque:{solucion['hash']}")
    assert 100 <= float(bloque["duracion"]) < 110  # antes de BC-73 daba casi 0 (sólo la última tarea)


def test_sin_nadie_que_la_resuelva_el_prefijo_baja_de_a_uno_cada_240_s(armar, redis, publicador):
    """El NCT rearma la caja cada vez que vence la tarea; el TrP le baja un carácter por cada 240 s."""
    nct = armar(tarea_vence=600, emergencia=240)
    ajustes = Ajustes(espacio=2**38, porcentaje=10, prefijo_base=9, segundos_objetivo=60, ajustar=True,
                      vivo_segundos=15, emergencia=240)
    # Una «GPU» de 2 GH/s que avisa pero no resuelve nada, y la referencia en 9.
    redis.hset("prefijo:referencia", mapping={"potencia": 2e9, "altura": 0, "prefijo": 9, "cuando": 0})
    id_tarea = tarea_en_curso(redis, nct)
    armada = float(redis.hget(f"tarea:{id_tarea}", "armada"))
    prefijos = []
    for vuelta in range(3):
        ahora = armada + 240 * vuelta + 1
        redis.hset("nodo:gpu-falsa", mapping={"tipo": "gpu", "bench": 2_000_000_000, "ultimo_aviso": ahora})
        tarea = publicador.tareas()[-1]
        rangos = []
        particionar(tarea, redis, lambda cola, m: rangos.append(m), ajustes, ahora=ahora)
        prefijos.append(len(rangos[0]["prefijo"]))
        redis.delete("tarea:actual")  # venció a los 240 s
        nct.armar_si_libre()  # el tic: la vieja queda vencida y la misma caja se arma otra vez
        assert redis.hget(f"tarea:{tarea['tarea']}", "estado") == "vencida"

    assert prefijos == [9, 8, 7]
    assert CUENTA["minero"] not in redis.hgetall("saldos")  # nadie minó nada
