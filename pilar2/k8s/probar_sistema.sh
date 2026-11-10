#!/usr/bin/env bash
# Pruebas del sistema desplegado en k3s (BC-30): el recorrido de una transferencia y las caídas.
# Desde pilar2/, con todo levantado (ver README): k8s/probar_sistema.sh [-k filtro de pytest]
set -euo pipefail
export NAMESPACE=${NAMESPACE:-blockchain}
export SISTEMA_URL=${SISTEMA_URL:-http://localhost}        # el Ingress de Traefik
export GATEWAY_URL=${GATEWAY_URL:-http://localhost:8081}    # el LoadBalancer del gateway
MINER_TOKEN=$(kubectl -n "$NAMESPACE" get secret blockchain-secretos -o jsonpath='{.data.MINER_TOKEN}' | base64 -d)
export MINER_TOKEN
exec python -m pytest sistema -v "$@"
