# Gateway de los mineros

La única puerta de los mineros al sistema. Los mineros están fuera del cluster (las VMs de Compute Engine y el cluster K3s de la cátedra), y RabbitMQ y Redis no se publican en Internet: el gateway habla HTTP con los mineros y, por adentro, con RabbitMQ y Redis. No guarda estado en memoria, así que cualquier pod atiende a cualquier minero.

## API

Todos los pedidos de los mineros llevan `Authorization: Bearer MINER_TOKEN`; sin token, 401. `/health` y `/metrics` no lo piden.

| Pedido | Qué hace | Respuesta |
|---|---|---|
| `POST /hello` `{minero, tipo, placa}` | Guarda `tipo`, `placa` y `ultimo_aviso` en `nodo:<id>` | La tarea modelo si todavía no hizo el bench; si ya lo hizo, `{registrado: true}` |
| `POST /bench` `{minero, segundos, hashes_por_segundo}` | Guarda `bench` (hashes/s) | 204 |
| `GET /tasks/next?minero=<id>` | Toma un rango de `rangos` y se lo presta a ese minero | 200 con el rango, o 204 sin trabajo |
| `POST /results` `{tarea, rango, minero, nonce, hash}` o `{…, sin_solucion: true}` | Lo publica en `resultados` y libera el préstamo | 202 |
| `POST /keepalive` `{minero, hashes_por_segundo, rango: {tarea, rango} \| null}` | Actualiza `nodo:<id>` y renueva el préstamo | `{cortar: true \| false}` |

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
| `MINER_TOKEN` | (obligatoria, del Secret) | El token de los mineros |
| `TASK_LEASE_SECONDS` | 30 | Cuánto dura un préstamo sin keep-alive |
| `REVISAR_PRESTAMOS_SEGUNDOS` | 5 | Cada cuánto se buscan los vencidos |
| `MINERO_VIVO_SEGUNDOS` | 15 | Desde cuándo un minero sin avisar deja de contar en las métricas |
| `COLA_RANGOS` · `COLA_RESULTADOS` | `rangos` · `resultados` | Las colas (los tests usan temporales) |

Métricas: `blockchain_tareas_entregadas_total`, `blockchain_prestamos_vencidos_total`, y `blockchain_mineros{tipo}` y `blockchain_hashes_por_segundo{tipo}`, leídas de `nodo:*` (sólo los vivos). Las dos réplicas reportan lo mismo, así que KEDA tiene que usar `max`.

## Ejecutar y probar (desde `pilar2/`)

```bash
MINER_TOKEN=secreto python -m gateway
curl -H 'Authorization: Bearer secreto' -X POST localhost:8080/hello \
  -H 'content-type: application/json' -d '{"minero":"cpu-1","tipo":"cpu","placa":"Ryzen"}'
INTEGRACION=1 python -m pytest gateway     # Redis real (base 15) y colas temporales
```

## Desplegar en k3s

`k8s/gateway.yaml` (2 réplicas y un Service LoadBalancer en el **8081**) está en el kustomization. El Secret tiene que tener `MINER_TOKEN` (ver el README de `pilar2`).

```bash
docker build --build-arg SERVICIO=gateway -t gateway:dev .
docker save gateway:dev | sudo k3s ctr images import -   # no hace falta si k3s usa Docker
kubectl apply -k k8s
```

Un minero de afuera usa `GATEWAY_URL=http://<ip del nodo o del LoadBalancer>:8081` y el mismo `MINER_TOKEN`.
