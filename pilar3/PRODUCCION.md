# Endurecimiento para ambiente productivo (BC-100)

Resumen técnico de los controles aplicados sobre Kubernetes y la infraestructura en GKE (`BC-101` a `BC-108`), junto con las limitaciones aceptadas por costo/quota para documentar en el informe final.

## 1. Cluster Autoscaler en GKE (`BC-101`)

Los *node pools* de GKE (`pilar3/infra/main.tf`) pasan de `node_count` fijo a `autoscaling { min_node_count, max_node_count }` con `initial_node_count`:

| Pool | Mínimo (`min_node_count`) | Máximo (`max_node_count`) | Máquina (`e2-standard-2`, spot) | Costo spot aprox. en máximo |
|---|---:|---:|---|---|
| `infra` | 1 | 2 | 2 vCPU, 8 GB RAM (~US\$0,022/h) | ~US\$1,06/día (2 nodos) |
| `apps` | 1 | 3 | 2 vCPU, 8 GB RAM (~US\$0,022/h) | ~US\$1,58/día (3 nodos) |

**Justificación de los topes (quota y costo):** el proyecto de GCP tiene quota de **32 vCPU** y **8 IPs públicas** en `us-central1`. Con 1 nodo `infra` + 1 nodo `apps` + 2 LoadBalancer (`traefik` y `gateway`) ya se consumen 4 IPs públicas y con hasta `VMS_MAXIMO = 3` VMs mineras CPU quedan 7 IPs en uso. Cada nodo GKE consume 1 IP pública y 2 vCPU: el máximo `2 + 3 = 5` nodos totales (10 vCPU) mantiene margen holgado de vCPU y permite escalar pods sin saturar la quota ni elevar el gasto diario (~US\$2,64/día en el tope combinado de spot).

## 2. HorizontalPodAutoscaler (`BC-102`)

Se agregan `HorizontalPodAutoscaler` (`autoscaling/v2`, métrica CPU al 70 % con `resources.requests` definidos en todos los contenedores):

- **`backend`**: `minReplicas: 2`, `maxReplicas: 6` (sin estado; recibe el tráfico de `/api` y lotes).
- **`gateway`**: `minReplicas: 2`, `maxReplicas: 6` (sin estado; atiende mineros externos y préstamos sobre Redis).
- **`trp`**: `minReplicas: 2`, `maxReplicas: 4` (parte tareas en rangos y evalúa resultados).
- **`nct` (sin HPA, fijo en `replicas: 2`)**: por diseño (`BC-89`), el NCT arma una única caja a la vez mediante lock en Redis (`SET NX`), por lo que agregar más de 2 réplicas no aumenta el throughput; las 2 réplicas cumplen únicamente un rol de alta disponibilidad.
- **`frontend` (fijo en `replicas: 2`)**: sirve archivos estáticos y retransmite SSE desde `eventos`; el cuello de botella no está en su CPU.

Se quita `replicas: 2` de los `Deployment` con HPA (`backend`, `gateway`, `trp`) para evitar que un `kubectl apply` pise la cantidad escalada.

## 3. RBAC y tokens de ServiceAccount (`BC-103`)

- La cuenta `default` del namespace `blockchain` se configura con `automountServiceAccountToken: false`.
- Cada carga de trabajo (`backend`, `frontend`, `nct`, `trp`, `gateway`, `escalador`, `nodo`, `redis`, `rabbitmq`) recibe su propia `ServiceAccount`.
- Todas las cuentas salvo `rabbitmq` tienen `automountServiceAccountToken: false` (tanto en la `ServiceAccount` como en el `pod.spec`): ninguna habla con la API de Kubernetes (en GKE, `escalador` usa Workload Identity a través del GKE Metadata Server del nodo, sin token de API server).
- **`rabbitmq`**: usa `rabbitmq_peer_discovery_k8s` para formar el cluster entre réplicas, por lo que conserva el token montado junto a un `Role` + `RoleBinding` mínimos en `blockchain` que habilitan únicamente `get`, `list` y `watch` sobre `endpoints`.

## 4. NetworkPolicies (`BC-104`)

- En `pilar2/k8s/network-policies.yaml` se define una política base **`denegar-ingreso`** (`podSelector: {}`, `policyTypes: [Ingress]`) que bloquea todo tráfico entrante dentro del namespace `blockchain`, y políticas explícitas por servicio:
  - **`redis` (`6379`, `26379`)**: sólo `redis`, `backend`, `gateway`, `nct`, `trp` y `escalador`. Un pod ajeno (o `frontend` / `nodo`) no puede conectarse a Redis ni Sentinel.
  - **`rabbitmq`**: AMQP (`5672`) sólo desde `rabbitmq`, `backend`, `frontend`, `gateway`, `nct`, `trp`; clustering (`4369`, `25672`) sólo entre pods `rabbitmq`; métricas (`15692`) sólo desde `monitoring`.
  - **`backend` (`8080`)**: desde `traefik`, `kube-system` (Traefik en k3s), `monitoring` (Prometheus) y pods `escalador` / `nodo`.
  - **`frontend` (`8080`)**, **`nct` / `trp` (`8080`)**, **`nodo` (`8090`)**: desde `traefik`, `kube-system` y `monitoring`.
  - **`escalador` (`8080`)**: únicamente desde `monitoring`.
  - **`gateway` (`8080`)**: abierto para recibir a los mineros externos a través de su `LoadBalancer`, además de Prometheus y `/salud/gateway`.
