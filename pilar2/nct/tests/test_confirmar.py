"""Ticket 04 (BC-26 y BC-62): una solución válida termina en un bloque nuevo con la coinbase de su ganador."""

import json
import threading

from prometheus_client import REGISTRY

from comun import contrato
from comun.tests.claves import CUENTA, carga
from nct.tests.escenario import datos, guardar_recibida, minar, saldo, tarea_en_curso


def avisos_de_bloque(publicador):
    return [aviso for clave, aviso in publicador.avisos if clave == "bloque.confirmado"]


def test_una_solucion_valida_guarda_el_bloque_con_su_coinbase_mueve_saldos_y_avisa(armar, redis, publicador):
    nct = armar()
    id_tarea = tarea_en_curso(redis, nct, prefijo="00")
    genesis = redis.lindex("cadena", 0)
    solucion = minar(redis, id_tarea)

    nct.procesar_solucion(solucion)

    assert redis.lrange("cadena", 0, -1) == [genesis, solucion["hash"]]
    bloque = redis.hgetall(f"bloque:{solucion['hash']}")
    assert (bloque["previous_hash"], bloque["altura"], bloque["nonce"]) == (genesis, "1", str(solucion["nonce"]))
    assert bloque["prefijo"] == "00"
    # El minero y la recompensa no se guardan aparte: están en la coinbase, adentro del hash.
    assert "minero" not in bloque and "recompensa" not in bloque
    coinbase, *transferencias = json.loads(bloque["transactions"])
    assert coinbase == contrato.coinbase(1, CUENTA["minero"], 20, int(bloque["timestamp"]))
    assert [tx["monto"] for tx in transferencias] == [30, 20]
    texto = contrato._texto(genesis, int(bloque["timestamp"]), json.loads(bloque["transactions"]))
    assert contrato.hash_bloque(texto, solucion["nonce"]) == solucion["hash"]
    # alice 100 → 70; bob 50 + 30 − 20 = 60; carol 0 + 20; el minero cobra 10 × largo del prefijo.
    assert {n: saldo(redis, n) for n in ("alice", "bob", "carol", "minero")} == {
        "alice": 70, "bob": 60, "carol": 20, "minero": 20}
    assert "emision" not in redis.hgetall("saldos")
    assert {int(v) for v in redis.hgetall("comprometido").values()} == {0}
    for tx in json.loads(bloque["transactions"]):
        assert redis.hmget(f"tx:{tx['id']}", "estado", "bloque") == ["confirmada", solucion["hash"]]
    assert redis.hget(f"tarea:{id_tarea}", "estado") == "confirmada"
    assert redis.llen("cajas") == 0 and not redis.exists("tarea:actual")
    assert redis.hgetall(f"historial:{CUENTA['minero']}") == {"bloques": "1", "recompensas": "20"}
    assert avisos_de_bloque(publicador) == [{"tarea": id_tarea, "block_hash": solucion["hash"], "altura": 1}]


def test_despues_de_confirmar_arma_la_tarea_siguiente(armar, redis, publicador):
    nct = armar(tx_por_caja=1)
    id_tarea = tarea_en_curso(redis, nct)  # dos transacciones: dos cajas
    solucion = minar(redis, id_tarea)

    nct.procesar_solucion(solucion)

    [_, siguiente] = publicador.tareas()
    assert siguiente["previous_hash"] == solucion["hash"] and siguiente["altura"] == 2
    assert redis.get("tarea:actual") == siguiente["tarea"]


def test_una_solucion_repetida_no_duplica_el_bloque_y_repite_el_aviso(armar, redis, publicador):
    nct = armar()
    id_tarea = tarea_en_curso(redis, nct)
    solucion = minar(redis, id_tarea)

    nct.procesar_solucion(solucion)
    nct.procesar_solucion(solucion)  # el pod murió antes del ack y volvió el mensaje

    assert redis.llen("cadena") == 2
    assert saldo(redis, "minero") == 10
    assert len(avisos_de_bloque(publicador)) == 2 and avisos_de_bloque(publicador)[0] == avisos_de_bloque(publicador)[1]


