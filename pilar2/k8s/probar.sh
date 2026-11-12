#!/usr/bin/env bash
# Prueba RabbitMQ y Redis en el cluster: que lo definido aparece solo y que aguantan la caída de un pod.
#   pilar2/k8s/probar.sh        (kubectl apuntando al cluster; NS=blockchain por defecto)
set -euo pipefail
ns=${NS:-blockchain}
k() { kubectl -n "$ns" "$@"; }
ok() { echo "  ✔ $*"; }
falla() { echo "  ✘ $*" >&2; exit 1; }
esperar() {  # esperar <segundos> <comando…>: reintenta hasta que el comando ande
  local hasta=$((SECONDS + $1)); shift
  until "$@" >/dev/null 2>&1; do ((SECONDS < hasta)) || return 1; sleep 2; done
}
secreto() { k get secret blockchain-secretos -o jsonpath="{.data.$1}" | base64 -d; }

usuario=$(secreto RABBIT_USUARIO)
clave=$(secreto RABBIT_CLAVE)
# rabbitmqadmin dentro del pod; la clave entra por stdin, así no queda en los argumentos.
admin() {
  local pod=$1; shift
  printf '%s\n' "$clave" | k exec -i "$pod" -c rabbitmq -- \
    sh -c 'read -r c; u=$1; shift; rabbitmqadmin -u "$u" -p "$c" --non-interactive "$@"' _ "$usuario" "$@"
}
ctl() { local pod=$1; shift; k exec "$pod" -c rabbitmq -- rabbitmqctl -q "$@"; }
nodos_corriendo() {
  ctl "$1" cluster_status --formatter json | python3 -c 'import json, sys; print(len(json.load(sys.stdin)["running_nodes"]))'
}

echo "== RabbitMQ"
k rollout status sts/rabbitmq --timeout=300s >/dev/null
[[ $(nodos_corriendo rabbitmq-0) == 3 ]] || falla "el cluster no tiene 3 nodos"
ok "cluster de 3 nodos"
ctl rabbitmq-0 list_queues name type members --formatter json | python3 -c '
import json, sys
colas = {c["name"]: c for c in json.load(sys.stdin)}
for nombre in ("transacciones", "tareas", "rangos", "resultados", "soluciones"):
    c = colas.get(nombre) or sys.exit(f"falta la cola {nombre}")
    if c["type"] != "quorum" or len(c["members"]) != 3:
        sys.exit(nombre + ": " + c["type"] + " con " + str(len(c["members"])) + " réplicas")
' || falla "colas"
ok "5 colas quorum con 3 réplicas"
ctl rabbitmq-0 list_exchanges name type | grep -P '^eventos\ttopic$' >/dev/null || falla "falta el exchange eventos"
ok "exchange eventos (topic)"
ctl rabbitmq-0 list_permissions | grep -P "^$usuario\t\.\*\t\.\*\t\.\*$" >/dev/null || falla "falta el usuario $usuario"
ok "usuario $usuario con permisos sobre /"

admin rabbitmq-1 declare queue --name prueba-ha --type quorum --durable true >/dev/null
admin rabbitmq-1 publish message --routing-key prueba-ha --payload antes >/dev/null
k delete pod rabbitmq-0 --wait=false >/dev/null
sin_rabbitmq0() { [[ $(nodos_corriendo rabbitmq-1) == 2 ]]; }
esperar 60 sin_rabbitmq0 || falla "rabbitmq-0 no se cayó"
admin rabbitmq-1 publish message --routing-key prueba-ha --payload durante >/dev/null
recibidos=$(admin rabbitmq-2 get messages --queue prueba-ha --count 2 --ack-mode ack_requeue_false)
grep -q antes <<<"$recibidos" && grep -q durante <<<"$recibidos" || falla "se perdieron mensajes: $recibidos"
ok "sin rabbitmq-0 la cola sigue: se publicó y se leyó por los otros dos"
k rollout status sts/rabbitmq --timeout=300s >/dev/null
con_rabbitmq0() { [[ $(nodos_corriendo rabbitmq-0) == 3 ]]; }
esperar 120 con_rabbitmq0 || falla "rabbitmq-0 no volvió al cluster"
ok "rabbitmq-0 volvió al cluster"
admin rabbitmq-1 delete queue --name prueba-ha >/dev/null

echo "== Redis"
k rollout status sts/redis --timeout=300s >/dev/null
rcli() { local pod=$1; shift; k exec "$pod" -c redis -- redis-cli "$@"; }
primario() { k exec "$1" -c sentinel -- redis-cli -p 26379 sentinel get-master-addr-by-name mymaster | sed -n 1p | cut -d. -f1; }
viejo=$(primario redis-0)
ok "primario: $viejo"
for pod in redis-0 redis-1 redis-2; do
  [[ $pod == "$viejo" ]] || rcli "$pod" info replication | grep 'master_link_status:up' >/dev/null || falla "$pod no replica"
done
ok "las otras dos replican"
rcli "$viejo" set prueba:antes 1 >/dev/null
[[ $(rcli "$viejo" wait 2 5000) == 2 ]] || falla "la escritura no llegó a las 2 réplicas"
ok "escritura copiada a las 2 réplicas (WAIT 2)"

testigo=$([[ $viejo == redis-0 ]] && echo redis-1 || echo redis-0)
k delete pod "$viejo" --wait=false >/dev/null
otro_primario() { [[ $(primario "$testigo") != "$viejo" ]]; }
esperar 90 otro_primario || falla "Sentinel no eligió otro primario"
nuevo=$(primario "$testigo")
ok "Sentinel promovió a $nuevo"
[[ $(rcli "$nuevo" get prueba:antes) == 1 ]] || falla "se perdió prueba:antes"
rcli "$nuevo" set prueba:despues 1 >/dev/null
ok "en $nuevo está la clave de antes y se puede escribir"
k rollout status sts/redis --timeout=300s >/dev/null
replica() { rcli "$viejo" info replication | grep 'master_link_status:up' >/dev/null; }
esperar 60 replica || falla "$viejo no volvió como réplica"
[[ $(rcli "$viejo" get prueba:despues) == 1 ]] || falla "$viejo no tiene lo escrito durante la caída"
ok "$viejo volvió como réplica de $nuevo, con lo escrito mientras no estaba"
rcli "$nuevo" del prueba:antes prueba:despues >/dev/null

echo "Todo OK"
