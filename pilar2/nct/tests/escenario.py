"""Lo que usan los tests del NCT: transacciones recibidas, una tarea en curso y un minero que la resuelve."""

import json

from comun import contrato
from comun.tests.claves import CUENTA, PRIVADA, Billetera, cuenta, cadena_actual

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
        tx = contrato.transaccion(PRIVADA[origen], cuenta(destino), monto, nonce, 1793836800, cadena=cadena_actual())
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
            "transacciones": json.loads(tarea["transacciones"]), "raiz_estado": tarea["raiz_estado"]}


def minar(redis, id_tarea, desde=0, minero=None, texto=None):
    """Lo que haría un minero: el primer nonce desde `desde` cuyo hash, sobre su propio texto, cumple el prefijo."""
    minero = minero or CUENTA["minero"]
    tarea = datos(redis, id_tarea)
    texto = texto or contrato.texto_para(tarea, minero)
    nonce = next(n for n in range(desde, desde + 10**7) if contrato.hash_bloque(texto, n).startswith(tarea["prefijo"]))
    return {"tarea": id_tarea, "nonce": nonce, "hash": contrato.hash_bloque(texto, nonce),
            "prefijo": tarea["prefijo"], "minero": minero}


# ── Cadenas de verdad (v3): el NCT verifica lo que sigue, así que los bloques de relleno también se minan ──

def confirmar(redis, nct, transacciones, minero=None, prefijo="0"):
    """Valida `transacciones` (ya firmadas), arma la caja y mina su bloque. Devuelve el hash del bloque."""
    for tx in transacciones:
        nct.validar(guardar_recibida(redis, tx))
    nct.cerrar_cajas()
    id_tarea = redis.get("tarea:actual") or nct.armar_si_libre()
    redis.hset(f"tarea:{id_tarea}", "prefijo", prefijo)
    solucion = minar(redis, id_tarea, minero=minero)
    assert nct.procesar_solucion(solucion) == "confirmada"
    return solucion["hash"]


_RELLENO = {"cargas": 0}


def rellenar(redis, nct, altura, primera_carga=None):
    """Génesis y bloques minados de verdad (una carga de 1 a «relleno» en cada uno) hasta que la tarea siguiente
    sea la de `altura`. Las cargas siguen el nonce de «emision» que tiene Redis."""
    from comun.tests.claves import carga
    nct.cargar_genesis()
    while redis.llen("cadena") < altura:
        numero = int(redis.hget("nonces", "emision") or 0) + 1
        confirmar(redis, nct, [carga(numero, "relleno", 1)], minero=cuenta("minero-relleno"))


def punta_timestamp(redis):
    return int(redis.hget(f"bloque:{redis.lindex('cadena', -1)}", "timestamp"))


def bloque_ajeno(redis, minero="otro"):
    """Un bloque válido con sólo la coinbase sobre la punta, guardado como lo haría la otra réplica del NCT."""
    from comun import estado as estado_cadena
    seguidor = estado_cadena.Seguidor(redis, estado_cadena.Reglas(verificar_firmas=False), snapshot_cada=0)
    seguidor.arrancar()
    actual = seguidor.estado
    tarea = {"previous_hash": actual.punta, "timestamp": actual.timestamp + 1, "altura": actual.altura + 1,
             "prefijo": "0", "transacciones": [], "raiz_estado": actual.raiz()}
    texto = contrato.texto_para(tarea, cuenta(minero))
    nonce = next(n for n in range(10**6) if contrato.hash_bloque(texto, n).startswith("0"))
    hash_ = contrato.hash_bloque(texto, nonce)
    transacciones = contrato.transacciones_del_bloque(tarea, cuenta(minero))
    redis.hset(f"bloque:{hash_}", mapping={"previous_hash": actual.punta, "timestamp": tarea["timestamp"],
                                           "transactions": json.dumps(transacciones), "nonce": nonce,
                                           "block_hash": hash_, "prefijo": "0", "altura": tarea["altura"],
                                           "raiz_estado": tarea["raiz_estado"]})
    redis.rpush("cadena", hash_)
    redis.hincrby("saldos", cuenta(minero), transacciones[0]["monto"])
    return hash_
