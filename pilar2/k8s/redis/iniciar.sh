#!/bin/sh
# Arma redis.conf y sentinel.conf antes de arrancar el pod.
# El primario es el que diga Sentinel. Si ningún Sentinel contesta (primer arranque), es redis-0.
set -eu

dominio="redis-headless.${POD_NAMESPACE}.svc.cluster.local"
yo="${POD_NAME}.${dominio}"

primario=$(timeout 5 redis-cli -h redis -p 26379 sentinel get-master-addr-by-name mymaster 2>/dev/null | head -1 || true)
case "$primario" in
  ""|*ERR*|*NOAUTH*) primario="redis-0.${dominio}" ;;
esac
echo "Yo: $yo; primario: $primario"

cat > /config/redis.conf <<EOF
port 6379
dir /data
appendonly yes
appendfsync everysec
save 3600 1 300 100 60 10000
requirepass ${REDIS_CLAVE}
masterauth ${REDIS_CLAVE}
replica-announce-ip ${yo}
EOF
if [ "$primario" != "$yo" ]; then
  echo "replicaof ${primario} 6379" >> /config/redis.conf
fi

# Sentinel reescribe este archivo, por eso va en un volumen propio y se arma de nuevo en cada arranque.
cat > /config/sentinel.conf <<EOF
port 26379
dir /tmp
requirepass ${REDIS_CLAVE}
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
sentinel announce-ip ${yo}
sentinel sentinel-pass ${REDIS_CLAVE}
sentinel monitor mymaster ${primario} 6379 2
sentinel auth-pass mymaster ${REDIS_CLAVE}
sentinel down-after-milliseconds mymaster 5000
sentinel failover-timeout mymaster 15000
sentinel parallel-syncs mymaster 1
EOF
