"""Ticket 05 (BC-26): sin solución, soluciones que no sirven y punta cambiada."""

import json

from comun import contrato
from nct.tests.escenario import minar, tarea_en_curso
from nct.tests.test_validar import recibida


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


def test_si_vence_la_tarea_actual_la_vieja_queda_vencida_y_se_arma_otra_con_la_misma_caja(armar, redis, publicador):
    nct = armar()
    vieja = tarea_en_curso(redis, nct)
    redis.delete("tarea:actual")  # venció sin bloque (TAREA_VENCE_SEGUNDOS)

    nueva = nct.armar_si_libre()

    assert nueva and nueva != vieja and redis.get("tarea:actual") == nueva
    assert redis.hget(f"tarea:{vieja}", "estado") == "vencida"
    assert redis.hget(f"tarea:{nueva}", "estado") == "en_curso"
    assert redis.hget(f"tarea:{nueva}", "caja") == redis.hget(f"tarea:{vieja}", "caja")


def test_la_solucion_tardia_de_una_tarea_vencida_no_entra(armar, redis, publicador):
    nct = armar()
    vieja = tarea_en_curso(redis, nct)
    solucion = minar(redis, vieja)
    redis.delete("tarea:actual")
    nueva = nct.armar_si_libre()

    nct.procesar_solucion(solucion)

    assert redis.llen("cadena") == 1
    assert redis.hget(f"tarea:{vieja}", "estado") == "vencida"
    assert redis.get("tarea:actual") == nueva  # la nueva sigue: nadie le soltó la marca


def test_rearmar_o_confirmar_no_marca_como_vencida_a_la_anterior(armar, redis, publicador):
    nct = armar()
    vieja = tarea_en_curso(redis, nct)
    nct.procesar_solucion({"tarea": vieja, "sin_solucion": True})
    nueva = rearmada(redis, publicador, vieja)["tarea"]
    redis.hset(f"tarea:{nueva}", "prefijo", "0")

    nct.procesar_solucion(minar(redis, nueva))

    assert redis.hget(f"tarea:{vieja}", "estado") == "sin_solucion"
    assert redis.hget(f"tarea:{nueva}", "estado") == "confirmada"


# Lo que pasó el 13/11 en GKE: con prefijo 8 la misma caja se armó varias veces, y la transacción
# quedó en dos bloques seguidos (102 y 103). Pase lo que pase antes, nunca entra dos veces.

def confirmada_y_otra_vez_en_la_fila(armar, redis):
    """Una tarea minada y confirmada, y su misma caja de nuevo primera en `cajas`."""
    nct = armar()
    primera = tarea_en_curso(redis, nct)
    caja = redis.hget(f"tarea:{primera}", "caja")
    nct.procesar_solucion(minar(redis, primera))
    assert redis.llen("cadena") == 2 and redis.llen("cajas") == 0
    redis.rpush("cajas", caja)
    return nct, primera, caja


def test_un_bloque_con_una_transaccion_ya_confirmada_no_entra(armar, redis, publicador):
    nct = armar()
    primera = tarea_en_curso(redis, nct)
    nct.procesar_solucion(minar(redis, primera))
    saldos, comprometido = redis.hgetall("saldos"), redis.hgetall("comprometido")
    # una tarea en curso sobre la punta nueva con las mismas transacciones, como la que hizo el 103
    tarea = {k: v for k, v in redis.hgetall(f"tarea:{primera}").items() if k != "block_hash"}
    redis.hset("tarea:repetida", mapping={**tarea, "previous_hash": redis.lindex("cadena", -1), "estado": "en_curso"})
    redis.set("tarea:actual", "repetida")

    resultado = nct.procesar_solucion(minar(redis, "repetida"))

    assert resultado == "descartada"
    assert redis.llen("cadena") == 2
    assert redis.hgetall("saldos") == saldos and redis.hgetall("comprometido") == comprometido
    assert redis.hget("tarea:repetida", "estado") == "descartada"
    assert redis.get("tarea:actual") is None


def test_una_caja_que_ya_esta_en_la_cadena_sale_de_la_fila_sin_publicar_nada(armar, redis, publicador):
    nct, _, _ = confirmada_y_otra_vez_en_la_fila(armar, redis)
    publicadas = len(publicador.tareas())

    assert nct.armar_si_libre() is None

    assert redis.llen("cajas") == 0 and redis.get("tarea:actual") is None
    assert len(publicador.tareas()) == publicadas


def test_de_una_caja_sale_solo_lo_que_no_esta_en_la_cadena(armar, redis, publicador):
    nct, _, caja = confirmada_y_otra_vez_en_la_fila(armar, redis)
    nueva = recibida(redis, "carol", "alice", 5)
    redis.hset("saldos", "carol", 5)
    nct.validar(nueva)
    redis.delete("cajas", "tx:pendientes")
    redis.rpush("cajas", json.dumps([*json.loads(caja), nueva["id"]]))

    id_tarea = nct.armar_si_libre()

    [*_, tarea] = publicador.tareas()
    assert tarea["tarea"] == id_tarea
    assert [tx["id"] for tx in tarea["transacciones"]] == [nueva["id"]]


def test_si_al_rearmar_la_caja_ya_esta_en_la_cadena_se_sigue_con_la_proxima(armar, redis, publicador):
    nct, _, caja = confirmada_y_otra_vez_en_la_fila(armar, redis)
    vieja = nct.armar_si_libre()  # nada: la caja sobraba
    assert vieja is None
    # una tarea vieja de esa caja, todavía en curso, vuelve sin solución
    redis.rpush("cajas", caja)
    redis.hset("tarea:vieja", mapping={"caja": caja, "estado": "en_curso", "timestamp": 1793836800,
                                       "previous_hash": redis.lindex("cadena", -1), "publicada": 0})
    redis.set("tarea:actual", "vieja")

    nct.procesar_solucion({"tarea": "vieja", "sin_solucion": True})

    assert redis.llen("cajas") == 0 and redis.get("tarea:actual") is None
