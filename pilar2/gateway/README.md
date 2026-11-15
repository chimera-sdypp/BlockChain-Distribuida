# Gateway de los mineros

La única puerta de los mineros al sistema. Los mineros están fuera del cluster (las VMs de Compute Engine y el k3s de la PC con GPU), y RabbitMQ y Redis no se publican en Internet: el gateway habla HTTP con los mineros y, por adentro, con RabbitMQ y Redis. No tiene estado propio (los textos que guarda en memoria salen de Redis y no cambian), así que cualquier pod atiende a cualquier minero.

## API

Cada pedido va **firmado con la clave del minero** (BC-57): `X-Minero` (su cuenta), `X-Tiempo` y `X-Firma` (Ed25519 sobre método, ruta, tiempo y cuerpo; ver [Pedidos firmados](../CONTRATO.md#pedidos-firmados-de-los-mineros)). Firma mala o con la hora corrida más de 30 s: 401. Cuenta que no está en `cuentas` como `minero`: 403. El `minero` del cuerpo tiene que ser el que firma: si no, 400. `/health` y `/metrics` no piden firma. No hay token compartido.

| Pedido | Qué hace | Respuesta |
|---|---|---|
| `POST /hello` `{minero, tipo, placa}` | Guarda `tipo`, `placa`, `alias` (el `nombre` de `cuentas`) y `ultimo_aviso` en `nodo:<cuenta>` | La tarea modelo si todavía no hizo el bench; si ya lo hizo, `{registrado: true}` |
| `POST /bench` `{minero, segundos, hashes_por_segundo}` | Guarda `bench` (hashes/s) | 204 |
| `GET /tasks/next` | Toma un rango de `rangos` y se lo presta a quien firma, con **su** texto (`texto_para`: su coinbase en la raíz) | 200 con el rango y `bloque`, o 204 sin trabajo |
| `POST /results` `{tarea, rango, minero, nonce, hash, shares}` o `{…, sin_solucion: true}` | Suma los shares, lo publica en `resultados` (sin los shares) y libera el préstamo | 202 `{shares}` |
| `POST /keepalive` `{minero, hashes_por_segundo, rango: {tarea, rango} \| null, shares?}` | Actualiza `nodo:<cuenta>`, suma los shares y renueva el préstamo | `{cortar: true \| false, shares?}` |

## El texto de cada minero y los shares

- **Texto (BC-62):** el TrP publica los rangos sin texto. Al prestar uno, el gateway arma el texto de ese minero con los datos de `tarea:<id>` (los del NCT y el prefijo del TrP) y lo guarda en memoria por (tarea, minero): no recalcula la raíz en cada rango. Como cada minero mina un texto distinto, un nonce robado no le sirve a otro.
- **Shares (BC-63):** verifica cada uno (que esté en el rango prestado y que su hash cumpla el prefijo menos un carácter) y un Lua los suma en `shares:<época>` y `historial:<minero>` sólo si la tarea sigue en curso, si el préstamo es de ese minero, sin repetidos (`tarea:<id>:shares`) y hasta 64 por préstamo.

## Préstamos

Por HTTP no hay un canal abierto: si el minero muere a mitad de un rango, RabbitMQ no se entera. Por eso, el ack del mensaje lo hace el gateway y el rango queda **prestado**:

1. **Entrega:** se anotan `prestamo:<tarea>:<rango>` (`mensaje` y `minero`) y el vencimiento en `prestamos` (ahora + 30 s), y **recién después** se hace el ack del mensaje. Un rango nunca queda sin dueño ni en la cola. Si Redis falla, el mensaje vuelve a la cola.
2. **Keep-alive (cada 5 s):** renueva el préstamo, pero sólo si es de ese minero. Así un minero lento no le estira el préstamo a otro que ya tomó el mismo rango.
3. **Resultado:** se publica en `resultados` y después se borra el préstamo.
4. **Vencido:** cada 5 s, cada pod busca préstamos vencidos. Un Lua los **reclama** corriéndoles el vencimiento, así los vuelve a publicar un solo pod. Ese pod lo publica en `rangos` y recién después lo borra. Si muere en el medio, el préstamo vuelve a vencer y otro pod lo reintenta: no se pierde ningún rango.

**Tareas cerradas:** el gateway consulta el `estado` de `tarea:<id>`, que escribe el NCT. Si no está `en_curso` (confirmada, sin solución, rechazada o descartada):
- los rangos de esa tarea que queden en la cola se descartan al entregarlos;
- al minero que esté con uno, el próximo keep-alive le contesta **«cortá»** y el gateway borra su préstamo.

No hace falta suscribirse a `eventos`, y un pod que se reinicia no pierde nada.

## Configuración

| Variable | Por defecto | Para qué |
|---|---|---|
| `TASK_LEASE_SECONDS` | 30 | Cuánto dura un préstamo sin keep-alive |
| `REVISAR_PRESTAMOS_SEGUNDOS` | 5 | Cada cuánto se buscan los vencidos |
| `MINERO_VIVO_SEGUNDOS` | 15 | Desde cuándo un minero sin avisar deja de contar en las métricas |
| `COLA_RANGOS` · `COLA_RESULTADOS` | `rangos` · `resultados` | Las colas (los tests usan temporales) |

Métricas: `blockchain_tareas_entregadas_total`, `blockchain_prestamos_vencidos_total`, `blockchain_shares_total{resultado}` (aceptado o descartado), `blockchain_pedidos_rechazados_total{motivo}` (firma o no_aprobado), y `blockchain_mineros{tipo}` y `blockchain_hashes_por_segundo{tipo}`, leídas de `nodo:*` (sólo los vivos). Las dos réplicas reportan lo mismo, así que KEDA tiene que usar `max`.

## Ejecutar y probar (desde `pilar2/`)

```bash
python -m gateway
python -m claves generar cpu-1.clave                          # muestra la cuenta del minero
python -m claves aprobar <cuenta> --tipo minero --nombre cpu-1   # REDIS_*: lo que hace el organizador
GATEWAY_URL=http://localhost:8080 CLAVE_ARCHIVO=cpu-1.clave MINERO_ID=cpu-1 python -m minero_cpu
INTEGRACION=1 python -m pytest gateway     # Redis real (base 15) y colas temporales
```

## Desplegar en k3s

`k8s/gateway.yaml` (2 réplicas y un Service LoadBalancer en el **8081**) está en el kustomization.

```bash
docker build --build-arg SERVICIO=gateway -t gateway:dev .
docker save gateway:dev | sudo k3s ctr images import -   # no hace falta si k3s usa Docker
kubectl apply -k k8s
```

Un minero de afuera usa `GATEWAY_URL=http://<ip del nodo o del LoadBalancer>:8081` y su clave; el organizador aprueba su cuenta con `k8s/aprobar.sh <cuenta> minero <alias>`.
