#!/usr/bin/env bash
# Una ráfaga: N transferencias chiquitas (1 a 5) en ronda entre las cuentas de cuentas.py, firmadas
# con sus claves y con el nonce de cada cuenta, y sigue cada una hasta que se confirma o se
# rechaza. Reemplaza a tpi-gcp/tx-prueba.sh, que mandaba transacciones sin firma (contrato v1).
#
#   pilar3/pruebas/rafaga.sh http://<Ingress> [N]       # N = 10 por defecto
#
# Las cuentas salen de cuentas.py (BC-91): en GKE las del Secret cuentas-prueba, en k3s alice, bob y carol. Usa el Python
# de pilar2 (pilar2/.venv, o PYTHON=...) porque firma con `cryptography`.
set -euo pipefail

URL=${1:?uso: rafaga.sh http://<Ingress> [cantidad]}
N=${2:-10}
PILAR2="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../pilar2" && pwd)"
PYTHON=${PYTHON:-$PILAR2/.venv/bin/python}
[[ -x $PYTHON ]] || PYTHON=python3

PRUEBAS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

claves=$(mktemp -d)
trap 'rm -rf "$claves"' EXIT
cd "$PILAR2"
# Las privadas, a archivos con permisos 600 (la herramienta firma desde un archivo).
"$PYTHON" - "$claves" "$PRUEBAS" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[2])
from cuentas import ORIGENES, PRIVADA
for nombre in ORIGENES:
    ruta = os.path.join(sys.argv[1], f"{nombre}.clave")
    with os.fdopen(os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as archivo:
        archivo.write(PRIVADA[nombre])
PY
"$PYTHON" -m claves rafaga --url "$URL" -n "$N" "$claves"/*.clave
