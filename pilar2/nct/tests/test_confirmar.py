"""Ticket 04 (BC-26): una solución válida termina en un bloque nuevo."""

import json
import threading

from prometheus_client import REGISTRY

from nct.tests.escenario import minar, tarea_en_curso


def avisos_de_bloque(publicador):
    return [aviso for clave, aviso in publicador.avisos if clave == "bloque.confirmado"]


def test_una_solucion_valida_guarda_el_bloque_mueve_saldos_y_avisa(armar, redis, publicador):
    nct = armar()
    id_tarea = tarea_en_curso(redis, nct, prefijo="00")
    genesis = redis.lindex("cadena", 0)
    solucion = minar(redis, id_tarea)

    nct.procesar_solucion(solucion)

    assert redis.lrange("cadena", 0, -1) == [genesis, solucion["hash"]]
    bloque = redis.hgetall(f"bloque:{solucion['hash']}")
    assert (bloque["previous_hash"], bloque["altura"], bloque["nonce"]) == (genesis, "1", str(solucion["nonce"]))
    assert (bloque["prefijo"], bloque["minero"], bloque["recompensa"]) == ("00", "gpu-1", "20")
    assert [tx["monto"] for tx in json.loads(bloque["transactions"])] == [30, 20]
    # alice 100 → 70; bob 50 + 30 − 20 = 60; carol 0 + 20; el minero cobra 10 × largo del prefijo.
    assert redis.hgetall("saldos") == {"alice": "70", "bob": "60", "carol": "20", "gpu-1": "20"}
    assert {k: int(v) for k, v in redis.hgetall("comprometido").items()} == {"alice": 0, "bob": 0}
    for tx in json.loads(redis.hget(f"tarea:{id_tarea}", "transacciones")):
        assert redis.hmget(f"tx:{tx['id']}", "estado", "bloque") == ["confirmada", solucion["hash"]]
    assert redis.hget(f"tarea:{id_tarea}", "estado") == "confirmada"
    assert redis.llen("cajas") == 0 and not redis.exists("tarea:actual")
    assert avisos_de_bloque(publicador) == [{"tarea": id_tarea, "block_hash": solucion["hash"], "altura": 1}]


def test_despues_de_confirmar_arma_la_tarea_siguiente(armar, redis, publicador):
    nct = armar(tx_por_caja=1)
    id_tarea = tarea_en_curso(redis, nct)  # dos transacciones: dos cajas
    solucion = minar(redis, id_tarea)

    nct.procesar_solucion(solucion)

    [_, siguiente] = publicador.tareas()
    assert siguiente["previous_hash"] == solucion["hash"]
    assert redis.get("tarea:actual") == siguiente["tarea"]


def test_una_solucion_repetida_no_duplica_el_bloque_y_repite_el_aviso(armar, redis, publicador):
    nct = armar()
    id_tarea = tarea_en_curso(redis, nct)
    solucion = minar(redis, id_tarea)

    nct.procesar_solucion(solucion)
    nct.procesar_solucion(solucion)  # el pod murió antes del ack y volvió el mensaje

    assert redis.llen("cadena") == 2
    assert redis.hget("saldos", "gpu-1") == "10"
    assert len(avisos_de_bloque(publicador)) == 2 and avisos_de_bloque(publicador)[0] == avisos_de_bloque(publicador)[1]


def test_dos_soluciones_de_la_misma_tarea_a_la_vez_dejan_un_solo_bloque(armar, redis, publicador):
    nct = armar()
    id_tarea = tarea_en_curso(redis, nct)
    soluciones = [minar(redis, id_tarea, desde=0, minero="gpu-1")]
    soluciones.append(minar(redis, id_tarea, desde=soluciones[0]["nonce"] + 1, minero="gpu-2"))
    replicas = [armar(), armar()]
    hilos = [threading.Thread(target=r.procesar_solucion, args=(s,)) for r, s in zip(replicas, soluciones)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    assert redis.llen("cadena") == 2
    ganador = redis.hget(f"bloque:{redis.lindex('cadena', -1)}", "minero")
    perdedor = {"gpu-1": "gpu-2", "gpu-2": "gpu-1"}[ganador]
    assert redis.hget("saldos", ganador) == "10" and redis.hget("saldos", perdedor) is None


def test_cuenta_los_bloques_y_el_tiempo_por_bloque(armar, redis):
    nct = armar()
    id_tarea = tarea_en_curso(redis, nct)
    bloques = REGISTRY.get_sample_value("blockchain_bloques_total") or 0
    medidos = REGISTRY.get_sample_value("blockchain_tiempo_bloque_segundos_count") or 0

    nct.procesar_solucion(minar(redis, id_tarea))

    assert REGISTRY.get_sample_value("blockchain_bloques_total") == bloques + 1
    assert REGISTRY.get_sample_value("blockchain_tiempo_bloque_segundos_count") == medidos + 1


def test_si_ninguna_replica_de_redis_confirma_a_tiempo_avisa_igual_y_lo_cuenta(armar, redis, publicador):
    nct = armar(wait_replicas=1, wait_ms=50)  # el Redis de los tests no tiene réplicas
    id_tarea = tarea_en_curso(redis, nct)
    sin_replica = REGISTRY.get_sample_value("blockchain_bloques_sin_replica_total") or 0

    nct.procesar_solucion(minar(redis, id_tarea))

    assert len(avisos_de_bloque(publicador)) == 1
    assert REGISTRY.get_sample_value("blockchain_bloques_sin_replica_total") == sin_replica + 1
