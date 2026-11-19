"""Ticket 02 (BC-24 y BC-56): validar la firma, la cuenta, el nonce y el saldo de cada transacción."""

import random
import threading
import time

import pytest
from redis.exceptions import ConnectionError

from comun import contrato
from comun.tests.claves import CUENTA, PRIVADA, carga
from nct.tests.escenario import confirmar, guardar_recibida, recibida


def estado(redis, tx):
    return redis.hget(f"tx:{tx['id']}", "estado")


def motivo(redis, tx):
    return redis.hget(f"tx:{tx['id']}", "motivo")


def test_con_el_saldo_justo_queda_pendiente_y_se_avisa(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()
    tx = recibida(redis, "alice", "bob", 100)

    assert nct.validar(tx) == "pendiente"

    assert estado(redis, tx) == "pendiente"
    assert redis.lrange("tx:pendientes", 0, -1) == [tx["id"]]
    assert redis.hget("comprometido", CUENTA["alice"]) == "100"
    assert redis.hget("nonces", CUENTA["alice"]) == "1"
    assert publicador.avisos == [("transaccion.estado", {"id": tx["id"], "estado": "pendiente"})]


def test_sin_saldo_queda_rechazada_con_el_motivo_y_no_gasta_el_nonce(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()
    tx = recibida(redis, "carol", "bob", 1)

    nct.validar(tx)

    assert estado(redis, tx) == "rechazada" and motivo(redis, tx) == "saldo insuficiente"
    assert redis.llen("tx:pendientes") == 0 and not redis.hget("comprometido", CUENTA["carol"])
    assert not redis.hget("nonces", CUENTA["carol"])
    assert publicador.avisos == [("transaccion.estado",
                                  {"id": tx["id"], "estado": "rechazada", "motivo": "saldo insuficiente"})]


def test_descuenta_lo_que_ya_esta_comprometido(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    primera, segunda = recibida(redis, "alice", "bob", 60), recibida(redis, "alice", "carol", 50)

    nct.validar(primera)
    nct.validar(segunda)

    assert estado(redis, primera) == "pendiente"
    assert estado(redis, segunda) == "rechazada"
    assert redis.hget("comprometido", CUENTA["alice"]) == "60"


# ── Las cuatro trampas de BC-56 ──

def test_una_firma_falsa_se_rechaza(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    tx = recibida(redis, "alice", "bob", 10)
    redis.hset(f"tx:{tx['id']}", "firma", "ab" * 64)

    nct.validar(tx)

    assert (estado(redis, tx), motivo(redis, tx)) == ("rechazada", "firma inválida")
    assert not redis.hget("nonces", CUENTA["alice"])


def test_un_monto_cambiado_despues_de_firmar_se_rechaza(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    tx = recibida(redis, "alice", "bob", 10)
    redis.hset(f"tx:{tx['id']}", "monto", 99)

    nct.validar(tx)

    assert (estado(redis, tx), motivo(redis, tx)) == ("rechazada", "firma inválida")


def test_un_nonce_repetido_se_rechaza(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    primera = recibida(redis, "alice", "bob", 10, nonce=1)
    otra = recibida(redis, "alice", "carol", 5, nonce=1)  # otra transacción firmada con el mismo nonce

    nct.validar(primera)
    nct.validar(otra)

    assert estado(redis, primera) == "pendiente"
    assert (estado(redis, otra), motivo(redis, otra)) == ("rechazada", "nonce repetido")
    assert redis.hget("comprometido", CUENTA["alice"]) == "10"


def test_una_cuenta_no_aprobada_no_puede_transferir(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    redis.hset("saldos", CUENTA["zoe"], 100)  # tiene saldo, pero el organizador no la aprobó
    tx = recibida(redis, "zoe", "bob", 1)

    nct.validar(tx)

    assert (estado(redis, tx), motivo(redis, tx)) == ("rechazada", "cuenta no aprobada")


def test_un_usuario_dado_de_baja_no_puede_transferir(armar, redis):
    """BC-58 y BC-79: la baja (en la cadena) saca la cuenta de `cuentas`; el saldo queda, pero ya no puede operar."""
    from comun.tests.claves import PRIVADA
    nct = armar()
    nct.cargar_genesis()
    confirmar(redis, nct, [contrato.baja(PRIVADA["organizador"], 1, CUENTA["alice"], "se fue", 1793836800)])
    assert not redis.hexists("cuentas", CUENTA["alice"])
    tx = recibida(redis, "alice", "bob", 1)

    nct.validar(tx)

    assert (estado(redis, tx), motivo(redis, tx)) == ("rechazada", "cuenta no aprobada")
    assert redis.hget("saldos", CUENTA["alice"]) is not None


# ── Nonces que llegan desordenados ──

def test_un_nonce_adelantado_espera_al_anterior_y_entran_los_dos_en_orden(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()
    primera, segunda = recibida(redis, "alice", "bob", 10), recibida(redis, "alice", "bob", 20)

    assert nct.validar(segunda) == "en_espera"
    assert estado(redis, segunda) == "recibida" and publicador.avisos == []

    assert nct.validar(primera) == "pendiente"

    assert estado(redis, primera) == estado(redis, segunda) == "pendiente"
    assert redis.lrange("tx:pendientes", 0, -1) == [primera["id"], segunda["id"]]
    assert redis.hget("nonces", CUENTA["alice"]) == "2" and redis.hget("comprometido", CUENTA["alice"]) == "30"
    assert [aviso["id"] for _, aviso in publicador.avisos] == [primera["id"], segunda["id"]]
    assert not redis.exists("espera:" + CUENTA["alice"], "tx:en_espera")


def test_si_el_nonce_que_falta_no_llega_la_que_espera_se_rechaza(armar, redis, publicador):
    nct = armar(espera_nonce=60)
    nct.cargar_genesis()
    recibida(redis, "alice", "bob", 10)  # el nonce 1 nunca llega al NCT
    salteada = recibida(redis, "alice", "bob", 20)
    nct.validar(salteada)

    assert nct.vencer_espera() == []  # todavía está a tiempo
    assert nct.vencer_espera(ahora=time.time() + 61) == [salteada["id"]]

    assert (estado(redis, salteada), motivo(redis, salteada)) == ("rechazada", "nonce salteado")
    assert publicador.avisos == [("transaccion.estado",
                                  {"id": salteada["id"], "estado": "rechazada", "motivo": "nonce salteado"})]


def test_un_mensaje_reentregado_no_se_cuenta_dos_veces(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()
    tx = recibida(redis, "alice", "bob", 60)

    nct.validar(tx)
    nct.validar(tx)  # el pod murió antes del ack y RabbitMQ lo volvió a entregar

    assert redis.hget("comprometido", CUENTA["alice"]) == "60"
    assert redis.lrange("tx:pendientes", 0, -1) == [tx["id"]]
    # El aviso se repite: si la primera vez falló avisar, ésta es la que llega a la página.
    assert publicador.avisos == [("transaccion.estado", {"id": tx["id"], "estado": "pendiente"})] * 2


def test_una_transaccion_que_no_esta_en_redis_vuelve_a_la_cola(armar, redis, publicador):
    # Sólo pasa si se le habla a un Redis equivocado (un failover): error transitorio, no se descarta.
    nct = armar()
    nct.cargar_genesis()
    with pytest.raises(ConnectionError):
        nct.validar({"id": "fantasma"})
    assert not redis.exists("tx:fantasma") and publicador.avisos == []


def test_dos_replicas_validando_desordenadas_aceptan_los_nonces_en_orden_hasta_el_saldo(armar, redis):
    armar().cargar_genesis()
    txs = [recibida(redis, "alice", "bob", 10) for _ in range(20)]  # 200 contra un saldo de 100
    desordenadas = random.Random(7).sample(txs, len(txs))
    replicas = [armar(), armar()]
    hilos = [threading.Thread(target=replicas[i % 2].validar, args=(tx,)) for i, tx in enumerate(desordenadas)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    # Entran los nonces 1 a 10, en orden; el 11 no tiene saldo y los siguientes esperan un 11 que no llega.
    assert redis.lrange("tx:pendientes", 0, -1) == [tx["id"] for tx in txs[:10]]
    assert redis.hget("comprometido", CUENTA["alice"]) == "100"
    assert (estado(redis, txs[10]), motivo(redis, txs[10])) == ("rechazada", "saldo insuficiente")
    assert {estado(redis, tx) for tx in txs[11:]} == {"recibida"}


def test_dos_transacciones_con_el_mismo_nonce_a_la_vez_entra_una(armar, redis):
    armar().cargar_genesis()
    gastos = [recibida(redis, "alice", destino, 80, nonce=1) for destino in ("bob", "carol")]
    replicas, barrera = [armar(), armar()], threading.Barrier(2)

    def validar(nct, tx):
        barrera.wait()
        nct.validar(tx)

    hilos = [threading.Thread(target=validar, args=par) for par in zip(replicas, gastos)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    assert sorted(motivo(redis, tx) or estado(redis, tx) for tx in gastos) == ["nonce repetido", "pendiente"]


# ── Cargas del organizador (el modelo exchange: la plata entra por cargas firmadas) ──

def test_una_carga_del_organizador_no_mira_saldo_y_sube_el_nonce_de_emision(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    tx = guardar_recibida(redis, carga(1, "zoe", 5000))

    assert nct.validar(tx) == "pendiente"

    assert redis.hget("nonces", "emision") == "1" and not redis.hget("comprometido", "emision")
    assert redis.lrange("tx:pendientes", 0, -1) == ["carga-1"]


def test_una_carga_firmada_por_otro_se_rechaza(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    falsa = guardar_recibida(redis, contrato.carga(PRIVADA["alice"], 1, CUENTA["alice"], 5000, 1793836800))

    nct.validar(falsa)

    assert (estado(redis, falsa), motivo(redis, falsa)) == ("rechazada", "firma inválida")


def test_sin_organizador_no_entra_ninguna_carga(armar, redis):
    nct = armar(organizador="")
    nct.cargar_genesis()
    tx = guardar_recibida(redis, carga(1, "zoe", 5000))
    nct.validar(tx)
    assert motivo(redis, tx) == "firma inválida"


def test_las_cargas_van_en_orden_como_los_nonces(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    segunda, primera = guardar_recibida(redis, carga(2, "zoe", 7)), guardar_recibida(redis, carga(1, "zoe", 5))

    assert nct.validar(segunda) == "en_espera"
    nct.validar(primera)

    assert redis.lrange("tx:pendientes", 0, -1) == ["carga-1", "carga-2"]
