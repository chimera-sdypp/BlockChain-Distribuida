"""Ticket 01: el génesis se carga una sola vez."""

import threading

from comun import contrato


def test_con_la_cadena_vacia_carga_el_genesis_con_los_saldos_iniciales(armar, redis):
    armar().cargar_genesis()

    [hash_] = redis.lrange("cadena", 0, -1)
    bloque = redis.hgetall(f"bloque:{hash_}")
    assert bloque["previous_hash"] == contrato.CEROS
    assert bloque["altura"] == "0"
    assert redis.hgetall("saldos") == {"alice": "100", "bob": "50", "carol": "0"}


def test_con_la_cadena_existente_no_toca_nada(armar, redis):
    redis.rpush("cadena", "punta")
    redis.hset("saldos", "alice", 7)

    armar().cargar_genesis()

    assert redis.lrange("cadena", 0, -1) == ["punta"]
    assert redis.hgetall("saldos") == {"alice": "7"}


def test_dos_replicas_que_arrancan_juntas_cargan_un_solo_genesis(armar, redis):
    replicas = [armar() for _ in range(8)]
    hilos = [threading.Thread(target=r.cargar_genesis) for r in replicas]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    assert redis.llen("cadena") == 1
    assert redis.hget("saldos", "alice") == "100"
