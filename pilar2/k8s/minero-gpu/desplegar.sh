#!/bin/bash
# Despliega (o escala) los mineros GPU en el K3s de la cátedra (BC-36).
#
#   export KUBECONFIG=~/.kube/chimera-catedra.yaml        # el del grupo, con chmod 600
#   export NAMESPACE=<namespace del grupo> GATEWAY_URL=http://<IP pública del gateway>:8081
#   export MINER_TOKEN=...                                 # el mismo que tiene el gateway
#   ./desplegar.sh 2                                       # 2 réplicas = 2 GPU (por defecto 1; 0 las apaga)
#
# Opcionales: IMAGEN (por defecto ghcr.io/chimera-sdypp/minero-gpu:dev) y DRY_RUN=1, que muestra
# los manifiestos (con el token tapado) sin tocar el cluster.
# El token no se imprime nunca: va del entorno al Secret por stdin.

set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPLICAS="${1:-1}"
IMAGEN="${IMAGEN:-ghcr.io/chimera-sdypp/minero-gpu:dev}"

error() { echo "Error: $*" >&2; exit 1; }

[[ "$REPLICAS" =~ ^[0-9]+$ ]] || error "las réplicas tienen que ser un entero >= 0 (se recibió '$REPLICAS')"
[[ -n "${NAMESPACE:-}" ]] || error "falta NAMESPACE (el namespace del grupo en el cluster de la cátedra)"
[[ "${GATEWAY_URL:-}" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] \
    || error "GATEWAY_URL tiene que ser http://<ip o host>:<puerto>, sin ruta (se recibió '${GATEWAY_URL:-}')"
[[ "$IMAGEN" =~ ^[a-z0-9./_-]+(:[A-Za-z0-9._-]+)?$ ]] || error "IMAGEN no parece una imagen válida: '$IMAGEN'"
command -v kubectl > /dev/null || error "no está kubectl"

# El Deployment con la URL del gateway, la imagen y las réplicas pedidas.
deployment() {
    sed -e "s#http://GATEWAY_IP:8081#${GATEWAY_URL}#" \
        -e "s#image: ghcr.io/[^ ]*minero-gpu:[^ ]*#image: ${IMAGEN}#" \
        -e "s#^  replicas: [0-9]*#  replicas: ${REPLICAS}#" \
        "$AQUI/minero-gpu.yaml"
}

# El Secret con el token. Se arma acá (printf es un builtin y base64 lo lee por stdin), así el
# token nunca aparece en los argumentos de un proceso ni en la salida.
secreto() {
    printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: minero-gpu\n  namespace: %s\ntype: Opaque\ndata:\n  MINER_TOKEN: %s\n' \
        "$NAMESPACE" "$(printf '%s' "$MINER_TOKEN" | base64 | tr -d '\n')"
}

if [[ "${DRY_RUN:-}" == "1" ]]; then
    MINER_TOKEN="${MINER_TOKEN:-sin-token}"
    echo "# --- Secret (token tapado) ---"
    secreto | sed -E 's/^(  MINER_TOKEN: ).*/\1<oculto>/'
    echo "---"
    echo "# --- Deployment ---"
    deployment
    exit 0
fi

[[ -n "${MINER_TOKEN:-}" ]] || error "falta MINER_TOKEN (el mismo que usa el gateway)"
if [[ -n "${KUBECONFIG:-}" ]]; then
    [[ -f "$KUBECONFIG" ]] || error "KUBECONFIG apunta a '$KUBECONFIG', que no existe"
    permisos=$(stat -c %a "$KUBECONFIG" 2> /dev/null || echo "")
    [[ -z "$permisos" || "$permisos" == "600" ]] || echo "Aviso: $KUBECONFIG tiene permisos $permisos; conviene chmod 600" >&2
fi

echo "Cluster: $(kubectl config current-context) · namespace: $NAMESPACE"
[[ "$(kubectl auth can-i create deployments -n "$NAMESPACE" 2> /dev/null)" == "yes" ]] \
    || error "no hay permiso para crear Deployments en '$NAMESPACE' (¿KUBECONFIG o NAMESPACE equivocados?)"

echo "Secret minero-gpu..."
secreto | kubectl apply -f - > /dev/null
echo "Deployment minero-gpu: $REPLICAS réplica(s) de $IMAGEN contra $GATEWAY_URL"
deployment | kubectl -n "$NAMESPACE" apply -f -

if [[ "$REPLICAS" -gt 0 ]]; then
    if ! kubectl -n "$NAMESPACE" rollout status deployment/minero-gpu --timeout=180s; then
        echo "No quedaron listos en 3 minutos. Estado y eventos:" >&2
        kubectl -n "$NAMESPACE" get pods -l app=minero-gpu -o wide >&2 || true
        kubectl -n "$NAMESPACE" describe pods -l app=minero-gpu 2> /dev/null | sed -n '/^Events:/,/^$/p' | tail -20 >&2 || true
        exit 1
    fi
fi
kubectl -n "$NAMESPACE" get pods -l app=minero-gpu -o wide
echo "Listo. Para ver que se registraron en el gateway: BACKEND_URL=http://<backend> ./verificar.sh"
