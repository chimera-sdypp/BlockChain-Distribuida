#!/usr/bin/env bash
# Pruebas del sistema desplegado en k3s (BC-30 y BC-37): el recorrido de una transferencia, las caídas
# y el monitoreo. Desde pilar2/, con todo levantado (ver README): k8s/probar_sistema.sh [-k filtro de pytest]
set -euo pipefail
export NAMESPACE=${NAMESPACE:-blockchain}
export SISTEMA_URL=${SISTEMA_URL:-http://localhost}        # el Ingress de Traefik
export GATEWAY_URL=${GATEWAY_URL:-http://localhost:8081}    # el LoadBalancer del gateway
MINER_TOKEN=$(kubectl -n "$NAMESPACE" get secret blockchain-secretos -o jsonpath='{.data.MINER_TOKEN}' | base64 -d)
export MINER_TOKEN

# Monitoreo (namespace monitoring): si no vienen las URLs, abre los port-forwards y los cierra al salir.
reenviar() {  # variable, servicio, puerto local, puerto del Service
  if [[ -z ${!1:-} ]]; then
    kubectl -n monitoring port-forward "svc/$2" "$3:$4" >/dev/null &
    export "$1=http://localhost:$3"
  fi
}
if kubectl get namespace monitoring >/dev/null 2>&1; then
  trap 'kill $(jobs -p) 2>/dev/null || true' EXIT
  reenviar PROMETHEUS_URL monitoreo-prometheus 9090 9090
  reenviar LOKI_URL loki 3100 3100
  reenviar GRAFANA_URL monitoreo-grafana 3000 80
  GRAFANA_USUARIO=$(kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-user}' | base64 -d)
  GRAFANA_CLAVE=$(kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d)
  export GRAFANA_USUARIO GRAFANA_CLAVE
  sleep 2
fi
python -m pytest sistema -v "$@"
