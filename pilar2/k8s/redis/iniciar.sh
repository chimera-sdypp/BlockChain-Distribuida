#!/bin/sh
# Arma redis.conf y sentinel.conf antes de arrancar el pod.
# El primario es el que diga Sentinel (nunca yo si me acabo de caer). Si ningún Sentinel contesta
# (primer arranque), es redis-0.
set -eu

dominio="redis-headless.${POD_NAMESPACE}.svc.cluster.local"
yo="${POD_NAME}.${dominio}"

preguntar() {
  r=$(timeout 5 redis-cli -h redis -p 26379 sentinel get-master-addr-by-name mymaster 2>/dev/null | head -1 || true)
  case "$r" in ""|*ERR*|*NOAUTH*) r="" ;; esac
  echo "$r"
}

primario=$(preguntar)
# Si los Sentinel dicen que el primario soy yo, este pod se cayó siendo primario y el cluster sigue
# vivo. Arrancar como primario dejaría dos primarios hasta que Sentinel lo note, y lo escrito en el
# viejo en ese rato se pierde. Mientras corre este init el Redis de este pod está apagado, así que
# Sentinel lo ve caído y promueve una réplica: se espera eso y se arranca como réplica del nuevo.
# Si en ESPERA_FAILOVER segundos no hay otro (no queda ninguna réplica), se arranca como primario.
# Con un Sentinel caído, los otros dos pueden empatar la elección del líder y reintentan cada 30 s
# (2 × failover-timeout): por eso la espera es larga.
if [ "$primario" = "$yo" ]; then
  echo "Sentinel me nombra primario: espero que promueva a otro"
  fin=$(( $(date +%s) + ${ESPERA_FAILOVER:-180} ))
  while [ "$primario" = "$yo" ] && [ "$(date +%s)" -lt "$fin" ]; do
    sleep 2
    nuevo=$(preguntar)
    [ -n "$nuevo" ] && primario="$nuevo"
  done
fi
[ -n "$primario" ] || primario="redis-0.${dominio}"   # ningún Sentinel contesta: primer arranque
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
