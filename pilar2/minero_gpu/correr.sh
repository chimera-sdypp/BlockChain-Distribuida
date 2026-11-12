#!/bin/bash
# Mineros GPU en una PC con NVIDIA (BC-36): N procesos de minero_gpu en la GPU local, contra el gateway público.
#
#   GATEWAY_URL=http://<IP del gateway>:8081 GCP_PROYECTO=<id> pilar2/minero_gpu/correr.sh [N]
#
# - Compila minar.cu la primera vez (y cuando cambia) en ~/.cache/blockchain-distribuida/minar.
# - El MINER_TOKEN sale del entorno o, si no está, de Secret Manager (gcloud, proyecto GCP_PROYECTO).
# - Ctrl-C (o kill) corta todos los mineros: así «sale» la GPU (BC-41); el gateway la da por muerta a los 15 s.
# Necesita nvcc, gcloud (si no se pasa el token) y un Python con pilar2/comun/requirements.txt.

set -euo pipefail

N="${1:-1}"
[[ "$N" =~ ^[1-9][0-9]*$ ]] || { echo "Error: N tiene que ser un entero >= 1" >&2; exit 1; }
[[ "${GATEWAY_URL:-}" =~ ^https?://[^/]+/?$ ]] || { echo "Error: falta GATEWAY_URL (p. ej. http://<IP>:8081)" >&2; exit 1; }

RAIZ="$(cd "$(dirname "$0")/../.." && pwd)"
MINAR="${MINAR:-$HOME/.cache/blockchain-distribuida/minar}"
FUENTE="$RAIZ/pilar2/minero_gpu/minar.cu"
if [[ ! -x "$MINAR" || "$FUENTE" -nt "$MINAR" ]]; then
    echo "Compilando $FUENTE (${ARQUITECTURA:=sm_89})…"
    mkdir -p "$(dirname "$MINAR")"
    (cd "$RAIZ" && nvcc -O3 -arch="$ARQUITECTURA" -o "$MINAR" pilar2/minero_gpu/minar.cu)
fi

if [[ -z "${MINER_TOKEN:-}" ]]; then
    [[ -n "${GCP_PROYECTO:-}" ]] || { echo "Error: falta MINER_TOKEN o GCP_PROYECTO (para leerlo de Secret Manager)" >&2; exit 1; }
    MINER_TOKEN="$(gcloud secrets versions access latest --secret=miner-token --project "$GCP_PROYECTO")"
fi
export MINAR GATEWAY_URL MINER_TOKEN

PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
PREFIJO="${MINERO_PREFIJO:-gpu-pc}"
pids=()
cortar() { trap - INT TERM EXIT; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cortar INT TERM EXIT

cd "$RAIZ/pilar2"
for ((i = 0; i < N; i++)); do
    MINERO_ID="$PREFIJO-$i" "$PYTHON" -m minero_gpu &
    pids+=("$!")
    echo "Minero $PREFIJO-$i (pid $!) → $GATEWAY_URL"
done
wait
