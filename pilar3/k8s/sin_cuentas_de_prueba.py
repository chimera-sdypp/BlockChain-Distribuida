"""BC-91: los manifiestos de GKE no cargan saldo en el génesis ni usan una cuenta de prueba.

Lee de stdin lo que renderiza `kubectl kustomize pilar3/k8s/apps` y falla (sale con 1) si:
- algún SALDOS_INICIALES no es «{}»: la plata entra sólo por cargas firmadas del organizador;
- aparece la clave pública de una cuenta de prueba (pilar2/comun/tests/claves.py): sus privadas salen del repo.

    kubectl kustomize --load-restrictor LoadRestrictionsNone pilar3/k8s/apps | python pilar3/k8s/sin_cuentas_de_prueba.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pilar2"))

from comun.tests.claves import CUENTA, NOMBRES  # noqa: E402

# Las de NOMBRES y las que usan los tests y los scripts de pilar3 (cualquier nombre deriva una).
PRUEBA = (*NOMBRES, "zoe", "operador", "escalador", "dave", "vm0", "vm-1", "intruso")


def problemas(texto):
    encontrados = []
    for valor in re.findall(r"name: SALDOS_INICIALES\s*\n\s*value: (.*)", texto):
        if valor.strip().strip("'\"") not in ("{}", ""):
            encontrados.append(f"SALDOS_INICIALES con cuentas: {valor.strip()[:80]}…")
    for nombre in PRUEBA:
        if CUENTA[nombre] in texto:
            encontrados.append(f"la cuenta de prueba «{nombre}» ({CUENTA[nombre][:12]}…)")
    return encontrados


if __name__ == "__main__":
    encontrados = problemas(sys.stdin.read())
    for problema in encontrados:
        print(f"::error::GKE: {problema}. En pilar3/ el génesis va vacío y sin claves de comun/tests/claves.py (BC-91).")
    sys.exit(1 if encontrados else 0)
