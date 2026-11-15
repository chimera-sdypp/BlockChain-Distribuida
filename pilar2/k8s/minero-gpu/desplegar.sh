#!/bin/bash
# Despliega (o escala) los mineros GPU en el k3s de una PC con NVIDIA (BC-36).
#
#   export KUBECONFIG=~/.kube/config                       # el del k3s local
#   export GATEWAY_URL=http://<IP pública del gateway>:8081
#   ./desplegar.sh 2                                       # 2 réplicas = 2 mineros (por defecto 1; 0 los apaga)
#
# Cada réplica firma con su propia clave (BC-57): sale de su nombre (minero-gpu-0, -1…) y de una semilla
# que este script genera la primera vez y guarda en el Secret minero-gpu (no la cambia si ya existe:
# las cuentas que aprobó el organizador siguen siendo las mismas). Al final muestra la cuenta de cada
# pod para aprobarla (pilar2/k8s/aprobar.sh, contra el cluster del sistema).
# Opcionales: NAMESPACE (por defecto mineros-gpu), IMAGEN (por defecto ghcr.io/chimera-sdypp/minero-gpu:main)
# y DRY_RUN=1, que muestra los manifiestos sin tocar el cluster. La semilla no se imprime nunca.

set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPLICAS="${1:-1}"
IMAGEN="${IMAGEN:-ghcr.io/chimera-sdypp/minero-gpu:main}"
NAMESPACE="${NAMESPACE:-mineros-gpu}"

error() { echo "Error: $*" >&2; exit 1; }

[[ "$REPLICAS" =~ ^[0-9]+$ ]] || error "las réplicas tienen que ser un entero >= 0 (se recibió '$REPLICAS')"
[[ "$NAMESPACE" =~ ^[a-z0-9-]+$ ]] || error "NAMESPACE no es un nombre válido: '$NAMESPACE'"
[[ "${GATEWAY_URL:-}" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] \
    || error "GATEWAY_URL tiene que ser http://<ip o host>:<puerto>, sin ruta (se recibió '${GATEWAY_URL:-}')"
[[ "$IMAGEN" =~ ^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)*(:[A-Za-z0-9._-]+)?$ ]] || error "IMAGEN no parece una imagen válida: '$IMAGEN'"
command -v kubectl > /dev/null || error "no está kubectl"

# El StatefulSet con la URL del gateway, la imagen y las réplicas pedidas.
statefulset() {
    sed -e "s#http://GATEWAY_IP:8081#${GATEWAY_URL}#" \
        -e "s#image: ghcr.io/[^ ]*minero-gpu:[^ ]*#image: ${IMAGEN}#" \
        -e "s#^  replicas: [0-9]*#  replicas: ${REPLICAS}#" \
        "$AQUI/minero-gpu.yaml"
}

# El Secret con una semilla nueva (32 bytes al azar), armado acá: no aparece en los argumentos de ningún proceso.
secreto() {
    printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: minero-gpu\n  namespace: %s\ntype: Opaque\ndata:\n  CLAVE_SEMILLA: %s\n' \
        "$NAMESPACE" "$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n' | base64 | tr -d '\n')"
}

if [[ "${DRY_RUN:-}" == "1" ]]; then
    echo "# --- Secret (semilla tapada; sólo se crea si no existe) ---"
    secreto | sed -E 's/^(  CLAVE_SEMILLA: ).*/\1<oculta>/'
    echo "---"
    echo "# --- StatefulSet ---"
    statefulset
    exit 0
fi

if [[ -n "${KUBECONFIG:-}" ]]; then
    [[ -f "$KUBECONFIG" ]] || error "KUBECONFIG apunta a '$KUBECONFIG', que no existe"
    permisos=$(stat -c %a "$KUBECONFIG" 2> /dev/null || echo "")
    [[ -z "$permisos" || "$permisos" == "600" ]] || echo "Aviso: $KUBECONFIG tiene permisos $permisos; conviene chmod 600" >&2
fi

echo "Cluster: $(kubectl config current-context) · namespace: $NAMESPACE"
gpus=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' | awk '{n += $1} END {print n + 0}')
[[ "${gpus:-0}" -gt 0 ]] || error "ningún nodo ofrece nvidia.com/gpu: falta el toolkit o el device plugin (ver README.md)"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - > /dev/null

if kubectl -n "$NAMESPACE" get secret minero-gpu -o jsonpath='{.data.CLAVE_SEMILLA}' 2> /dev/null | grep -q .; then
    echo "Secret minero-gpu: ya tiene su semilla (las cuentas no cambian)"
else
    echo "Secret minero-gpu: semilla nueva"
    kubectl -n "$NAMESPACE" delete secret minero-gpu --ignore-not-found > /dev/null  # el del MINER_TOKEN, si quedó
    secreto | kubectl apply -f - > /dev/null
fi
# Antes era un Deployment: si quedó, se borra (los dos pedirían la misma GPU).
kubectl -n "$NAMESPACE" delete deployment minero-gpu --ignore-not-found
echo "StatefulSet minero-gpu: $REPLICAS réplica(s) de $IMAGEN contra $GATEWAY_URL"
statefulset | kubectl -n "$NAMESPACE" apply -f -

if [[ "$REPLICAS" -gt 0 ]]; then
    if ! kubectl -n "$NAMESPACE" rollout status statefulset/minero-gpu --timeout=180s; then
        echo "No quedaron listos en 3 minutos. Estado y eventos:" >&2
        kubectl -n "$NAMESPACE" get pods -l app=minero-gpu -o wide >&2 || true
        kubectl -n "$NAMESPACE" describe pods -l app=minero-gpu 2> /dev/null | sed -n '/^Events:/,/^$/p' | tail -20 >&2 || true
        exit 1
    fi
    sleep 3  # que el minero llegue a escribir su cuenta en el log
    echo
    echo "Cuentas de los mineros (el organizador las tiene que aprobar):"
    for ((i = 0; i < REPLICAS; i++)); do
        cuenta=$(kubectl -n "$NAMESPACE" logs "minero-gpu-$i" 2> /dev/null | grep -oE 'cuenta [0-9a-f]{64}' | tail -1 | cut -d' ' -f2 || true)
        echo "  minero-gpu-$i  ${cuenta:-(todavía no arrancó: kubectl -n $NAMESPACE logs minero-gpu-$i)}"
        [[ -n "$cuenta" ]] && echo "    pilar2/k8s/aprobar.sh $cuenta minero minero-gpu-$i     # con el KUBECONFIG del sistema"
    done
fi
kubectl -n "$NAMESPACE" get pods -l app=minero-gpu -o wide
echo "Listo. Para ver que se registraron en el gateway: BACKEND_URL=http://<IP de Traefik> ./verificar.sh"
