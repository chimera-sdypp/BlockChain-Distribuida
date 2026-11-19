"""Ticket 01: el génesis se carga una sola vez, con sus cuentas aprobadas."""

import json
import threading

from comun import contrato
from comun.tests.claves import CUENTA
from nct import app as nct_app


def test_con_la_cadena_vacia_carga_el_genesis_con_los_saldos_iniciales(armar, redis):
    armar().cargar_genesis()

    [hash_] = redis.lrange("cadena", 0, -1)
    bloque = redis.hgetall(f"bloque:{hash_}")
    assert bloque["previous_hash"] == contrato.CEROS
    assert bloque["altura"] == "0"
    assert redis.hgetall("saldos") == {CUENTA["alice"]: "100", CUENTA["bob"]: "50", CUENTA["carol"]: "0"}
    ids = [tx["id"] for tx in json.loads(bloque["transactions"])]
    # v3: primero el organizador (los que verifican lo leen de acá) y después las cuentas de prueba
    assert ids == [f"organizador-{CUENTA['organizador']}"] + sorted(f"genesis-{CUENTA[n]}" for n in ("alice", "bob", "carol"))
    texto = contrato._texto(contrato.CEROS, int(bloque["timestamp"]), json.loads(bloque["transactions"]),
                            contrato.RAIZ_VACIA)
    assert contrato.hash_bloque(texto, 0) == hash_ and bloque["raiz_estado"] == contrato.RAIZ_VACIA
    assert json.loads(redis.hget("cuentas", CUENTA["organizador"]))["tipo"] == "organizador"


def test_las_cuentas_del_genesis_quedan_aprobadas_como_usuarios(armar, redis):
    armar(saldos_iniciales={CUENTA["alice"]: {"saldo": 5, "nombre": "alice"}, CUENTA["bob"]: 7}).cargar_genesis()

    alice = json.loads(redis.hget("cuentas", CUENTA["alice"]))
    bob = json.loads(redis.hget("cuentas", CUENTA["bob"]))
    assert (alice["tipo"], alice["nombre"], bob["tipo"], bob["nombre"]) == ("usuario", "alice", "usuario", "")
    assert redis.hgetall("saldos") == {CUENTA["alice"]: "5", CUENTA["bob"]: "7"}


def test_sin_saldos_iniciales_el_genesis_trae_solo_al_organizador(armar, redis):
    armar(saldos_iniciales={}).cargar_genesis()
    assert redis.llen("cadena") == 1 and not redis.exists("saldos")
    assert list(redis.hgetall("cuentas")) == [CUENTA["organizador"]]


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
    assert redis.hget("saldos", CUENTA["alice"]) == "100"


def test_saldos_iniciales_del_entorno():
    texto = json.dumps({CUENTA["alice"]: 1000, CUENTA["bob"]: {"saldo": 5, "nombre": "bob"}})
    assert nct_app.leer_saldos_iniciales(texto) == {CUENTA["alice"]: 1000, CUENTA["bob"]: {"saldo": 5, "nombre": "bob"}}
    assert nct_app.leer_saldos_iniciales("") == {}
    for malo in ('{"alice": 1000}', json.dumps({CUENTA["alice"]: -1}), json.dumps({CUENTA["alice"]: 1.5}), "[]"):
        try:
            nct_app.leer_saldos_iniciales(malo)
        except ValueError:
            continue
        raise AssertionError(f"aceptó {malo}")
