#!/bin/bash
# startup-script de las VMs minero-cpu (Container-Optimized OS). Corre en cada arranque.
# BC-95: la clave del minero se genera acá adentro, la primera vez, y no sale de la VM. El minero la guarda en
# /var/lib/minero (disco de la VM) y deja la pública al lado; este script la publica en los guest attributes de la
# instancia (minero/publica), que sólo la VM puede escribir. El escalador la lee de ahí y firma su alta delegada.
# Lo completa la plantilla: imagen, gateway_url y registro.
set -euo pipefail

md=http://metadata.google.internal/computeMetadata/v1
datos=/var/lib/minero
mkdir -p "$datos"
chown 10001 "$datos"   # el usuario del contenedor (pilar2/Dockerfile)
chmod 700 "$datos"

cat > /run/minero.env <<EOF
GATEWAY_URL=${gateway_url}
CLAVE_ARCHIVO=/datos/minero.clave
MINERO_ID=$(curl -sf -H 'Metadata-Flavor: Google' "$md/instance/name")
EOF

# En COS el home de root es de sólo lectura; docker-credential-gcr usa la cuenta de la VM.
export HOME=/home/chronos
docker-credential-gcr configure-docker --registries="${registro}"
docker rm -f minero 2>/dev/null || true
docker run -d --name minero --restart=always --env-file /run/minero.env -v "$datos:/datos" "${imagen}"

# El minero escribe la pública al arrancar (minero_cpu.minero.cargar_clave).
for _ in $(seq 1 60); do
  [[ -s $datos/minero.clave.pub ]] && break
  sleep 2
done
publica=$(tr -d '[:space:]' < "$datos/minero.clave.pub")
if [[ ! $publica =~ ^[0-9a-f]{64}$ ]]; then
  echo "El minero no dejó su clave pública en $datos/minero.clave.pub" >&2
  exit 1
fi
curl -sf -X PUT -H 'Metadata-Flavor: Google' --data "$publica" "$md/instance/guest-attributes/minero/publica"
echo "Clave del minero publicada: $publica"
