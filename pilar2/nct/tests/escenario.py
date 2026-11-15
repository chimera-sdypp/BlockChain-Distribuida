"""Lo que usan los tests del NCT: transacciones recibidas, una tarea en curso y un minero que la resuelve."""

import json

from comun import contrato
from comun.tests.claves import CUENTA, PRIVADA, Billetera, cuenta

BILLETERA = Billetera()  # la vacía conftest.py antes de cada test


def guardar_recibida(redis, tx):
    """Lo que dejan el backend en Redis y en la cola `transacciones`."""
    redis.hset(f"tx:{tx['id']}", mapping={**{k: v for k, v in tx.items() if k != "id"}, "estado": "recibida"})
    return tx


def recibida(redis, origen, destino, monto, nonce=None):
    """Una transferencia firmada por la cuenta de prueba `origen`; el nonce sigue al anterior si no se pasa."""
    if nonce is None:
        tx = BILLETERA(origen, destino, monto)
    else:
        tx = contrato.transaccion(PRIVADA[origen], cuenta(destino), monto, nonce, 1793836800)
    return guardar_recibida(redis, tx)


def saldo(redis, nombre):
    return int(redis.hget("saldos", cuenta(nombre)) or 0)


def tarea_en_curso(redis, nct, prefijo="0", transferencias=(("alice", "bob", 30), ("bob", "carol", 20))):
    """Génesis, transacciones validadas, caja y tarea publicada; y el prefijo que elegiría el TrP."""
    nct.cargar_genesis()
    for origen, destino, monto in transferencias:
        nct.validar(recibida(redis, origen, destino, monto))
    nct.cerrar_cajas()
    id_tarea = nct.armar_si_libre()
    redis.hset(f"tarea:{id_tarea}", "prefijo", prefijo)
    return id_tarea


def datos(redis, id_tarea):
    tarea = redis.hgetall(f"tarea:{id_tarea}")
    return {"previous_hash": tarea["previous_hash"], "timestamp": int(tarea["timestamp"]),
            "altura": int(tarea["altura"]), "prefijo": tarea["prefijo"],
            "transacciones": json.loads(tarea["transacciones"])}


def minar(redis, id_tarea, desde=0, minero=None, texto=None):
    """Lo que haría un minero: el primer nonce desde `desde` cuyo hash, sobre su propio texto, cumple el prefijo."""
    minero = minero or CUENTA["minero"]
    tarea = datos(redis, id_tarea)
    texto = texto or contrato.texto_para(tarea, minero)
    nonce = next(n for n in range(desde, desde + 10**7) if contrato.hash_bloque(texto, n).startswith(tarea["prefijo"]))
    return {"tarea": id_tarea, "nonce": nonce, "hash": contrato.hash_bloque(texto, nonce),
            "prefijo": tarea["prefijo"], "minero": minero}