def test_dos_mineros_con_la_misma_tarea_minan_textos_distintos(armar, redis):
    nct = armar()
    tarea = datos(redis, tarea_en_curso(redis, nct))
    assert contrato.texto_para(tarea, CUENTA["minero"]) != contrato.texto_para(tarea, CUENTA["minero2"])


def test_dos_soluciones_de_la_misma_tarea_a_la_vez_dejan_un_solo_bloque(armar, redis, publicador):
    nct = armar()
    id_tarea = tarea_en_curso(redis, nct)
    soluciones = [minar(redis, id_tarea, minero=CUENTA["minero"]), minar(redis, id_tarea, minero=CUENTA["minero2"])]
    replicas = [armar(), armar()]
    hilos = [threading.Thread(target=r.procesar_solucion, args=(s,)) for r, s in zip(replicas, soluciones)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    assert redis.llen("cadena") == 2
    ganador = json.loads(redis.hget(f"bloque:{redis.lindex('cadena', -1)}", "transactions"))[0]["destino"]
    perdedor = ({CUENTA["minero"], CUENTA["minero2"]} - {ganador}).pop()
    assert saldo(redis, ganador) == 10 and saldo(redis, perdedor) == 0


def test_robar_una_solucion_no_sirve(armar, redis, publicador):
    """Otro minero manda el nonce que encontró `minero`, a su nombre: con su coinbase el hash es otro."""
    nct = armar()
    id_tarea = tarea_en_curso(redis, nct, prefijo="00")
    robada = {**minar(redis, id_tarea), "minero": CUENTA["ladron"]}

    nct.procesar_solucion(robada)

    assert redis.llen("cadena") == 1 and saldo(redis, "ladron") == 0
    assert redis.hget(f"tarea:{id_tarea}", "estado") == "rechazada"


def test_una_coinbase_inflada_no_entra(armar, redis):
    """Un nonce minado sobre un texto con la recompensa cambiada no da el hash del texto que arma el NCT."""
    nct = armar()
    id_tarea = tarea_en_curso(redis, nct, prefijo="00")
    tarea = datos(redis, id_tarea)
    inflada = [contrato.coinbase(1, CUENTA["minero"], 10**6, tarea["timestamp"]), *tarea["transacciones"]]
    texto = contrato._texto(tarea["previous_hash"], tarea["timestamp"], inflada)

    nct.procesar_solucion(minar(redis, id_tarea, texto=texto))

    assert redis.llen("cadena") == 1 and saldo(redis, "minero") == 0


def test_una_carga_confirmada_le_suma_al_destino_sin_restarle_a_nadie(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    nct.validar(guardar_recibida(redis, carga(1, "zoe", 500)))
    nct.cerrar_cajas()
    id_tarea = nct.armar_si_libre()
    redis.hset(f"tarea:{id_tarea}", "prefijo", "0")

    nct.procesar_solucion(minar(redis, id_tarea))

    assert saldo(redis, "zoe") == 500 and saldo(redis, "minero") == 10
    assert redis.hget("tx:carga-1", "estado") == "confirmada"
    assert "emision" not in redis.hgetall("saldos") and "emision" not in redis.hgetall("comprometido")
    assert redis.hget(f"historial:{CUENTA['zoe']}", "recompensas") is None  # una carga no es una recompensa


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


def test_confirmar_no_pisa_lo_que_escribio_el_trp(armar, redis):
    nct = armar()
    id_tarea = tarea_en_curso(redis, nct)
    redis.hset(f"tarea:{id_tarea}", "rangos", "10")  # lo escribe el TrP (BC-21), con el prefijo
    solucion = minar(redis, id_tarea)

    nct.procesar_solucion(solucion)

    assert redis.hmget(f"tarea:{id_tarea}", "rangos", "prefijo") == ["10", "0"]
    assert redis.hget(f"tarea:{id_tarea}", "block_hash") == solucion["hash"]
