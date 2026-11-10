# Escalador (BC-29)

Cuando no hay GPU y se apila trabajo, levanta VMs con mineros CPU con OpenTofu; cuando vuelve la GPU o no queda trabajo, las borra. **No está siempre prendido:** es un Deployment de 0 o 1 pod que prende y apaga KEDA.

```
Prometheus ──► KEDA ──► Deployment escalador (0 ↔ 1)
                              │ cada 10 s: nodo:*, tx:pendientes, cajas (Redis)
                              └► tofu apply -var cantidad=N ──► VMs minero-cpu-0…N-1
```

## KEDA (`k8s/escalador/escalador.yaml`)

El ScaledObject lo prende si alguna de estas consultas a Prometheus da más de 0, y lo apaga 180 s después de que den 0 todas:

| Trigger | Consulta | De dónde sale |
|---|---|---|
| Rangos sin tomar | `max(rabbitmq_queue_messages{queue="rangos"})` | RabbitMQ (`/metrics/per-object`) |
| Transacciones en cajas | `max(blockchain_tx_en_cajas)` | NCT |
| Mineros CPU vivos | `max(blockchain_mineros{tipo="cpu"})` | Gateway |

El tercero existe para que el escalador siga vivo hasta borrar sus VMs (sus mineros avisan al gateway): sin él, KEDA lo apagaría con las VMs prendidas. Si Prometheus se cae, el escalador no se prende.

## Cuántas VMs

- **Objetivo:** que, sin GPU, un bloque con el prefijo base salga en unos `SEGUNDOS_POR_BLOQUE`. Con p caracteres hacen falta 16^p intentos en promedio, así que la potencia objetivo es 16^p / T (prefijo 6 y 30 s: ~560 kH/s, alcanza una VM).
- Se descuenta lo que aportan los otros mineros CPU vivos, y se divide por el bench de las VMs vivas (si no hay ninguna, `HPS_POR_VM`). Tope: `VMS_MAXIMO`.
- Con una GPU viva o sin trabajo (`tx:pendientes` y `cajas` vacías): 0.
- **Sin oscilar:** sube recién tras `ESPERA_SUBIR_SEGUNDOS` seguidos sin GPU, y baja recién cuando el número menor se sostuvo `ESPERA_BAJAR_SEGUNDOS`.
- Nuestras VMs se reconocen por el `MINERO_ID`, que es su hostname: `minero-cpu-<n>`.

| Variable | Por defecto | Qué es |
|---|---|---|
| `VMS_ACTIVAS` | 0 | 1: corre OpenTofu. 0: anota la cantidad en `escalador:vms` (Redis), para probar KEDA sin GCP |
| `TOFU_DIR` | `/tofu` | El módulo de las instancias (BC-35): variable y output `cantidad`, state en el bucket |
| `VMS_MAXIMO` | 3 | Tope de VMs |
| `HPS_POR_VM` | 1000000 | Hashes/s supuestos de una VM mientras no haya una viva |
| `PREFIJO_BASE` / `SEGUNDOS_POR_BLOQUE` | 6 / 30 | Los mismos que el TrP |
| `ESPERA_SUBIR_SEGUNDOS` / `ESPERA_BAJAR_SEGUNDOS` | 30 / 120 | Ver «sin oscilar» |
| `CICLO_SEGUNDOS` | 10 | Cada cuánto mira |

- El apply es declarativo (`cantidad = N`): si KEDA lo reinicia y aplica dos veces lo mismo, no crea VMs de más. Al arrancar lee la cantidad del state (`tofu output`).
- Si un apply falla, la cantidad no cambia y se reintenta en el ciclo siguiente.
- Al recibir SIGTERM termina el ciclo en curso (`terminationGracePeriodSeconds: 600`), para no dejar tomado el lock del state.
- Métricas: `blockchain_vms_cpu`, `blockchain_escalador_aplicados` y `blockchain_escalador_fallidos`.

## En k3s

Con RabbitMQ, Redis y los servicios ya desplegados (`kubectl apply -k k8s`):

```bash
helm install monitoreo prometheus-community/kube-prometheus-stack --version 92.13.1 \
  -n monitoring --create-namespace -f k8s/monitoreo/valores-prometheus.yaml
helm install keda kedacore/keda --version 2.21.0 -n keda --create-namespace
kubectl apply -k k8s/monitoreo
docker build -f escalador/Dockerfile -t escalador:dev .   # e importarla en k3s
kubectl apply -k k8s/escalador
```

Probado el 09/11 con `VMS_ACTIVAS=0` y el minero CPU local haciendo de VM (`MINERO_ID=minero-cpu-0`):

| t | Qué pasó |
|---|---|
| 0 s | 3 transferencias; el escalador en 0 pods |
| 24 s | KEDA lo prende (rangos sin tomar y transacciones en cajas) |
| 45 s | Pide 1 VM (30 s sin GPU y con trabajo) |
| 56 s | El minero resuelve el bloque: no queda trabajo |
| 171 s | Baja a 0 VMs (120 s sin trabajo) |
| ~3,5 min después de parar el minero | KEDA lo apaga (15 s hasta darlo por caído + 180 s de cooldown) |
