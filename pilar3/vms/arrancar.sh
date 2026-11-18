#!/bin/bash
# startup-script de las VMs minero-cpu (Container-Optimized OS). Corre en cada arranque.
# Lee la clave del minero de la metadata de la instancia (`clave-minero`: la genera el escalador, que
# además aprueba su cuenta, BC-57) y arranca el minero. Lo completa la plantilla: imagen, gateway_url y registro.
set -euo pipefail

md=http://metadata.google.internal/computeMetadata/v1
clave=$(curl -sf -H 'Metadata-Flavor: Google' "$md/instance/attributes/clave-minero" || true)
if [[ ! $clave =~ ^[0-9a-f]{64}$ ]]; then
  echo "La VM no tiene clave-minero en la metadata: la crea el escalador (tofu apply con TF_VAR_claves)" >&2
  exit 1
fi

# /run es tmpfs: la clave no queda en disco.
umask 077
cat > /run/minero.env <<EOF
GATEWAY_URL=${gateway_url}
CLAVE_PRIVADA=$clave
MINERO_ID=$(curl -sf -H 'Metadata-Flavor: Google' "$md/instance/name")
EOF

# En COS el home de root es de sólo lectura; docker-credential-gcr usa la cuenta de la VM.
export HOME=/home/chronos
docker-credential-gcr configure-docker --registries="${registro}"
docker rm -f minero 2>/dev/null || true
docker run -d --name minero --restart=always --env-file /run/minero.env "${imagen}"
