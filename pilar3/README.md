# Pilar 3 — Despliegue, pruebas y escalabilidad

> **Desplegado** en el proyecto `chimera-blockchain` (org `distribuidos-chimera`). Sistema en `http://<IP de Traefik>`
> y estado de cada servicio en `/salud/<servicio>`; los mineros entran por `http://<IP del gateway>:8081`.
> Sin las variables `GCP_*` en el repo los pipelines sólo validan (`fmt` y `validate`).

| Carpeta | Qué es | Pipeline |
|---|---|---|
| `bootstrap/` | Base de GCP: APIs, Artifact Registry, Secret Manager, Workload Identity Federation (BC-31) y la IP fija del gateway | A mano, una vez |
| `infra/` | VPC y GKE zonal con dos nodegroups, `infra` (con taint) y `apps` (BC-32) | 1 · `pilar3-cluster.yml` |
| `helm/`, `k8s/servicios-base/` | Traefik, KEDA, RabbitMQ ×3, Redis ×3 y la observabilidad en el nodegroup de infra (BC-33, BC-37) | 2 · `pilar3-servicios-base.yml` |
| `k8s/apps/` | frontend, backend, nct, trp, gateway y escalador en el nodegroup de apps, y `/salud/<servicio>` (BC-34) | 3 · `pilar3-apps.yml` |
| `vms/` | Plantilla de las VMs minero-cpu, firewall y permisos (BC-35) | 4 · `pilar3-vms.yml` |
| `vms/instancias/` | Las VMs `minero-cpu-0…N-1` (BC-35) | Ninguno: lo aplica el escalador |
| [`pruebas/`](pruebas/README.md) | Pruebas de carga (BC-38 a BC-42) y cómo dejar GKE en sus condiciones | A mano: `pilar3-preparar-prueba.yml` |

Cada módulo tiene `provider.tf`, `variables.tf`, `main.tf` y `outputs.tf`. El state va a un bucket de GCS (`<proyecto>-tfstate`), con lock y con versionado. Nunca va al repo.

## Base de GCP (una vez, a mano)

El pipeline necesita el bucket y WIF para entrar a GCP, así que estos dos se crean desde una PC con `gcloud auth application-default login`. No se usan claves de cuenta de servicio.

```bash
PROYECTO=<id-del-proyecto>
gcloud storage buckets create gs://$PROYECTO-tfstate --project $PROYECTO --location us-central1 \
  --uniform-bucket-level-access
gcloud storage buckets update gs://$PROYECTO-tfstate --versioning

cd pilar3/bootstrap
tofu init -backend-config="bucket=$PROYECTO-tfstate"
tofu apply -var proyecto=$PROYECTO

# El valor del MINER_TOKEN va directo a Secret Manager (así no queda en el state)
printf '%s' "$MINER_TOKEN" | gcloud secrets versions add miner-token --data-file=- --project $PROYECTO

# Las salidas se cargan como variables del repo (no son secretos)
for v in GCP_PROYECTO GCP_WIF_PROVEEDOR GCP_CUENTA_CI GCP_REGISTRO GATEWAY_URL; do
  gh variable set $v --body "$(tofu output -raw $v)"
done
```

El proveedor OIDC sólo acepta tokens de `chimera-sdypp/BlockChain-Distribuida`. El pipeline recibe un token de GCP que vence en 1 h.

## Pipeline 1 · Cluster

- **En un PR:** gitleaks, `tofu fmt`, `validate` y `plan`. El plan queda comentado en el PR.
- **Al mergear a `main`:** `apply`.
- **Nodegroups:** `infra` tiene 1 nodo y taint `nodegroup=infra:NoSchedule`; `apps`, 1 nodo (los requests de cada uno suman ~0,5 vCPU). Las máquinas son `e2-standard-2` **spot** con disco estándar de 30 GB: si GCP se lleva un nodo, GKE crea otro y los pods vuelven.
- **Cuentas de servicio:**
  - La de los nodos (`gke-nodos`) baja las imágenes de Artifact Registry, así que no hacen falta pull secrets.
  - La del escalador entra a GCP desde el ServiceAccount `blockchain/escalador` por Workload Identity. Los permisos sobre las VMs se los da `vms/` (BC-35).
- **Costo estimado:** ≈ USD 0,10/h (≈ USD 2,5/día): 2 nodos spot, los 2 LoadBalancer (Traefik y gateway, lo que más pesa) y los discos. El cluster zonal no paga la administración. **Cobra desde que existe:** al terminar, `tofu destroy`.

Para conectarse: `tofu output -raw conectarse`.

## Pipeline 2 · Servicios base

