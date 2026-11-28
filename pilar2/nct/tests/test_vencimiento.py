"""BC-94: una transacción rechazada vence. Antes quedaba en tx:<id> para siempre y, con lotes de mil, llenaba Redis."""

import json
import time

from comun import contrato
from comun.tests.claves import CUENTA, FECHA, PRIVADA, Billetera, carga, cadena_actual
from nct.tests.escenario import confirmar, guardar_recibida, recibida

GUARDA = 600


def vence(redis, tx):
    """Que tx:<id> esté rechazada y venza en a lo sumo GUARDA segundos."""
    clave = f"tx:{tx['id']}"
    return redis.hget(clave, "estado") == "rechazada" and 0 < redis.ttl(clave) <= GUARDA


def test_una_transferencia_de_una_cuenta_no_aprobada_vence(armar, redis):
    nct = armar(rechazada_guarda=GUARDA)
    nct.cargar_genesis()
    redis.hset("saldos", CUENTA["zoe"], 100)
    tx = recibida(redis, "zoe", "bob", 1)

    nct.validar(tx)

    assert vence(redis, tx)


def test_una_que_espera_un_nonce_que_no_llega_vence_al_rechazarse(armar, redis):
    nct = armar(rechazada_guarda=GUARDA, espera_nonce=60)
    nct.cargar_genesis()
    recibida(redis, "alice", "bob", 10)  # el nonce 1 nunca llega al NCT
    salteada = recibida(redis, "alice", "bob", 20)
    nct.validar(salteada)

    nct.vencer_espera(ahora=time.time() + 61)

    assert vence(redis, salteada)


def test_una_que_sale_de_la_caja_por_no_valer_contra_la_cadena_vence(armar, redis):
    """Una cuenta aprobada a mano en Redis: el NCT la valida con el caché, pero al armar el bloque la cadena dice que no."""
    nct = armar(rechazada_guarda=GUARDA)
    nct.cargar_genesis()
    confirmar(redis, nct, [carga(1, "zoe", 50)])
    redis.hset("cuentas", CUENTA["zoe"], json.dumps({"tipo": "usuario", "nombre": "zoe", "alta": FECHA}))
    trucha = contrato.transaccion(PRIVADA["zoe"], CUENTA["bob"], 10, 1, FECHA, cadena=cadena_actual())
    assert nct.validar(guardar_recibida(redis, trucha)) == "pendiente"

    confirmar(redis, nct, [Billetera()("alice", "bob", 5)])

    assert vence(redis, trucha)


def test_las_que_siguen_su_camino_no_vencen(armar, redis):
    nct = armar(rechazada_guarda=GUARDA)
    nct.cargar_genesis()
    tx = recibida(redis, "alice", "bob", 10)

    assert nct.validar(tx) == "pendiente"
    assert redis.ttl(f"tx:{tx['id']}") == -1
