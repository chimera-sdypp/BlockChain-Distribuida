"""Lo que usan las pruebas para firmar (contrato v2): el contrato y las cuentas de prueba del génesis.

alice, bob y carol son las cuentas de prueba (pilar2/comun/tests/claves.py): sus privadas salen de
sha256("chimera-prueba-<nombre>") y el NCT las carga con saldo en el génesis (pilar2/k8s/nct.yaml y,
en GKE, pilar3/k8s/apps/nct-saldos.yaml). Las pruebas firman con ellas. Necesita `cryptography`:
correrlas con el Python de pilar2 (pilar2/.venv/bin/python, o pip install -r pilar2/comun/requirements.txt).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pilar2"))

from comun import contrato  # noqa: E402
from comun.tests.claves import CUENTA, PRIVADA  # noqa: E402

__all__ = ["contrato", "CUENTA", "PRIVADA", "cuenta"]


def cuenta(nombre_o_cuenta):
    return nombre_o_cuenta if contrato.cuenta_valida(nombre_o_cuenta) else CUENTA[nombre_o_cuenta]
