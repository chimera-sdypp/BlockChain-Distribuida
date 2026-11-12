# Mineros GPU en el k3s de una PC con NVIDIA (BC-36)

El [minero GPU](../../minero_gpu/) corre en el k3s de la PC de un integrante (RTX 4060) y le habla al gateway de GKE por su IP pública. Va aparte de `k8s/kustomization.yaml` porque es otro cluster.

| Archivo | Qué hace |
|---|---|
| `minero-gpu.yaml` | El Deployment: una GPU (`nvidia.com/gpu: 1`) por pod, con `runtimeClassName: nvidia`; el id de cada minero es el nombre del pod |
| `desplegar.sh` | Crea el namespace y el Secret con el `MINER_TOKEN`, aplica el Deployment con la URL del gateway y la cantidad de réplicas, y espera a que estén listos |
| `verificar.sh` | Lista los mineros que ve el gateway (`/api/mineros` del backend) y comprueba cuántos GPU están vivos |

## 1. Preparar el k3s (una vez)

```bash
sudo pacman -S nvidia-container-toolkit     # en otras distros, el paquete de NVIDIA
sudo systemctl restart k3s                  # al arrancar, k3s detecta el runtime y crea la RuntimeClass nvidia
kubectl label node <nodo> nvidia.com/gpu.present=true   # el chart sólo corre en nodos con esta etiqueta
helm upgrade --install nvidia-device-plugin nvidia-device-plugin \
  --repo https://nvidia.github.io/k8s-device-plugin --version 0.17.1 \
  -n nvidia-device-plugin --create-namespace --set runtimeClassName=nvidia
kubectl get nodes -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}'   # tiene que dar 1
```

El device plugin es el que le dice a Kubernetes que el nodo tiene una GPU (`nvidia.com/gpu`). Sin él, el pod queda `Pending`.

## 2. Desplegar

```bash
export KUBECONFIG=~/.kube/config
export GATEWAY_URL=http://<IP pública del gateway>:8081 GCP_PROYECTO=<id del proyecto>
pilar2/k8s/minero-gpu/desplegar.sh 1
```

- El token sale de Secret Manager con `gcloud` (o de `MINER_TOKEN`, si está) y va al Secret sin imprimirse. Si cambia, hay que volver a correr `desplegar.sh` y `kubectl -n mineros-gpu rollout restart deployment/minero-gpu`.
- `IMAGEN=ghcr.io/chimera-sdypp/minero-gpu:<tag>` elige la imagen (por defecto `:main`). Es pública: no hace falta `imagePullSecret`.
- `NAMESPACE` cambia el namespace (por defecto `mineros-gpu`). `DRY_RUN=1` muestra los manifiestos (con el token tapado) sin tocar el cluster.

## 3. Ver que minan

```bash
kubectl -n mineros-gpu logs deploy/minero-gpu --tail=20         # bench, rangos tomados, bloques resueltos
BACKEND_URL=http://<IP de Traefik> pilar2/k8s/minero-gpu/verificar.sh
```

Cada pod tiene que aparecer como `gpu`, vivo y con su bench. `verificar.sh` termina con código 0 si hay al menos un GPU vivo (o los que se le pidan: `verificar.sh 2`).

## 4. Entrada y salida de GPU (lo usa BC-41)

Que entre o salga una GPU es cambiar las réplicas:

```bash
kubectl -n mineros-gpu scale deployment minero-gpu --replicas=0    # sale la GPU
BACKEND_URL=... pilar2/k8s/minero-gpu/verificar.sh 0                # código 0 cuando ya no queda ninguna viva
kubectl -n mineros-gpu scale deployment minero-gpu --replicas=1    # vuelve
```

Al bajar a 0, el keep-alive vence a los 15 s y el gateway deja de contarlo. Se ve en `nodo:*`, en la alerta SinMinerosGPU y en el prefijo que elige el TrP. Si hay trabajo apilado, KEDA prende el escalador en GKE y aparece una VM CPU (BC-29).

## Si algo falla

| Síntoma | Qué mirar |
|---|---|
| `desplegar.sh` dice que ningún nodo ofrece `nvidia.com/gpu` | Falta el toolkit, reiniciar k3s después de instalarlo, o el device plugin (paso 1) |
| Pod `Pending` | `kubectl describe pod`: la GPU ya la tiene otro pod. Con una sola GPU, una réplica |
| `ImagePullBackOff` | El tag no existe en GHCR. Usar `IMAGEN=...:dev` o un `:<sha>` publicado |
| `el gateway rechazó el MINER_TOKEN` en los logs | El Secret no coincide con el token del gateway: volver a desplegar y hacer `rollout restart` |
| No aparece en `verificar.sh` | El pod no llega al gateway: `kubectl run -it --rm prueba --image=curlimages/curl -- curl -s $GATEWAY_URL/health` |

## Quitarlo

```bash
kubectl delete namespace mineros-gpu
```
