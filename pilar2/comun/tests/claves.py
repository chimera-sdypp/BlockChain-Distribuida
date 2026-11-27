"""Claves Ed25519 fijas para los tests y el vector de prueba: la privada de cada una es sha256("chimera-prueba-<nombre>").

Son públicas a propósito (cualquiera las puede derivar): no sirven fuera de los tests. Cualquier
nombre sirve: `CUENTA["zoe"]` deriva la de zoe la primera vez que se pide.

    from comun.tests.claves import CUENTA, PRIVADA, firmada
    firmada("alice", "bob", 10, nonce=1)   # contrato.transaccion con las claves de prueba

v4: lo firmado lleva el hash del génesis. Por defecto es el de la cadena de prueba (`CADENA`: el génesis de
comun/tests/cadena.py con alice 100 y bob 50); para otra cadena, `cadena=<hash del génesis>`.
"""

import hashlib

from comun import contrato

NOMBRES = ("alice", "bob", "carol", "minero", "organizador")
FECHA = 1793836800  # 2026-11-05: el timestamp fijo de los tests


class _Derivadas(dict):
    """Un dict que deriva el valor de un nombre nuevo la primera vez que se lo pide."""

    def __init__(self, derivar):
        super().__init__()
        self._derivar = derivar

    def __missing__(self, nombre):
        self[nombre] = valor = self._derivar(nombre)
        return valor


PRIVADA = _Derivadas(lambda nombre: hashlib.sha256(f"chimera-prueba-{nombre}".encode()).hexdigest())
CUENTA = _Derivadas(lambda nombre: contrato.clave_publica(PRIVADA[nombre]))
for _nombre in NOMBRES:
    CUENTA[_nombre]


# El génesis de la cadena de prueba por defecto (comun/tests/cadena.py), y su hash: el id con que se firma.
GENESIS = contrato.bloque_genesis({CUENTA["alice"]: 100, CUENTA["bob"]: 50}, FECHA, organizador=CUENTA["organizador"],
                                  nombres={CUENTA["alice"]: "alice", CUENTA["bob"]: "bob"})
CADENA = GENESIS["block_hash"]
_ACTUAL = [lambda: CADENA]


def cadena_actual():
    """La cadena con que firman los ayudantes si no se les pasa otra: CADENA, o la que fijó `usar_cadena`."""
    return _ACTUAL[0]()


def usar_cadena(leer=None):
    """Los tests con Redis: `leer()` da el génesis que cargó el NCT (cada test tiene el suyo). Sin nada, CADENA."""
    _ACTUAL[0] = leer or (lambda: CADENA)


def cuenta(nombre_o_cuenta):
    """La cuenta de un nombre de prueba; si ya es una cuenta (64 hex), ella misma."""
    return nombre_o_cuenta if contrato.cuenta_valida(nombre_o_cuenta) else CUENTA[nombre_o_cuenta]


def firmada(origen, destino, monto, nonce=1, timestamp=FECHA, cadena=None):
    """Una transferencia firmada por la cuenta de prueba `origen` (un nombre)."""
    return contrato.transaccion(PRIVADA[origen], cuenta(destino), monto, nonce, timestamp, cadena=cadena or cadena_actual())


def carga(numero, destino, monto, timestamp=FECHA, cadena=None):
    """Una carga de saldo firmada por el organizador de prueba."""
    return contrato.carga(PRIVADA["organizador"], numero, cuenta(destino), monto, timestamp, cadena=cadena or cadena_actual())


class Billetera:
    """Firma transferencias llevando el nonce de cada origen, como un cliente: 1, 2, 3…"""

    def __init__(self):
        self.nonces = {}

    def __call__(self, origen, destino, monto, timestamp=FECHA, cadena=None):
        self.nonces[origen] = self.nonces.get(origen, 0) + 1
        return firmada(origen, destino, monto, self.nonces[origen], timestamp, cadena)


def sembrar_genesis(redis):
    """Deja en Redis el génesis de la cadena de prueba (CADENA), como si lo hubiera cargado el NCT: así el backend y el
    gateway verifican contra él (v4). Sólo si la cadena está vacía."""
    import json
    if redis.llen("cadena") == 0:
        redis.hset(f"bloque:{CADENA}", mapping={**GENESIS, "transactions": json.dumps(GENESIS["transactions"])})
        redis.rpush("cadena", CADENA)
