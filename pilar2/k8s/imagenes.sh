#!/usr/bin/env bash
# Construye las imágenes de los servicios y las importa en el containerd de k3s (no usa las de Docker).
# Desde pilar2/: k8s/imagenes.sh   (k3s ctr pide root). Después de reimportar con el mismo tag:
#   kubectl -n blockchain rollout restart deployment
set -euo pipefail
cd "$(dirname "$0")/.."
SERVICIOS=(backend frontend nct trp gateway)
for servicio in "${SERVICIOS[@]}"; do
  docker build -q --build-arg SERVICIO="$servicio" -t "$servicio:dev" . >/dev/null
  echo "construida $servicio:dev"
done
docker build -q -f escalador/Dockerfile -t escalador:dev . >/dev/null
echo "construida escalador:dev"
docker save "${SERVICIOS[@]/%/:dev}" escalador:dev | sudo k3s ctr images import -
