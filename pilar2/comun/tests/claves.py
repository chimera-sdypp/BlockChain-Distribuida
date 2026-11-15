"""Claves Ed25519 fijas para los tests y el vector de prueba: la privada de cada una es sha256("chimera-prueba-<nombre>").

Son públicas a propósito (cualquiera las puede derivar): no sirven fuera de los tests.

    from comun.tests.claves import CUENTA, PRIVADA
    contrato.transaccion(PRIVADA["alice"], CUENTA["bob"], 10, nonce=1)
"""

import hashlib

from comun import contrato

NOMBRES = ("alice", "bob", "carol", "minero", "organizador")
PRIVADA = {n: hashlib.sha256(f"chimera-prueba-{n}".encode()).hexdigest() for n in NOMBRES}
CUENTA = {n: contrato.clave_publica(p) for n, p in PRIVADA.items()}
