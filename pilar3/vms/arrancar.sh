#!/bin/bash
# startup-script de las VMs minero-cpu (Container-Optimized OS). Corre en cada arranque.
# Lee el MINER_TOKEN de Secret Manager con la cuenta de servicio de la VM y arranca el minero.
# Lo completa la plantilla: imagen, gateway_url, proyecto y registro.
set -euo pipefail

md=http://metadata.google.internal/computeMetadata/v1
acceso=$(curl -sf -H 'Metadata-Flavor: Google' "$md/instance/service-accounts/default/token" |
  sed -E 's/.*"access_token" *: *"([^"]+)".*/\1/')
token=$(curl -sf -H "Authorization: Bearer $acceso" \
  "https://secretmanager.googleapis.com/v1/projects/${proyecto}/secrets/miner-token/versions/latest:access" |
  sed -nE 's/.*"data" *: *"([^"]+)".*/\1/p' | base64 -d)

# /run es tmpfs: el token no queda en disco.
umask 077
cat > /run/minero.env <<EOF
GATEWAY_URL=${gateway_url}
MINER_TOKEN=$token
MINERO_ID=$(curl -sf -H 'Metadata-Flavor: Google' "$md/instance/name")
EOF

# En COS el home de root es de sólo lectura; docker-credential-gcr usa la cuenta de la VM.
export HOME=/home/chronos
docker-credential-gcr configure-docker --registries="${registro}"
docker rm -f minero 2>/dev/null || true
docker run -d --name minero --restart=always --env-file /run/minero.env "${imagen}"
