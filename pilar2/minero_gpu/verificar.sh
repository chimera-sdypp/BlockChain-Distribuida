#!/bin/bash
# Muestra los mineros que el gateway tiene registrados (nodo:<id>, vía la API del backend) y
# termina con código 0 si hay al menos MINIMO mineros GPU vivos (por defecto 1).
#
#   BACKEND_URL=http://<IP de Traefik> pilar2/minero_gpu/verificar.sh [MINIMO]
#
# Con MINIMO=0 sirve para comprobar el apagado: termina con 0 si NO queda ningún GPU vivo.

set -euo pipefail

MINIMO="${1:-1}"
[[ "$MINIMO" =~ ^[0-9]+$ ]] || { echo "Error: MINIMO tiene que ser un entero >= 0" >&2; exit 1; }
[[ -n "${BACKEND_URL:-}" ]] || { echo "Error: falta BACKEND_URL (p. ej. http://<IP del Ingress>)" >&2; exit 1; }
PYTHON="$(command -v python3 || command -v python)" || { echo "Error: hace falta Python" >&2; exit 1; }

MINEROS_JSON="$(curl -fsS --max-time 10 "${BACKEND_URL%/}/api/mineros")" \
    || { echo "Error: no respondió ${BACKEND_URL%/}/api/mineros" >&2; exit 1; }
export MINEROS_JSON

"$PYTHON" - "$MINIMO" << 'EOF'
import json
import os
import sys

minimo = int(sys.argv[1])
mineros = json.loads(os.environ["MINEROS_JSON"])


def mhs(valor):
    return f"{valor / 1e6:,.1f}" if isinstance(valor, (int, float)) else "-"


print(f"{'id':34} {'tipo':4} {'vivo':4} {'bench MH/s':>11} {'actual MH/s':>12} {'saldo':>8}")
for m in sorted(mineros, key=lambda m: (m.get("tipo") or "", m["id"])):
    print(f"{m['id']:34} {m.get('tipo') or '-':4} {'sí' if m.get('vivo') else 'no':4} "
          f"{mhs(m.get('bench')):>11} {mhs(m.get('hashes_por_segundo')):>12} {m.get('saldo', 0):>8}")

gpu = sum(1 for m in mineros if m.get("tipo") == "gpu" and m.get("vivo"))
print(f"\nMineros GPU vivos: {gpu}")
ok = gpu == 0 if minimo == 0 else gpu >= minimo
if ok:
    print("OK")
elif minimo == 0:
    print("FALTA: todavía hay mineros GPU vivos")
else:
    print(f"FALTA: se esperaban al menos {minimo}")
sys.exit(0 if ok else 1)
EOF
