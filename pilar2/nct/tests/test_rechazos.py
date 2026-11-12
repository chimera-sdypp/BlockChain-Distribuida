"""Ticket 05 (BC-26): sin solución, soluciones que no sirven y punta cambiada."""

import json

from comun import contrato
from nct.tests.escenario import minar, tarea_en_curso


def rearmada(redis, publicador, vieja):
    """La tarea nueva que salió en lugar de `vieja` (y que quedó como tarea actual)."""
    [_, nueva] = publicador.tareas()
    assert nueva["tarea"] != vieja and redis.get("tarea:actual") == nueva["tarea"]
    return nueva


def test_sin_solucion_rearma_la_misma_caja_con_otro_timestamp(armar, redis, publicador):
    nct = armar()
    vieja = tarea_en_curso(redis, nct)
    [primera] = publicador.tareas()

    nct.procesar_solucion({"tarea": vieja, "sin_solucion": True})

    nueva = rearmada(redis, publicador, vieja)
    assert nueva["transacciones"] == primera["transacciones"]
    assert nueva["timestamp"] > primera["timestamp"]
    assert redis.hget(f"tarea:{vieja}", "estado") == "sin_solucion"
    assert redis.hget(f"tarea:{nueva['tarea']}", "caja") == redis.lindex("cajas", 0)
    assert redis.llen("cadena") == 1


def verificacion_fallida(armar, redis, publicador, estropear):
    nct = armar()
    vieja = tarea_en_curso(redis, nct, prefijo="00")
    solucion = estropear(minar(redis, vieja))

    nct.procesar_solucion(solucion)

    assert redis.llen("cadena") == 1  # no entra ningún bloque
    assert redis.hget(f"tarea:{vieja}", "estado") == "rechazada"
    return rearmada(redis, publicador, vieja)


def test_un_hash_trucho_se_rechaza_y_se_rearma(armar, redis, publicador):
    verificacion_fallida(armar, redis, publicador, lambda s: {**s, "hash": "00" + "f" * 30})


def test_un_nonce_que_no_cumple_el_prefijo_se_rechaza(armar, redis, publicador):
    def otro_nonce(solucion):
        tarea = redis.hgetall(f"tarea:{solucion['tarea']}")
        texto = contrato.texto_bloque(tarea["previous_hash"], int(tarea["timestamp"]), json.loads(tarea["transacciones"]))
        nonce = next(n for n in range(10**6) if not contrato.hash_bloque(texto, n).startswith("00"))
        return {**solucion, "nonce": nonce, "hash": contrato.hash_bloque(texto, nonce)}  # el hash es el real
    verificacion_fallida(armar, redis, publicador, otro_nonce)


def test_un_minero_que_no_es_un_usuario_valido_se_rechaza(armar, redis, publicador):
    verificacion_fallida(armar, redis, publicador, lambda s: {**s, "minero": "gpu:1"})


def test_si_cambio_la_punta_el_bloque_no_entra_y_la_caja_se_vuelve_a_armar(armar, redis, publicador):
    nct = armar()
    vieja = tarea_en_curso(redis, nct)
    solucion = minar(redis, vieja)
    redis.rpush("cadena", "otro-bloque")  # entró otro bloque antes (por ejemplo, de una tarea vencida)

    nct.procesar_solucion(solucion)

    assert redis.lrange("cadena", 1, -1) == ["otro-bloque"]
    assert redis.hget(f"tarea:{vieja}", "estado") == "descartada"
    assert redis.hget("saldos", "gpu-1") is None
    nueva = rearmada(redis, publicador, vieja)
    assert nueva["previous_hash"] == "otro-bloque"
    assert redis.hget(f"tarea:{nueva['tarea']}", "caja") == redis.lindex("cajas", 0)


def test_un_sin_solucion_tardio_de_una_tarea_ya_confirmada_no_rearma(armar, redis, publicador):
    nct = armar()
    vieja = tarea_en_curso(redis, nct)
    nct.procesar_solucion(minar(redis, vieja))

    nct.procesar_solucion({"tarea": vieja, "sin_solucion": True})

    assert len(publicador.tareas()) == 1 and redis.hget(f"tarea:{vieja}", "estado") == "confirmada"
