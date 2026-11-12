"""Ticket 02 (BC-24): validar transacciones contra el saldo menos lo comprometido."""

import threading
import uuid

import pytest
from redis.exceptions import ConnectionError


def recibida(redis, origen, destino, monto):
    """Lo que dejan el backend en Redis y en la cola `transacciones`."""
    id_ = uuid.uuid4().hex
    tx = {"id": id_, "origen": origen, "destino": destino, "monto": monto, "timestamp": 1793836800}
    redis.hset(f"tx:{id_}", mapping={**{k: v for k, v in tx.items() if k != "id"}, "estado": "recibida"})
    return tx


def test_con_el_saldo_justo_queda_pendiente_y_se_avisa(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()
    tx = recibida(redis, "alice", "bob", 100)

    nct.validar(tx)

    assert redis.hget(f"tx:{tx['id']}", "estado") == "pendiente"
    assert redis.lrange("tx:pendientes", 0, -1) == [tx["id"]]
    assert redis.hget("comprometido", "alice") == "100"
    assert publicador.avisos == [("transaccion.estado", {"id": tx["id"], "estado": "pendiente"})]


def test_sin_saldo_queda_rechazada_con_el_motivo(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()
    tx = recibida(redis, "carol", "bob", 1)

    nct.validar(tx)

    assert redis.hget(f"tx:{tx['id']}", "estado") == "rechazada"
    assert redis.hget(f"tx:{tx['id']}", "motivo") == "saldo insuficiente"
    assert redis.llen("tx:pendientes") == 0 and not redis.hget("comprometido", "carol")
    assert publicador.avisos == [("transaccion.estado",
                                  {"id": tx["id"], "estado": "rechazada", "motivo": "saldo insuficiente"})]


def test_un_usuario_que_no_esta_en_los_saldos_no_tiene_saldo(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    tx = recibida(redis, "zoe", "bob", 1)
    nct.validar(tx)
    assert redis.hget(f"tx:{tx['id']}", "estado") == "rechazada"


def test_descuenta_lo_que_ya_esta_comprometido(armar, redis):
    nct = armar()
    nct.cargar_genesis()
    primera, segunda = recibida(redis, "alice", "bob", 60), recibida(redis, "alice", "carol", 50)

    nct.validar(primera)
    nct.validar(segunda)

    assert redis.hget(f"tx:{primera['id']}", "estado") == "pendiente"
    assert redis.hget(f"tx:{segunda['id']}", "estado") == "rechazada"
    assert redis.hget("comprometido", "alice") == "60"


def test_un_mensaje_reentregado_no_se_cuenta_dos_veces(armar, redis, publicador):
    nct = armar()
    nct.cargar_genesis()
    tx = recibida(redis, "alice", "bob", 60)

    nct.validar(tx)
    nct.validar(tx)  # el pod murió antes del ack y RabbitMQ lo volvió a entregar

    assert redis.hget("comprometido", "alice") == "60"
    assert redis.lrange("tx:pendientes", 0, -1) == [tx["id"]]
    # El aviso se repite: si la primera vez falló avisar, ésta es la que llega a la página.
    assert publicador.avisos == [("transaccion.estado", {"id": tx["id"], "estado": "pendiente"})] * 2


def test_una_transaccion_que_no_esta_en_redis_vuelve_a_la_cola(armar, redis, publicador):
    # Sólo pasa si se le habla a un Redis equivocado (un failover): error transitorio, no se descarta.
    nct = armar()
    nct.cargar_genesis()
    with pytest.raises(ConnectionError):
        nct.validar({"id": "fantasma", "origen": "alice", "destino": "bob", "monto": 1, "timestamp": 1})
    assert not redis.exists("tx:fantasma") and publicador.avisos == []


def test_dos_replicas_validando_a_la_vez_no_gastan_dos_veces_el_mismo_saldo(armar, redis):
    armar().cargar_genesis()
    txs = [recibida(redis, "alice", "bob", 10) for _ in range(20)]  # 200 contra un saldo de 100
    replicas = [armar(), armar()]
    hilos = [threading.Thread(target=replicas[i % 2].validar, args=(tx,)) for i, tx in enumerate(txs)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    estados = [redis.hget(f"tx:{tx['id']}", "estado") for tx in txs]
    assert estados.count("pendiente") == 10 and estados.count("rechazada") == 10
    assert redis.hget("comprometido", "alice") == "100"
