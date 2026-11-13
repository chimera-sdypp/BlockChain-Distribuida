#!/usr/bin/env bash
# Prepara GKE para las pruebas de carga del Pilar 3 (BC-38 a BC-41) y después lo deja como estaba.
# Lo corre el workflow «Pilar 3 · Preparar prueba» (.github/workflows/pilar3-preparar-prueba.yml);
# también anda desde una PC con kubectl apuntando al cluster.
#
#   preparar.sh estado                 IP pública, altura, saldos, mineros y condiciones actuales
#   preparar.sh reiniciar              BORRA LA CADENA: vacía Redis y las colas; el NCT carga el génesis
#                                      de nuevo, con los SALDOS_INICIALES de su Deployment
#   preparar.sh condiciones P A F      TrP con PREFIJO_BASE=P, AJUSTAR_PREFIJO=A (0 o 1) y
#                                      FRAGMENTO_PORCENTAJE=F (el escalador, con el mismo PREFIJO_BASE)
#   preparar.sh fijar-vms N            pausa el escalador en KEDA y deja N VMs minero-cpu
#                                      (necesita tofu y PROYECTO, el proyecto de GCP)
#   preparar.sh restaurar              TrP y escalador como en los manifiestos, y el escalador de nuevo
#                                      en manos de KEDA (la cadena reiniciada no vuelve)
set -euo pipefail

NS=${NS:-blockchain}
SALDO_MINIMO=${SALDO_MINIMO:-1000000}   # reiniciar se niega con saldos iniciales menores
RAIZ=$(cd "$(dirname "$0")/../.." && pwd)
COLAS="transacciones tareas rangos resultados soluciones"

k() { kubectl -n "$NS" "$@"; }

ip_publica() {
    kubectl -n traefik get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
}

# Valor de una variable de entorno del contenedor principal de un Deployment (vacío si no está).
env_de() {
    k get deploy "$1" -o jsonpath="{.spec.template.spec.containers[?(@.name=='$1')].env[?(@.name=='$2')].value}"
}

mineros_cpu_vivos() {   # cuántas VMs nuestras (minero-cpu-*) avisaron hace poco
    curl -fsS --max-time 10 "http://$1/api/mineros" | python3 -c '
import json, sys
print(sum(1 for m in json.load(sys.stdin) if m.get("vivo") and str(m.get("id", "")).startswith("minero-cpu-")))'
}

estado() {
    local ip
    ip=$(ip_publica)
    echo "Sistema:   http://$ip   (para correr.py: --url http://$ip)"
    echo "TrP:       PREFIJO_BASE=$(env_de trp PREFIJO_BASE) AJUSTAR_PREFIJO=$(env_de trp AJUSTAR_PREFIJO) FRAGMENTO_PORCENTAJE=$(env_de trp FRAGMENTO_PORCENTAJE)   (vacío: el valor por defecto del TrP)"
    echo "Escalador: pausado en KEDA=$(k get scaledobject escalador -o jsonpath='{.metadata.annotations.autoscaling\.keda\.sh/paused-replicas}' 2>/dev/null || true)   (vacío: lo maneja KEDA)"
    echo "NCT:       SALDOS_INICIALES=$(env_de nct SALDOS_INICIALES)"
    local base="http://$ip"
    python3 - "$base" <<'EOF'
import json, sys, urllib.request

base = sys.argv[1]
def get(ruta):
    with urllib.request.urlopen(base + ruta, timeout=10) as r:
        return json.load(r)

try:
    punta = get("/api/bloques?limite=1")
    saldos = get("/api/saldos")
    mineros = get("/api/mineros")
except OSError as error:
    sys.exit(f"No responde {base}: {error}")
if punta:
    print(f"Cadena:    altura {punta[0]['altura']}, prefijo del último bloque «{punta[0].get('prefijo', '')}»")
print("Saldos:    " + ", ".join(f"{u}={saldos.get(u, 0)}" for u in ("alice", "bob", "carol")))
vivos = [m for m in mineros if m.get("vivo")]
print(f"Mineros:   {sum(m.get('tipo') == 'gpu' for m in vivos)} GPU y {sum(m.get('tipo') == 'cpu' for m in vivos)} CPU vivos")
for m in sorted(vivos, key=lambda m: (m.get("tipo", ""), m.get("id", ""))):
    print(f"           - {m.get('tipo')}: {m.get('id')}")
EOF
}

