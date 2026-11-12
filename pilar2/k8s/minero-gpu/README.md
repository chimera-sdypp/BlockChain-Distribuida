# Mineros GPU en el K3s de la cátedra (BC-36)

El [minero GPU](../../minero_gpu/) corre en el cluster K3s de la cátedra, que tiene las GPU, y le habla al gateway por su IP pública. Va aparte de `k8s/kustomization.yaml` porque es otro cluster.

| Archivo | Qué hace |
|---|---|
| `minero-gpu.yaml` | El Deployment: una GPU (`nvidia.com/gpu: 1`) por pod; el id de cada minero es el nombre del pod |
| `desplegar.sh` | Crea o actualiza el Secret con el `MINER_TOKEN`, aplica el Deployment con la URL del gateway y la cantidad de réplicas, y espera a que estén listos |
| `verificar.sh` | Lista los mineros que ve el gateway (`/api/mineros` del backend) y comprueba cuántos GPU están vivos |

## 1. Kubeconfig del grupo

```bash
chmod 600 ~/.kube/chimera-catedra.yaml          # el que da la cátedra; nunca al repo (.gitignore: kubeconfig*)
export KUBECONFIG=~/.kube/chimera-catedra.yaml
export NAMESPACE=<namespace del grupo>
kubectl auth can-i create deployments -n "$NAMESPACE"      # tiene que decir yes
kubectl describe resourcequota -n "$NAMESPACE"             # cuántas GPU, CPU y memoria nos tocan
```

## 2. Desplegar

```bash
export GATEWAY_URL=http://<IP pública del gateway>:8081   # en GKE: la del LoadBalancer del gateway
export MINER_TOKEN=...                                    # el mismo que tiene el gateway
pilar2/k8s/minero-gpu/desplegar.sh 1                      # 1 réplica = 1 GPU
```

- El token va del entorno al Secret sin imprimirse. Si se cambia, hay que volver a correr `desplegar.sh` y `kubectl -n "$NAMESPACE" rollout restart deployment/minero-gpu`: el pod lee el Secret solo al arrancar.
- `IMAGEN=ghcr.io/chimera-sdypp/minero-gpu:<tag>` elige la imagen. Por defecto se usa `:dev`, que la CI publica al mergear a `dev`; `:main` aparece recién cuando se mergea a `main`. La imagen es pública, así que no hace falta `imagePullSecret`.
- `DRY_RUN=1` muestra los manifiestos (con el token tapado) sin tocar el cluster.

## 3. Ver que minan

```bash
kubectl -n "$NAMESPACE" logs deploy/minero-gpu --tail=20        # bench, rangos tomados, bloques resueltos
BACKEND_URL=http://<IP del Ingress> pilar2/k8s/minero-gpu/verificar.sh
```

`verificar.sh` lee `nodo:<id>` a través del backend: cada pod tiene que aparecer como `gpu`, vivo y con su bench. Termina con código 0 si hay al menos un GPU vivo (o los que se le pidan: `verificar.sh 2`).

## 4. Entrada y salida de GPU (lo usa BC-41)

Que entre o salga una GPU es cambiar las réplicas:

```bash
kubectl -n "$NAMESPACE" scale deployment minero-gpu --replicas=0    # salen todas las GPU
BACKEND_URL=... pilar2/k8s/minero-gpu/verificar.sh 0                 # código 0 cuando ya no queda ninguna viva
kubectl -n "$NAMESPACE" scale deployment minero-gpu --replicas=1    # vuelve una
```

Al bajar a 0, el keep-alive de cada minero vence a los 15 s y el gateway deja de contarlo. Eso se ve en `nodo:*`, en la alerta SinMinerosGPU y en el prefijo que elige el TrP. Con el sistema en GKE, si hay trabajo apilado, KEDA prende el escalador y aparece una VM CPU (BC-29).

## Desde GitHub Actions

Si la cátedra deja llegar al API del cluster desde afuera, el workflow **Minero GPU · K3s de la cátedra** (`.github/workflows/minero-gpu-catedra.yml`) corre `desplegar.sh`. Se lanza solo a mano (Actions → Run workflow), con las réplicas y el tag de la imagen. Usa el ambiente `catedra`:

- **Secrets:** `KUBECONFIG_CATEDRA` (el contenido del kubeconfig) y `MINER_TOKEN`.
- **Variables:** `CATEDRA_NAMESPACE` y `GATEWAY_URL`.

## Si algo falla

| Síntoma | Qué mirar |
|---|---|
| `ImagePullBackOff` | El tag no existe en GHCR (por ejemplo `:main` antes del primer merge a `main`). Usar `IMAGEN=...:dev` o un `:<sha>` publicado |
| Pod `Pending` | `kubectl describe pod`: no hay GPU libres o se pasa de la cuota del namespace. Bajar las réplicas |
| El minero arranca pero no encuentra la GPU | En algunos K3s el pod necesita `runtimeClassName: nvidia`; consultar con la cátedra cómo están configurados sus nodos |
| `el gateway rechazó el MINER_TOKEN` en los logs | El Secret no coincide con el token del gateway: volver a desplegar con el correcto y hacer `rollout restart` |
| No aparece en `verificar.sh` | El pod no llega al gateway: probar `GATEWAY_URL` desde adentro (`kubectl run -it --rm prueba --image=curlimages/curl -- curl -s $GATEWAY_URL/health`) |

## Quitarlo

```bash
kubectl -n "$NAMESPACE" delete deployment/minero-gpu secret/minero-gpu
```
