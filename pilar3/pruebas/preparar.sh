#!/usr/bin/env bash
# Prepara GKE para las pruebas de carga del Pilar 3 (BC-38 a BC-41) y después lo deja como estaba.
# Lo corre el workflow «Pilar 3 · Preparar prueba» (.github/workflows/pilar3-preparar-prueba.yml);
# también anda desde una PC con kubectl apuntando al cluster.
#
#   preparar.sh estado                 IP pública, altura, saldos, mineros y condiciones actuales
#   preparar.sh reiniciar              BORRA LA CADENA: vacía Redis y las colas; el NCT carga el génesis
#                                      de nuevo (en GKE vacío, BC-91: después, `cuentas.py fondear`)
#   preparar.sh cuentas [N]            el Secret cuentas-prueba con N cuentas para las pruebas de carga (3);
#                                      no pisa uno que existe. Las fondea el organizador (cuentas.py fondear)
#   preparar.sh condiciones P A F [C]  TrP con PREFIJO_BASE=P, AJUSTAR_PREFIJO=A (0 o 1),
#                                      FRAGMENTO_PORCENTAJE=F y, si viene, CRITERIO_POTENCIA=C (suma,
#                                      promedio, minimo o maximo; BC-59). El escalador, con el mismo PREFIJO_BASE
#   preparar.sh fijar-vms N            pausa el escalador en KEDA y deja N VMs minero-cpu, cada una con su
#                                      clave (la genera la VM) aprobada (como el escalador, BC-57 y BC-95)
#                                      (necesita tofu, PROYECTO y credenciales de GCP: fuera del workflow,
#                                      GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token))
#   preparar.sh restaurar              TrP y escalador como en los manifiestos, y el escalador de nuevo
#                                      en manos de KEDA (la cadena reiniciada no vuelve)
set -euo pipefail

NS=${NS:-blockchain}
RAIZ=$(cd "$(dirname "$0")/../.." && pwd)
COLAS="transacciones tareas rangos resultados soluciones"

k() { kubectl -n "$NS" "$@"; }

ip_publica() {
    kubectl -n traefik get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
}

# La URL del sistema: en GKE el Ingress tiene HTTPS con nombre (y redirige el HTTP); en k3s, http://<IP>.
url_publica() {
    local nombre
    nombre=$(k get ingress blockchain -o jsonpath='{.spec.tls[0].hosts[0]}' 2>/dev/null || true)
    if [ -n "$nombre" ]; then echo "https://$nombre"; else echo "http://$(ip_publica)"; fi
}

# El gateway, que lista los mineros (GET /mineros; salieron de la API de la app, BC-54): GATEWAY_URL o su LoadBalancer.
url_gateway() {
    if [ -n "${GATEWAY_URL:-}" ]; then echo "${GATEWAY_URL%/}"; return; fi
    echo "http://$(k get svc gateway -o jsonpath='{.status.loadBalancer.ingress[0].ip}'):8081"
}

# Valor de una variable de entorno del contenedor principal de un Deployment (vacío si no está).
env_de() {
    k get deploy "$1" -o jsonpath="{.spec.template.spec.containers[?(@.name=='$1')].env[?(@.name=='$2')].value}"
}

mineros_cpu_vivos() {   # cuántas VMs nuestras (alias minero-cpu-*) avisaron hace poco
    curl -fsS --max-time 10 "$1/mineros" | python3 -c '
import json, sys
print(sum(1 for m in json.load(sys.stdin) if m.get("vivo") and str(m.get("alias") or "").startswith("minero-cpu-")))'
}

