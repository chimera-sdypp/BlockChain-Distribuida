"""Un escenario armado para los tests de soluciones: una tarea en curso, lista para minar."""

import json

from comun import contrato
from nct.tests.test_validar import recibida


def tarea_en_curso(redis, nct, prefijo="0", transferencias=(("alice", "bob", 30), ("bob", "carol", 20))):
    """Génesis, transacciones validadas, caja y tarea publicada; y el prefijo que elegiría el TrP."""
    nct.cargar_genesis()
    for origen, destino, monto in transferencias:
        nct.validar(recibida(redis, origen, destino, monto))
    nct.cerrar_cajas()
    id_tarea = nct.armar_si_libre()
    redis.hset(f"tarea:{id_tarea}", "prefijo", prefijo)
    return id_tarea


def minar(redis, id_tarea, desde=0, minero="gpu-1"):
    """Lo que haría un minero: el primer nonce desde `desde` cuyo hash cumple el prefijo de la tarea."""
    tarea = redis.hgetall(f"tarea:{id_tarea}")
    texto = contrato.texto_bloque(tarea["previous_hash"], int(tarea["timestamp"]), json.loads(tarea["transacciones"]))
    nonce = next(n for n in range(desde, desde + 10**7) if contrato.hash_bloque(texto, n).startswith(tarea["prefijo"]))
    return {"tarea": id_tarea, "nonce": nonce, "hash": contrato.hash_bloque(texto, nonce),
            "prefijo": tarea["prefijo"], "minero": minero}