reiniciar() {
    local saldos replicas_nct replicas_trp primario
    saldos=$(env_de nct SALDOS_INICIALES)
    echo "SALDOS_INICIALES del NCT: ${saldos:-(los de por defecto)}"
    if ! python3 -c 'import json, sys; s = json.loads(sys.argv[1] or "{}"); sys.exit(0 if s and min(s.values()) >= int(sys.argv[2]) else 1)' \
        "$saldos" "$SALDO_MINIMO"; then
        echo "::error::El NCT no tiene saldos iniciales de al menos $SALDO_MINIMO: reiniciar no serviría. Falta desplegar pilar3/k8s/apps/nct-saldos.yaml."
        exit 1
    fi

    replicas_nct=$(k get deploy nct -o jsonpath='{.spec.replicas}')
    replicas_trp=$(k get deploy trp -o jsonpath='{.spec.replicas}')
    echo "Apagando el NCT ($replicas_nct) y el TrP ($replicas_trp), para que nadie escriba en el medio"
    k scale deploy nct trp --replicas=0
    k wait --for=delete pod -l app=nct --timeout=180s
    k wait --for=delete pod -l app=trp --timeout=180s

    # Lo que quedó en las colas es de la cadena vieja. Si no se puede purgar no es grave: el NCT
    # descarta las tareas y transacciones que ya no están en Redis.
    for cola in $COLAS; do
        k exec rabbitmq-0 -c rabbitmq -- rabbitmqctl -q purge_queue "$cola" \
            && echo "Cola $cola vacía" || echo "::warning::No se pudo vaciar la cola $cola"
    done

    primario=$(k exec redis-0 -c sentinel -- redis-cli -p 26379 sentinel get-master-addr-by-name mymaster | head -1)
    echo "Vaciando Redis (primario: $primario); las réplicas copian el FLUSHALL"
    k exec redis-0 -c redis -- redis-cli -h "$primario" FLUSHALL
    k exec redis-0 -c redis -- redis-cli -h "$primario" WAIT 1 5000 > /dev/null

    echo "Prendiendo el NCT y el TrP: el NCT carga el génesis al arrancar"
    k scale deploy nct --replicas="$replicas_nct"
    k scale deploy trp --replicas="$replicas_trp"
    k rollout status deploy/nct --timeout=5m
    k rollout status deploy/trp --timeout=5m
    k logs -l app=nct --tail=200 --prefix | grep -i "génesis" || true
}

condiciones() {
    local prefijo=${1:?falta PREFIJO_BASE} ajustar=${2:?falta AJUSTAR_PREFIJO} fragmento=${3:?falta FRAGMENTO_PORCENTAJE}
    [[ $prefijo =~ ^[0-9]+$ ]] && ((prefijo >= 1 && prefijo <= 32)) || { echo "::error::PREFIJO_BASE va de 1 a 32"; exit 1; }
    [[ $ajustar == 0 || $ajustar == 1 ]] || { echo "::error::AJUSTAR_PREFIJO es 0 o 1"; exit 1; }
    [[ $fragmento =~ ^[0-9]+$ ]] && ((fragmento >= 1 && fragmento <= 50)) || { echo "::error::FRAGMENTO_PORCENTAJE va de 1 a 50"; exit 1; }
    k set env deploy/trp PREFIJO_BASE="$prefijo" AJUSTAR_PREFIJO="$ajustar" FRAGMENTO_PORCENTAJE="$fragmento"
    # El escalador calcula las VMs con el prefijo del sistema: el mismo que el TrP.
    k set env deploy/escalador PREFIJO_BASE="$prefijo"
    k rollout status deploy/trp --timeout=5m
}

