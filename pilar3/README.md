# Pilar 3 — Despliegue, pruebas y escalabilidad

> **Falta la nube.** El código está, pero todavía no se aplicó en GCP: no hay proyecto, ni cluster, ni
> variables `GCP_*` en el repo. Sin esas variables los pipelines sólo validan (`fmt` y `validate`).

| Carpeta | Qué es | Pipeline |
|---|---|---|
| `bootstrap/` | Base de GCP: APIs, Artifact Registry, Secret Manager y Workload Identity Federation (BC-31) | A mano, una vez |
| `infra/` | VPC y GKE zonal con dos nodegroups, `infra` (con taint) y `apps` (BC-32) | 1 · `pilar3-cluster.yml` |

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
for v in GCP_PROYECTO GCP_WIF_PROVEEDOR GCP_CUENTA_CI GCP_REGISTRO; do
  gh variable set $v --body "$(tofu output -raw $v)"
done
```

El proveedor OIDC sólo acepta tokens de `chimera-sdypp/BlockChain-Distribuida`. El pipeline recibe un token de GCP que vence en 1 h.

## Pipeline 1 · Cluster

- **En un PR:** gitleaks, `tofu fmt`, `validate` y `plan`. El plan queda comentado en el PR.
- **Al mergear a `main`:** `apply`.
- **Nodegroups:** `infra` tiene 2 nodos y taint `nodegroup=infra:NoSchedule`; `apps`, 2 nodos. Las máquinas son `e2-standard-2` con disco estándar de 30 GB.
- **Cuentas de servicio:**
  - La de los nodos (`gke-nodos`) baja las imágenes de Artifact Registry, así que no hacen falta pull secrets.
  - La del escalador entra a GCP desde el ServiceAccount `blockchain/escalador` por Workload Identity. Los permisos sobre las VMs se los da `vms/` (BC-35).
- **Costo estimado:** 4 × e2-standard-2 ≈ USD 0,27/h (≈ USD 6,5/día), más los LoadBalancer. El cluster zonal no paga la administración. **Cobra desde que existe:** al terminar, `tofu destroy`.

Para conectarse: `tofu output -raw conectarse`.
