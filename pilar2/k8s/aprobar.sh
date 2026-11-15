#!/bin/bash
# Aprueba una cuenta en el Redis del sistema desplegado: es lo que hace el organizador hasta que esté
# el panel (BC-58). Un minero sin aprobar recibe 403 del gateway; un usuario sin aprobar no puede
# transferir (el NCT la rechaza con «cuenta no aprobada»).
#
#   pilar2/k8s/aprobar.sh <cuenta> <usuario | minero> [nombre]      # con el KUBECONFIG del sistema
#   pilar2/k8s/aprobar.sh <cuenta> baja                             # la saca de `cuentas`
#
# Le habla al primario de Redis con kubectl exec (el que diga Sentinel): la clave de Redis no sale del pod.
# NAMESPACE (por defecto blockchain).
set -euo pipefail

NAMESPACE=${NAMESPACE:-blockchain}
cuenta=${1:-} tipo=${2:-} nombre=${3:-}
error() { echo "Error: $*" >&2; exit 1; }

[[ $cuenta =~ ^[0-9a-f]{64}$ ]] || error "la cuenta es la clave pública en hex (64 caracteres en minúscula)"
[[ $tipo =~ ^(usuario|minero|organizador|baja)$ ]] || error "el tipo es usuario, minero, organizador o baja"
[[ -z $nombre || $nombre =~ ^[A-Za-z0-9_.-]{1,32}$ ]] || error "el nombre: de 1 a 32 letras, números, «_», «.» o «-»"

k() { kubectl -n "$NAMESPACE" "$@"; }
primario=$(k exec redis-0 -c sentinel -- redis-cli -p 26379 sentinel get-master-addr-by-name mymaster | head -1)
[[ -n $primario ]] || error "Sentinel no dijo cuál es el primario de Redis"

if [[ $tipo == baja ]]; then
    k exec redis-0 -c redis -- redis-cli -h "$primario" HDEL cuentas "$cuenta" > /dev/null
    echo "Dada de baja: $cuenta"
    exit 0
fi
datos=$(printf '{"tipo": "%s", "nombre": "%s", "alta": %d}' "$tipo" "$nombre" "$(date +%s)")
k exec redis-0 -c redis -- redis-cli -h "$primario" HSET cuentas "$cuenta" "$datos" > /dev/null
k exec redis-0 -c redis -- redis-cli -h "$primario" HDEL altas:pendientes "$cuenta" > /dev/null
echo "Aprobada como $tipo: $cuenta ${nombre:+($nombre)}"