fijar_vms() {
    local cantidad=${1:?falta la cantidad de VMs} ip i
    [[ $cantidad =~ ^[0-9]+$ ]] && ((cantidad <= 10)) || { echo "::error::La cantidad de VMs va de 0 a 10"; exit 1; }
    : "${PROYECTO:?falta PROYECTO, el proyecto de GCP}"

    # Con el escalador pausado nadie más aplica el módulo de las VMs. Si estaba en medio de un apply,
    # lo termina antes de salir (y el lock del state lo protege igual).
    k annotate scaledobject escalador autoscaling.keda.sh/paused-replicas=0 --overwrite
    for i in $(seq 1 60); do
        [ -z "$(k get pods -l app=escalador -o name)" ] && break
        sleep 10
    done
    [ -z "$(k get pods -l app=escalador -o name)" ] || { echo "::error::El escalador no se apagó en 10 minutos"; exit 1; }

    tofu -chdir="$RAIZ/pilar3/vms/instancias" init -input=false -reconfigure -backend-config="bucket=$PROYECTO-tfstate"
    TF_VAR_proyecto=$PROYECTO tofu -chdir="$RAIZ/pilar3/vms/instancias" apply -input=false -auto-approve -var "cantidad=$cantidad"

    ip=$(ip_publica)
    echo "Esperando $cantidad minero(s) CPU vivo(s) (las VMs tardan unos minutos en arrancar)"
    for i in $(seq 1 60); do
        [ "$(mineros_cpu_vivos "$ip")" -ge "$cantidad" ] && { echo "Listo: $cantidad VM(s)"; return; }
        sleep 10
    done
    echo "::warning::A los 10 minutos hay $(mineros_cpu_vivos "$ip") de $cantidad mineros CPU vivos"
}

# Las variables de entorno de un Deployment como las ponen los manifiestos (NOMBRE=valor por línea).
# Se recorta ese Deployment del render porque `set env --local` no entiende las CRDs (KEDA, Traefik).
env_del_manifiesto() {
    kubectl kustomize --load-restrictor LoadRestrictionsNone "$RAIZ/pilar3/k8s/apps" | python3 -c '
import re, sys
nombre = sys.argv[1]
for doc in sys.stdin.read().split("\n---\n"):
    if re.search(r"^kind: Deployment$", doc, re.M) and \
            re.search(r"^metadata:\n(?:  .*\n)*?  name: " + re.escape(nombre) + r"$", doc, re.M):
        print(doc)' "$1" | kubectl set env --local -f - --list --containers="$1" | grep -v '^#'
}

restaurar() {
    local manifiesto argumentos variable valor variables
    # Cada variable vuelve al valor del manifiesto, o se saca si el manifiesto no la pone.
    for despliegue in trp escalador; do
        variables="PREFIJO_BASE AJUSTAR_PREFIJO FRAGMENTO_PORCENTAJE"
        [ "$despliegue" = escalador ] && variables="PREFIJO_BASE"
        manifiesto=$(env_del_manifiesto "$despliegue")
        argumentos=()
        for variable in $variables; do
            valor=$(printf '%s\n' "$manifiesto" | sed -n "s/^$variable=//p")
            if [ -n "$valor" ]; then
                argumentos+=("$variable=$valor")
            elif [ -n "$(env_de "$despliegue" "$variable")" ]; then
                argumentos+=("$variable-")
            fi
        done
        echo "$despliegue: ${argumentos[*]}"
        [ ${#argumentos[@]} -eq 0 ] || k set env "deploy/$despliegue" "${argumentos[@]}"
    done
    k rollout status deploy/trp --timeout=5m
    # KEDA lo vuelve a prender si hace falta; con sus VMs vivas lo deja prendido hasta borrarlas.
    k annotate scaledobject escalador autoscaling.keda.sh/paused-replicas- || true
}

case "${1:-}" in
    estado) estado ;;
    reiniciar) reiniciar ;;
    condiciones) shift; condiciones "$@" ;;
    fijar-vms) shift; fijar_vms "$@" ;;
    restaurar) restaurar ;;
    *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
