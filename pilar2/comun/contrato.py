"""Contrato entre los servicios: transacción, bloque, recompensa y nombres de colas y claves.

La especificación completa está en pilar2/CONTRATO.md.
"""

import hashlib
import re

# Colas, exchange y claves de Redis
COLAS = ("transacciones", "tareas", "rangos", "resultados", "soluciones")
EXCHANGE_EVENTOS = "eventos"            # topic
BLOQUE_CONFIRMADO = "bloque.confirmado"
CADENA, SALDOS, PENDIENTES, CAJAS, PRESTAMOS = "cadena", "saldos", "tx:pendientes", "cajas", "prestamos"
TX, BLOQUE, TAREA, NODO = "tx:{}", "bloque:{}", "tarea:{}", "nodo:{}"
ESTADOS_TX = ("recibida", "pendiente", "confirmada", "rechazada")

CEROS = "0" * 32
ORIGEN_GENESIS = "genesis"
USUARIO = re.compile(r"^[A-Za-z0-9_-]{1,32}$")  # sin ":" ni "|", que separan campos al serializar
# Los saldos van a Redis con HINCRBY (entero de 64 bits, hasta ~9,2 × 10^18): con este tope no se desborda.
MONTO_MAXIMO = 10**12

# Tarea modelo (bench): el prefijo no aparece nunca, así el minero recorre el rango entero.
BENCH = {"bloque": "bench;", "desde": 0, "hasta": 2**24 - 1, "prefijo": "f" * 32}


def md5(texto):
    return hashlib.md5(texto.encode()).hexdigest()


def usuario_valido(valor):
    """Usuarios y ids de minero: el minero cobra la recompensa en saldos y la gasta como un usuario más."""
    return isinstance(valor, str) and bool(USUARIO.match(valor))


def validar_transaccion(datos):
    """Validación de formato (la hace el backend). Devuelve la lista de errores; vacía si es válida."""
    if not isinstance(datos, dict):
        return ["el cuerpo tiene que ser un objeto JSON"]
    errores = []
    for campo in ("origen", "destino"):
        valor = datos.get(campo)
        if not usuario_valido(valor):
            errores.append(f"{campo}: de 1 a 32 letras, números, «_» o «-»")
    monto = datos.get("monto")
    if not isinstance(monto, int) or isinstance(monto, bool) or not 0 < monto <= MONTO_MAXIMO:
        errores.append(f"monto: entero entre 1 y {MONTO_MAXIMO}")
    if not errores and datos["origen"] == datos["destino"]:
        errores.append("origen y destino tienen que ser distintos")
    for campo in ("origen", "destino"):
        if datos.get(campo) == ORIGEN_GENESIS:
            errores.append(f"{campo}: «genesis» está reservado")
    return errores


def serializar_transaccion(tx):
    return f"{tx['id']}:{tx['origen']}:{tx['destino']}:{tx['monto']}:{tx['timestamp']}"


def raiz_transacciones(transacciones):
    """MD5 de las transacciones en el orden del bloque, separadas por «|»."""
    return md5("|".join(serializar_transaccion(tx) for tx in transacciones))


def texto_bloque(previous_hash, timestamp, transacciones):
    """El string que reciben los mineros: el nonce se le concatena al final, en decimal.

    Lleva la raíz de las transacciones y no las transacciones, así mide siempre 77
    caracteres (el minero CUDA acepta hasta 120) aunque el bloque tenga muchas.
    """
    return f"{previous_hash};{timestamp};{raiz_transacciones(transacciones)};"


def hash_bloque(texto, nonce):
    return md5(f"{texto}{nonce}")


def cumple(hash_hex, prefijo):
    return hash_hex.startswith(prefijo)


def recompensa(prefijo, base=10):
    """Lo que cobra el minero que encontró el nonce: más largo el prefijo, más paga."""
    return base * len(prefijo)


def bloque_genesis(saldos, timestamp):
    """Bloque 0: sin minar (nonce 0, sin prefijo); reparte los saldos iniciales."""
    transacciones = [{"id": f"genesis-{usuario}", "origen": ORIGEN_GENESIS, "destino": usuario,
                      "monto": monto, "timestamp": timestamp} for usuario, monto in sorted(saldos.items())]
    texto = texto_bloque(CEROS, timestamp, transacciones)
    return {"previous_hash": CEROS, "timestamp": timestamp, "transactions": transacciones, "nonce": 0,
            "block_hash": hash_bloque(texto, 0), "prefijo": "", "minero": "", "recompensa": 0, "altura": 0}