- **En un PR:** gitleaks, `helm template` de todos los charts y `kubectl kustomize` de RabbitMQ, Redis y el monitoreo.
- **Al mergear a `main`** (o a mano, con *Run workflow*): los instala en GKE y espera a que estén listos.
- **Traefik** (chart 42.3.2) es la clase de Ingress por defecto, con 2 réplicas. Su LoadBalancer es la IP pública del sistema, en el puerto 80. El balanceador HTTP de GKE está desactivado en `infra/`.
- **KEDA** usa el chart 2.21.0, el mismo que en k3s.
- **RabbitMQ y Redis** usan los manifiestos del Pilar 2 sin cambios: [`servicios-base/`](k8s/servicios-base/kustomization.yaml) sólo les agrega el nodeSelector y la toleration de infra. Sus Services son internos.
- **El Secret `blockchain-secretos`** sale de los secretos del ambiente `produccion` de GitHub: `RABBIT_USUARIO`, `RABBIT_CLAVE`, `RABBIT_COOKIE`, `REDIS_CLAVE` y `MINER_TOKEN`. El `MINER_TOKEN` tiene que ser el mismo que el de Secret Manager, porque es el que leen las VMs.
- **Observabilidad (BC-37):** los mismos charts y values que en k3s ([pilar2](../pilar2/README.md)), más [`monitoreo.yaml`](helm/monitoreo.yaml) y [`loki.yaml`](helm/loki.yaml), que los mandan al nodegroup de infra. La contraseña de Grafana sale del secreto `GRAFANA_CLAVE` del ambiente. Se entra por port-forward: `kubectl -n monitoring port-forward svc/monitoreo-grafana 3000:80`.

## Pipeline 3 · Apps

- **En un PR:** gitleaks y, por cada app que cambió, sus tests con RabbitMQ y Redis reales, y el build de la imagen. Además se valida `k8s/apps/`.
- **Al mergear a `main`:**
  - push a Artifact Registry (tag = sha) de las apps que cambiaron;
  - un solo apply de [`k8s/apps/`](k8s/apps/kustomization.yaml): las que cambiaron usan la imagen nueva y las demás, la que ya corre. Así el rolling update es sólo de lo que cambió.
  - **La primera vez** se corre a mano (*Run workflow*) con «todas».
- **Los manifiestos** son los del Pilar 2, con el nodeSelector de apps. Los nodos bajan las imágenes con su cuenta de servicio, sin pull secrets.
- **El escalador** corre con VMs de verdad:
  - el módulo `vms/instancias/` entra como ConfigMap y un initContainer lo copia a `/tofu`;
  - el proyecto y el bucket salen de [`gcp.env`](k8s/apps/gcp.env), que completa el pipeline;
  - Workload Identity se configura con la anotación del ServiceAccount.

  KEDA lo prende y lo apaga según el Prometheus del Pipeline 2 (0 pods en reposo).
- **Health-check público:** `http://<IP de Traefik>/salud/<servicio>` para `backend`, `frontend`, `nct`, `trp` y `gateway`. Devuelve el `/health` del servicio: `{servicio: estado, rabbitmq: …, redis: …}`, y 503 si algo está caído. El escalador no tiene, porque apagado es su estado normal.
- La imagen del minero GPU no va en este pipeline: la publica `ci.yml` en GHCR.

## Pipeline 4 · VMs minero-cpu

- **En un PR:** gitleaks, build de la imagen del minero CPU, `fmt`, `validate` (también de `instancias/`) y `plan` comentado.
- **Al mergear a `main`:** push de la imagen a Artifact Registry (tag = sha) y `apply` de la plantilla. Necesita la variable `GATEWAY_URL`, la IP pública del gateway (`http://<ip>:8081`).
- **La VM:** Container-Optimized OS con un startup-script ([`arrancar.sh`](vms/arrancar.sh)) que:
  1. lee el `MINER_TOKEN` de Secret Manager con la cuenta de la VM (queda en `/run`, que está en RAM);
  2. baja la imagen con esa misma cuenta;
  3. arranca el minero con `MINERO_ID` = nombre de la VM.

  La VM no tiene puertos de entrada: el minero sólo sale hacia el gateway.
- **Dos states:** la plantilla (`vms`) la aplica el pipeline y las instancias (`vms-instancias`) las aplica el escalador. Si fueran uno solo, un apply del pipeline volvería a `cantidad = 0` y borraría las VMs del escalador.
- **Cada imagen nueva crea una plantilla nueva.** Las VMs que ya estaban se recrean con ella en el próximo apply del escalador.

### El escalador con VMs de verdad

Lo arma `k8s/apps/` (Pipeline 3). El pod necesita:

| Variable | Valor |
|---|---|
| `VMS_ACTIVAS` | `1` |
| `TOFU_DIR` | `/tofu` (ahí copia el módulo el initContainer) |
| `TF_CLI_ARGS_init` | `-backend-config=bucket=<proyecto>-tfstate` |
| `TF_VAR_proyecto` | el proyecto |

A mano, desde `pilar3/vms/instancias/`: `tofu apply -var proyecto=$PROYECTO -var cantidad=1` crea `minero-cpu-0`, y con `cantidad=0` se borra.

> **Quotas del proyecto:** 32 vCPU en todas las regiones y **8 IPs públicas en us-central1**. Las IPs son el límite: 2 nodos + 2 LoadBalancer usan 4, y cada VM minera (e2-highcpu-2 spot) usa una, así que `VMS_MAXIMO=3` entra.
