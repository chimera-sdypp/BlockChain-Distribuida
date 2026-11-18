#!/usr/bin/env bash
# La ráfaga de la demo: N transferencias chiquitas (1 a 5) en ronda entre alice, bob y carol, firmadas
# con sus claves de prueba y con el nonce de cada cuenta, y sigue cada una hasta que se confirma o se
# rechaza. Reemplaza a tpi-gcp/tx-prueba.sh, que mandaba transacciones sin firma (contrato v1).
#
#   pilar3/pruebas/rafaga.sh http://<Ingress> [N]       # N = 10 por defecto
#
# alice, bob y carol son las cuentas de prueba del génesis (pilar2/comun/tests/claves.py). Usa el Python
# de pilar2 (pilar2/.venv, o PYTHON=...) porque firma con `cryptography`.
set -euo pipefail

URL=${1:?uso: rafaga.sh http://<Ingress> [cantidad]}
N=${2:-10}
PILAR2="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../pilar2" && pwd)"
PYTHON=${PYTHON:-$PILAR2/.venv/bin/python}
[[ -x $PYTHON ]] || PYTHON=python3

claves=$(mktemp -d)
trap 'rm -rf "$claves"' EXIT
cd "$PILAR2"
# Las privadas de prueba, a archivos con permisos 600 (la herramienta firma desde un archivo).
"$PYTHON" - "$claves" <<'PY'
import os, sys
from comun.tests.claves import PRIVADA
for nombre in ("alice", "bob", "carol"):
    ruta = os.path.join(sys.argv[1], f"{nombre}.clave")
    with os.fdopen(os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as archivo:
        archivo.write(PRIVADA[nombre])
PY
"$PYTHON" -m claves rafaga --url "$URL" -n "$N" "$claves/alice.clave" "$claves/bob.clave" "$claves/carol.clave"