# Lo mismo que hace el escalador (ClavesVMs, BC-57, BC-79 y BC-95) después del apply: cada VM minero-cpu-0..N-1
# genera su clave al arrancar y publica la pública en sus guest attributes (minero/publica); acá se leen con gcloud y,
# para las que no están en la cadena, se firma un alta delegada con la clave del escalador (necesita su permiso:
# `python -m claves permiso`). Las de las VMs que sobran (o una clave vieja de una VM recreada), una baja delegada.
# Firma en un pod del TrP, que tiene el contrato, Redis y llega al backend. La clave del escalador sale del Secret
# escalador-clave y entra por stdin: no queda en los argumentos (ni en el registro de auditoría de kubectl exec).
ALTAS_VMS=$(cat <<'PYTHON'
import json, sys, time, urllib.request
from comun import config, contrato, redisdb

PREFIJO, BACKEND = "minero-cpu-", "http://backend:8080"
cantidad, publicas = int(sys.argv[1]), json.loads(sys.argv[2])
privada = sys.stdin.readline().strip()
redis = redisdb.conectar(config.cargar("preparar"))
escalador = contrato.clave_publica(privada)
if redis.hget(contrato.PERMISOS, escalador) is None:
    sys.exit("El escalador no tiene permiso en la cadena: ver pilar2/escalador/README.md")


def pedir(metodo, ruta, cuerpo=None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    pedido = urllib.request.Request(BACKEND + ruta, data=datos, method=metodo, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(pedido, timeout=10) as respuesta:
        return json.loads(respuesta.read() or b"null")


cadena = pedir("GET", "/api/cadena")["genesis"]
nonce = pedir("GET", f"/api/cuentas/{escalador}")["nonce"]


def acta(tipo, cuenta, datos):
    global nonce
    nonce += 1
    pedir("POST", "/api/transacciones", contrato.acta(privada, tipo, nonce, cuenta, datos, int(time.time()),
                                                       delegada=True, cadena=cadena))
    print(f"{tipo} {datos}: {cuenta}")


propios = {}
for cuenta, datos in redis.hgetall(contrato.CUENTAS).items():
    datos = json.loads(datos)
    nombre = str(datos.get("nombre", ""))
    if datos.get("tipo") == "minero" and nombre.startswith(PREFIJO) and nombre[len(PREFIJO):].isdigit():
        propios.setdefault(nombre, []).append(cuenta)
for nombre, publica in publicas.items():
    for vieja in propios.get(nombre, []):
        if vieja != publica:
            acta("baja", vieja, "la VM se recreó con otra clave")
    if not any(redis.hexists(clave, publica) for clave in (contrato.CUENTAS, contrato.ALTAS_EN_CAMINO, contrato.VETADAS)):
        acta("alta", publica, f"minero:{nombre}")
for nombre, cuentas in propios.items():
    if int(nombre[len(PREFIJO):]) >= cantidad:
        for cuenta in cuentas:
            acta("baja", cuenta, "la VM se borró")
PYTHON
)

altas_vms() {
    local cantidad=$1 publicas="{}" n i nombre publica
    for ((n = 0; n < cantidad; n++)); do
        nombre="minero-cpu-$n"
        for i in $(seq 1 60); do
            publica=$(gcloud compute instances get-guest-attributes "$nombre" --project "$PROYECTO" \
                --zone "${ZONA:-us-central1-a}" --query-path=minero/publica --format='value(value)' 2>/dev/null || true)
            [[ $publica =~ ^[0-9a-f]{64}$ ]] && break
            sleep 10
        done
        [[ $publica =~ ^[0-9a-f]{64}$ ]] || { echo "::error::$nombre no publicó su clave en 10 minutos"; exit 1; }
        publicas=$(python3 -c 'import json, sys; d = json.loads(sys.argv[1]); d[sys.argv[2]] = sys.argv[3]; print(json.dumps(d))' \
            "$publicas" "$nombre" "$publica")
    done
    k get secret escalador-clave -o jsonpath='{.data.ESCALADOR_CLAVE}' | base64 -d | \
        k exec -i deploy/trp -- python -c "$ALTAS_VMS" "$cantidad" "$publicas"
}

# «nombre pública» de cada cuenta del Secret cuentas-prueba (BC-91), o nada si no está. La pública la deriva un pod
# del TrP, que tiene el contrato; la privada entra por stdin y no sale del pod.
cuentas_prueba() {
    k get secret cuentas-prueba -o json 2>/dev/null | k exec -i deploy/trp -- python -c '
import base64, json, sys
from comun import contrato
for nombre, valor in sorted((json.load(sys.stdin).get("data") or {}).items()):
    print(nombre, contrato.clave_publica(base64.b64decode(valor).decode().strip()))' 2>/dev/null || true
}

estado() {
    local base
    base=$(url_publica)
    echo "Sistema:   $base   (para correr.py: --url $base)"
    echo "TrP:       PREFIJO_BASE=$(env_de trp PREFIJO_BASE) AJUSTAR_PREFIJO=$(env_de trp AJUSTAR_PREFIJO) FRAGMENTO_PORCENTAJE=$(env_de trp FRAGMENTO_PORCENTAJE) CRITERIO_POTENCIA=$(env_de trp CRITERIO_POTENCIA)   (vacío: el valor por defecto del TrP)"
    echo "Escalador: pausado en KEDA=$(k get scaledobject escalador -o jsonpath='{.metadata.annotations.autoscaling\.keda\.sh/paused-replicas}' 2>/dev/null || true)   (vacío: lo maneja KEDA)"
    echo "NCT:       SALDOS_INICIALES=$(env_de nct SALDOS_INICIALES)   (en GKE «{}»: el génesis va vacío, BC-91)"
    python3 - "$base" "$(url_gateway)" "$(cuentas_prueba)" <<'EOF'
import json, sys, urllib.request

base, gateway, cuentas = sys.argv[1], sys.argv[2], [l.split() for l in sys.argv[3].splitlines() if l.strip()]
def get(ruta, de=None):
    with urllib.request.urlopen((de or base) + ruta, timeout=10) as r:
        return json.load(r)

try:
    punta = get("/api/bloques?limite=1")
    saldos = get("/api/saldos")
    mineros = get("/mineros", gateway)
except OSError as error:
    sys.exit(f"No responden {base} o {gateway}: {error}")
if punta:
    print(f"Cadena:    altura {punta[0]['altura']}, prefijo del último bloque «{punta[0].get('prefijo', '')}»")
if cuentas:
    print("Cuentas:   " + ", ".join(f"{nombre}={saldos.get(publica, 0)}" for nombre, publica in cuentas)
          + "   (las del Secret cuentas-prueba; sin saldo: cuentas.py fondear)")
else:
    print("Cuentas:   no hay Secret cuentas-prueba (preparar.sh cuentas)")
vivos = [m for m in mineros if m.get("vivo")]
print(f"Mineros:   {sum(m.get('tipo') == 'gpu' for m in vivos)} GPU y {sum(m.get('tipo') == 'cpu' for m in vivos)} CPU vivos")
for m in sorted(vivos, key=lambda m: (m.get("tipo", ""), m.get("id", ""))):
    print(f"           - {m.get('tipo')}: {m.get('id')}")
EOF
}

reiniciar() {
    local replicas_nct replicas_trp replicas_nodo primario
    echo "SALDOS_INICIALES del NCT: $(env_de nct SALDOS_INICIALES)   (en GKE «{}»: el génesis va vacío, BC-91)"

    replicas_nct=$(k get deploy nct -o jsonpath='{.spec.replicas}')
    replicas_trp=$(k get deploy trp -o jsonpath='{.spec.replicas}')
    echo "Apagando el NCT ($replicas_nct) y el TrP ($replicas_trp), para que nadie escriba en el medio"
    k scale deploy nct trp --replicas=0
    k wait --for=delete pod -l app=nct --timeout=180s
    k wait --for=delete pod -l app=trp --timeout=180s
    # Los nodos (BC-83) guardan su copia de la cadena: con la vieja verían la nueva como una historia reescrita.
    replicas_nodo=$(k get sts nodo -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)
    if [ "$replicas_nodo" != 0 ]; then
        echo "Apagando los nodos ($replicas_nodo) y borrando su copia de la cadena"
        k scale sts nodo --replicas=0
        k wait --for=delete pod -l app=nodo --timeout=180s
        k delete pvc -l app=nodo --wait=true
    fi

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
    if [ "$replicas_nodo" != 0 ]; then
        k scale sts nodo --replicas="$replicas_nodo"
        k rollout status sts/nodo --timeout=5m
    fi
    echo "Génesis vacío: las cuentas de las pruebas (Secret cuentas-prueba) necesitan alta y saldo de nuevo:"
    echo "  pilar2/.venv/bin/python pilar3/pruebas/cuentas.py fondear organizador.clave --url $(url_publica)"
    # BC-95: la clave del escalador está en el Secret escalador-clave (no en Redis): sobrevive al FLUSHALL, pero su
    # permiso estaba en la cadena vieja. El organizador se lo vuelve a dar con `python -m claves permiso`.
    if k get secret escalador-clave > /dev/null 2>&1; then
        local escalador
        escalador=$(k get secret escalador-clave -o jsonpath='{.data.ESCALADOR_CLAVE}' | base64 -d | \
            k exec -i deploy/trp -- python -c 'import sys; from comun import contrato; print(contrato.clave_publica(sys.stdin.readline().strip()))')
        echo "Clave del escalador (le falta el permiso del organizador en la cadena nueva): $escalador"
    fi
}

condiciones() {
    local prefijo=${1:?falta PREFIJO_BASE} ajustar=${2:?falta AJUSTAR_PREFIJO} fragmento=${3:?falta FRAGMENTO_PORCENTAJE}
    [[ $prefijo =~ ^[0-9]+$ ]] && ((prefijo >= 1 && prefijo <= 32)) || { echo "::error::PREFIJO_BASE va de 1 a 32"; exit 1; }
    [[ $ajustar == 0 || $ajustar == 1 ]] || { echo "::error::AJUSTAR_PREFIJO es 0 o 1"; exit 1; }
    local criterio=${4:-}
    [[ $fragmento =~ ^[0-9]+$ ]] && ((fragmento >= 1 && fragmento <= 50)) || { echo "::error::FRAGMENTO_PORCENTAJE va de 1 a 50"; exit 1; }
    [[ -z $criterio || $criterio =~ ^(suma|promedio|minimo|maximo)$ ]] || { echo "::error::CRITERIO_POTENCIA es suma, promedio, minimo o maximo"; exit 1; }
    # BC-73: el TrP no arranca si el espacio de nonces no alcanza para el prefijo (16^P × 4, como mínimo 2^32).
    local espacio
    espacio=$(python3 -c "print(max(2**32, 16**$prefijo * 4))")
    k set env deploy/trp PREFIJO_BASE="$prefijo" AJUSTAR_PREFIJO="$ajustar" FRAGMENTO_PORCENTAJE="$fragmento" \
        ESPACIO_NONCES="$espacio" ${criterio:+CRITERIO_POTENCIA="$criterio"}
    # El escalador calcula las VMs con el prefijo del sistema: el mismo que el TrP.
    k set env deploy/escalador PREFIJO_BASE="$prefijo"
    # Con el prefijo fijo (A=0), la bajada de emergencia del NCT no tiene que cortar las tareas largas.
    if [ "$ajustar" = 0 ]; then k set env deploy/nct EMERGENCIA_SEGUNDOS=0; else k set env deploy/nct EMERGENCIA_SEGUNDOS-; fi
    k rollout status deploy/trp --timeout=5m
    k rollout status deploy/nct --timeout=5m
}

fijar_vms() {
    local cantidad=${1:?falta la cantidad de VMs} url i
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
    # BC-95: sin claves en el apply ni en el state. Cada VM genera la suya; después se firman las altas.
    TF_VAR_proyecto=$PROYECTO tofu -chdir="$RAIZ/pilar3/vms/instancias" apply -input=false -auto-approve -var "cantidad=$cantidad"
    altas_vms "$cantidad"

    url=$(url_gateway)
    echo "Esperando $cantidad minero(s) CPU vivo(s) (las VMs tardan unos minutos en arrancar)"
    for i in $(seq 1 60); do
        [ "$(mineros_cpu_vivos "$url")" -ge "$cantidad" ] && { echo "Listo: $cantidad VM(s)"; return; }
        sleep 10
    done
    echo "::warning::A los 10 minutos hay $(mineros_cpu_vivos "$url") de $cantidad mineros CPU vivos"
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
    for despliegue in trp escalador nct; do
        variables="PREFIJO_BASE AJUSTAR_PREFIJO FRAGMENTO_PORCENTAJE CRITERIO_POTENCIA ESPACIO_NONCES"
        [ "$despliegue" = escalador ] && variables="PREFIJO_BASE"
        [ "$despliegue" = nct ] && variables="EMERGENCIA_SEGUNDOS"
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
    cuentas) python3 "$RAIZ/pilar3/pruebas/cuentas.py" generar "${2:-3}" ;;
    condiciones) shift; condiciones "$@" ;;
    fijar-vms) shift; fijar_vms "$@" ;;
    restaurar) restaurar ;;
    *) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
