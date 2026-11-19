#!/bin/bash
# Aprueba o da de baja una cuenta en el sistema desplegado (contrato v3, BC-79): firma un `alta-<n>` o un
# `baja-<n>` con la clave del organizador y lo manda al backend, como `python -m claves aprobar`. Nada se escribe
# directo en Redis: la cuenta queda aprobada cuando el alta entra en un bloque (un minero ya puede minar apenas el
# NCT la valida). Para scripts y pruebas; el organizador de verdad usa la herramienta de claves.
#
#   pilar2/k8s/aprobar.sh <cuenta> <usuario | minero | validador> [nombre]
#   pilar2/k8s/aprobar.sh <cuenta> baja
#
# SISTEMA_URL: el backend (por defecto http://localhost, el Ingress del k3s). ORGANIZADOR_ARCHIVO: la privada del
# organizador; si no está, la de prueba (comun/tests/claves.py), que es la que usa el k3s. PYTHON: el intérprete.
set -euo pipefail

cuenta=${1:-} tipo=${2:-} nombre=${3:-}
error() { echo "Error: $*" >&2; exit 1; }
[[ $cuenta =~ ^[0-9a-f]{64}$ ]] || error "la cuenta es la clave pública en hex (64 caracteres en minúscula)"
[[ $tipo =~ ^(usuario|minero|validador|baja)$ ]] || error "el tipo es usuario, minero, validador o baja"
nombre=${nombre:-${cuenta:0:12}}
[[ $nombre =~ ^[A-Za-z0-9_.-]{1,32}$ ]] || error "el nombre: de 1 a 32 letras, números, «_», «.» o «-»"

cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-$( [[ -x .venv/bin/python ]] && echo .venv/bin/python || echo python3 )}
URL=${SISTEMA_URL:-http://localhost}
archivo=${ORGANIZADOR_ARCHIVO:-}
if [[ -z $archivo ]]; then
    archivo=$(mktemp)
    trap 'rm -f "$archivo"' EXIT
    chmod 600 "$archivo"
    "$PYTHON" -c "import hashlib; print(hashlib.sha256(b'chimera-prueba-organizador').hexdigest())" > "$archivo"
fi

if [[ $tipo == baja ]]; then
    exec "$PYTHON" -m claves baja "$archivo" --cuenta "$cuenta" --motivo "aprobar.sh" --url "$URL"
fi
exec "$PYTHON" -m claves aprobar "$archivo" --cuenta "$cuenta" --tipo "$tipo" --nombre "$nombre" --url "$URL"
