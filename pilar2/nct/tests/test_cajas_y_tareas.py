"""Ticket 03 (BC-25): cajas por ventana y una sola tarea en curso."""

import json
import threading

from prometheus_client import REGISTRY

from nct.tests.escenario import recibida


def pendientes(redis, nct, cuantas, monto=1):
    txs = [recibida(redis, "alice", "bob", monto) for _ in range(cuantas)]
    for tx in txs:
        nct.validar(tx)
    return txs


def cajas(redis):
    return [json.loads(caja) for caja in redis.lrange("cajas", 0, -1)]


def test_cerrar_cajas_arma_una_sola_caja_de_como_mucho_n(armar, redis):
    nct = armar(tx_por_caja=2)
    nct.cargar_genesis()
    ids = [tx["id"] for tx in pendientes(redis, nct, 5)]

    assert nct.cerrar_cajas() == 1

    assert cajas(redis) == [ids[0:2]]
    assert redis.lrange("tx:pendientes", 0, -1) == ids[2:]  # esperan al bloque siguiente


def test_mientras_hay_una_caja_lo_que_llega_espera_en_pendientes(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    primeras = [tx["id"] for tx in pendientes(redis, nct, 2)]
    nct.cerrar_cajas()
    despues = [tx["id"] for tx in pendientes(redis, nct, 3)]

    assert nct.cerrar_cajas() == 0  # BC-89: no se apila otra caja detrás de la que se mina
    assert cajas(redis) == [primeras]
    assert redis.lrange("tx:pendientes", 0, -1) == despues


def test_sin_pendientes_no_arma_cajas_vacias(armar, redis):
    nct = armar()
    nct.cargar_genesis()

    assert nct.cerrar_cajas() == 0
    assert cajas(redis) == []


def test_dos_replicas_cerrando_cajas_a_la_vez_arman_una_sola(armar, redis):
    nct = armar(tx_por_caja=30)
    nct.cargar_genesis()
    ids = [tx["id"] for tx in pendientes(redis, nct, 40)]
    replicas = [armar(tx_por_caja=30) for _ in range(4)]
    hilos = [threading.Thread(target=r.cerrar_cajas) for r in replicas]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    assert cajas(redis) == [ids[:30]]
    assert redis.lrange("tx:pendientes", 0, -1) == ids[30:]


def test_el_tic_corre_una_sola_vez_por_ventana_entre_las_replicas(armar, redis):
    uno, otro = armar(ventana=30), armar(ventana=30)
    uno.cargar_genesis()
    assert uno.tic() is True
    assert otro.tic() is False


def test_armar_publica_una_tarea_con_la_primera_caja_sobre_la_punta(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()
    txs = pendientes(redis, nct, 2, monto=7)
    nct.cerrar_cajas()

    id_tarea = nct.armar_si_libre()

    punta = redis.lindex("cadena", -1)
    [tarea] = publicador.tareas()
    assert tarea["tarea"] == id_tarea and tarea["previous_hash"] == punta and tarea["altura"] == 1
    assert tarea["transacciones"] == txs  # completas y firmadas, como van en el bloque
    assert isinstance(tarea["timestamp"], int)
    guardada = redis.hgetall(f"tarea:{id_tarea}")
    assert guardada["estado"] == "en_curso" and guardada["previous_hash"] == punta and guardada["altura"] == "1"
    assert json.loads(guardada["caja"]) == [tx["id"] for tx in txs]
    assert json.loads(guardada["transacciones"]) == tarea["transacciones"]
    assert int(guardada["timestamp"]) == tarea["timestamp"] and "publicada" in guardada
    assert redis.get("tarea:actual") == id_tarea
    # La caja sigue en la fila mientras se mina: si el NCT muere, se vuelve a armar.
    assert cajas(redis) == [[tx["id"] for tx in txs]]


def test_no_arma_otra_tarea_mientras_hay_una_en_curso(armar, redis, publicador):
    nct = armar(tx_por_caja=1)
    nct.cargar_genesis()
    pendientes(redis, nct, 2)
    nct.cerrar_cajas()

    assert nct.armar_si_libre() is not None
    assert armar(tx_por_caja=1).armar_si_libre() is None  # la otra réplica
    assert len(publicador.tareas()) == 1


def test_armar_sin_caja_cierra_una_con_lo_que_espera(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()
    txs = pendientes(redis, nct, 3)

    assert nct.armar_si_libre() is not None  # sin esperar el tic

    [tarea] = publicador.tareas()
    assert tarea["transacciones"] == txs and redis.llen("tx:pendientes") == 0


def test_sin_cajas_no_arma_y_no_se_queda_con_la_tarea_actual(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()

    assert nct.armar_si_libre() is None
    assert not redis.exists("tarea:actual") and publicador.tareas() == []


def test_dos_replicas_armando_a_la_vez_publican_una_sola_tarea(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()
    pendientes(redis, nct, 3)
    nct.cerrar_cajas()
    replicas = [armar() for _ in range(6)]
    hilos = [threading.Thread(target=r.armar_si_libre) for r in replicas]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    assert len(publicador.tareas()) == 1


def test_el_tic_cierra_cajas_y_arma_la_tarea(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()
    pendientes(redis, nct, 2)

    nct.tic()

    assert len(publicador.tareas()) == 1


def test_las_metricas_de_pendientes_y_en_cajas_salen_de_redis(armar, redis):
    nct = armar(tx_por_caja=2)
    nct.cargar_genesis()
    pendientes(redis, nct, 3)
    nct.cerrar_cajas()
    pendientes(redis, nct, 1)

    assert REGISTRY.get_sample_value("blockchain_tx_pendientes") == 2
    assert REGISTRY.get_sample_value("blockchain_tx_en_cajas") == 2
    assert REGISTRY.get_sample_value("blockchain_tx_por_bloque_maximo") == 2
