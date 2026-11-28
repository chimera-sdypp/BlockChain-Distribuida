"""Lo que usan las pruebas para firmar (contrato v2): el contrato y las cuentas con saldo.

Las cuentas salen de cuentas.py (BC-91): en GKE las del Secret `cuentas-prueba`, que el organizador da de alta y les
carga saldo (el génesis va vacío); en k3s, alice, bob y carol, las de prueba del génesis de pilar2/k8s/nct.yaml.
ORIGENES son sus nombres: ORIGENES[0] firma y ORIGENES[1] recibe en las pruebas de una sola cuenta. Necesita
`cryptography`: correrlas con el Python de pilar2 (pilar2/.venv/bin/python, o pip install -r pilar2/comun/requirements.txt).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pilar2"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from comun import contrato  # noqa: E402
from cuentas import CUENTA, ORIGENES, PRIVADA  # noqa: E402

__all__ = ["contrato", "CUENTA", "ORIGENES", "PRIVADA", "cuenta"]


def cuenta(nombre_o_cuenta):
    return nombre_o_cuenta if contrato.cuenta_valida(nombre_o_cuenta) else CUENTA[nombre_o_cuenta]