- **Habilitación en GKE (`pilar3/infra/main.tf`):** se habilita `network_policy { enabled = true; provider = "CALICO" }` junto con `addons_config.network_policy_config { disabled = false }` en lugar de migrar a Dataplane V2 (`ADVANCED_DATAPATH`), porque cambiar `datapath_provider` exige destruir y recrear el cluster completo, mientras que Calico se activa sobre el cluster existente (impacto acotado al reinicio de nodos del pool durante la actualización). En k3s las políticas aplican de forma nativa con `kube-router`.

## 5. TLS entre servicios y para los mineros (`BC-105`)

- **Mineros → gateway por HTTPS:** los mineros (VMs, GPU, Colab) entran por `https://chimera-gateway.duckdns.org`. El gateway sirve HTTPS él mismo en el 8443 con el certificado del Secret `gateway-tls`, y su LoadBalancer publica sólo el 443: el 8081 por HTTP ya no existe en GKE. El nombre apunta a la IP fija del gateway (no a Traefik, así la minería no comparte la puerta de la API), por eso el certificado se valida por DNS-01: el workflow «Pilar 3 · Certificado del gateway» lo pide a Let's Encrypt con lego y el token de DuckDNS (secreto de `produccion`, no entra al cluster), y lo renueva cuando le quedan menos de 30 días. El 8080 por HTTP sigue para las probes, Prometheus y el k3s (que no tiene dominio).
- **Limitación interna:** el tráfico dentro del cluster hacia Redis y RabbitMQ sigue sin TLS: no sale de la VPC de GKE, las `NetworkPolicy` de `BC-104` (aplicadas por Calico) dejan hablar con ellos sólo a los servicios que los usan, y las claves están en el Secret `blockchain-secretos`. Agregarlo (CA propia con cert-manager, `tls-port` en Redis y Sentinel, listener SSL en RabbitMQ y los clientes de `comun/` con TLS) obligaba a volver a probar el failover de Sentinel antes de la entrega.

## 6. `securityContext` completo (`BC-106`)

- Todos los `containers` e `initContainers` de los manifiestos (`backend`, `frontend`, `nct`, `trp`, `gateway`, `escalador`, `nodo`, `redis` + `sentinel`, `rabbitmq` e `iniciar`, `minero-gpu`) pasan a tener:
  - `allowPrivilegeEscalation: false`
  - `runAsNonRoot: true`
  - `readOnlyRootFilesystem: true`
  - `capabilities: { drop: [ALL] }`
  - `seccompProfile: { type: RuntimeDefault }` a nivel pod.
- Las rutas de escritura en disco se respaldan con `emptyDir` o PVC: `/app/logs` y `/tmp` en las apps Python (más `HOME=/tmp` y `/tofu` en `escalador`, `CUDA_CACHE_PATH=/tmp/nv` en `minero-gpu`), `/tmp` y `/config` en Redis/Sentinel (donde Sentinel usa `dir /tmp`), `/datos` en `nodo`, y en RabbitMQ el contenedor principal corre explícitamente con usuario `100:101` (`rabbitmq` de Alpine, alineado con el initContainer `iniciar` y `fsGroup: 101`) montando `/var/lib/rabbitmq`, `/definiciones`, `/var/log/rabbitmq` y `/tmp`.

## 7. Réplicas repartidas entre nodos (`BC-107`)

- Redis, RabbitMQ, los nodos verificadores y todas las apps replicadas (`backend`, `frontend`, `nct`, `trp`, `gateway`) incorporan `topologySpreadConstraints` por `kubernetes.io/hostname` con `whenUnsatisfiable: ScheduleAnyway` (`maxSkew: 1`).
- **Limitación por costo:** en reposo el pool `infra` corre con `min_node_count = 1`. Con 1 solo nodo `infra`, la regla `ScheduleAnyway` permite agendar las 3 réplicas de Redis y RabbitMQ sin bloquear el arranque, pero **el quórum no sobrevive a la caída de ese único nodo**. Escalar `infra` a 3 nodos fijos eliminaría el punto único de falla con un costo adicional estimado de ~US\$1,05/día en instancias spot `e2-standard-2`.

## 8. Sincronización de reloj / NTP (`BC-108`)

- Cada pedido firmado al gateway (mineros en `/hello`, `/tasks`, `/results`, `/shares` y acciones del organizador) incluye la cabecera `X-Tiempo` con segundos Unix. `comun/contrato.py` (`VENTANA_PEDIDO = 30`) rechaza con **HTTP `401`** y cuerpo `{"error": "X-Tiempo: corrido más de 30 s"}` cualquier pedido cuyo reloj difiera en más de ±30 segundos respecto al reloj del pod receptor, impidiendo ataques de repetición (*replay*) con pedidos capturados.
- **Nodos en Google Cloud (GKE y VMs COS de minero-cpu):** tanto los nodos de GKE como las VMs con Container-Optimized OS sincronizan su reloj automáticamente contra el servicio NTP interno del metadata server de Google (`metadata.google.internal` / `169.254.169.254`) mediante `chronyd` / `systemd-timesyncd` (con *leap smearing* consistente entre instancias). Por este motivo todos los pods del cluster (`gateway`, `backend`, `nct`) y las VMs escaladas permanecen sincronizados con desviaciones de milisegundos.
- **Mineros externos (PC local, cluster k3s GPU, Google Colab):** dependen del reloj del sistema operativo anfitrión. Si una PC o entorno externo tiene un desfase superior a 30 s respecto al UTC real, el gateway rechaza todos sus pedidos con `401 {"error": "X-Tiempo: corrido más de 30 s"}` antes incluso de validar la firma o leer el cuerpo. En Linux/Windows se verifica y corrige habilitando NTP del sistema (`timedatectl status`, `sudo timedatectl set-ntp true` o `sudo chronyc makestep`).
