# Backend

La API por la que entran las transferencias y se consulta su estado (pasos 1 y 2 del recorrido). No guarda estado: escala sumando réplicas.

## API

| Pedido | Respuesta |
|---|---|
| `POST /api/transacciones` `{origen, destino, monto}` | 202 `{id}` · 400 `{errores: [...]}` · 503 `{error}` |
| `POST /api/transacciones/lote` `[{origen, destino, monto}, ...]` | 202 `{ids}` en el mismo orden · 400 · 413 · 503 `{error, encoladas}` |
| `GET /api/transacciones/<id>` | `{id, origen, destino, monto, timestamp, estado}`, más `motivo` (rechazada) o `bloque` (confirmada) · 404 |
| `GET /api/bloques?limite=20&desde=<altura>` | Los bloques del más nuevo al más viejo, con `altura` y `transactions` como lista |
| `GET /api/bloques/<hash>` | El bloque con su `altura` · 404 |
| `GET /api/saldos` · `GET /api/saldos/<usuario>` | Saldos confirmados; un usuario que no existe tiene 0 |
| `GET /health` · `GET /metrics` | Los de `comun/`, más `blockchain_tx_recibidas_total{resultado}` (`aceptada`, `invalida`, `error`, por transacción) |

- **Formato:** valida con `validar_transaccion` del [contrato](../CONTRATO.md). El saldo lo valida el NCT.
- **Orden:** primero guarda `tx:<id>` «recibida» y después publica (con confirmación del broker). Al revés, el NCT podría pasarla a «pendiente» y el backend pisarla con «recibida».
- **Fallas:** si la cola no confirma, borra los `tx:<id>` que no se publicaron y contesta 503. En un lote, `encoladas` dice cuáles sí entraron: ésas no se pueden sacar y el NCT las va a procesar, así que hay que reintentar sólo las demás.
- **Lote:** todo o nada. Si una es inválida no entra ninguna, y `errores` viene por índice.

## Configuración

Además de las de `comun/` (ver `.env.example`):

| Variable | Por defecto | Para qué |
|---|---|---|
| `LOTE_MAXIMO` | 1000 | Transacciones por lote; si se pasa, 413 |
| `COLA_TRANSACCIONES` | `transacciones` | Dónde publica (los tests usan una cola temporal) |

## Ejecutar (desde `pilar2/`)

```bash
python -m backend
curl -X POST localhost:8080/api/transacciones -H 'content-type: application/json' \
  -d '{"origen":"alice","destino":"bob","monto":10}'          # 202 {"id": ...}
curl localhost:8080/api/transacciones/<id>                      # {"estado": "recibida", ...}
```

Tests: `python -m pytest backend`; con `INTEGRACION=1`, también contra RabbitMQ y Redis (usan la base 15 de Redis y una cola temporal).

## Cuánto entra

Medido en local (un pod, RabbitMQ y Redis en Docker, 16 núcleos), con 1.000 POST sueltos y 10 lotes de 1.000:

| Cómo | tx/s |
|---|---|
| POST sueltos | ~440 |
| Lotes de 1.000 | ~1.100 |

Lo que frena es la confirmación del broker, que `comun/rabbit.py` espera mensaje por mensaje. Con 2 pods, 100.000 transacciones (BC-38) entran en ~45 s. Si hace falta más, el paso siguiente es publicar con confirmaciones asíncronas en `comun/`.

## Desplegar en k3s

El manifiesto es `k8s/backend.yaml`: un Deployment con 2 réplicas y un Service ClusterIP en el 8080. Usa `RABBIT_HOST=rabbitmq`, `REDIS_SENTINELS=redis:26379` y las claves del Secret `blockchain-secretos`. Hasta que haya registry, la imagen se importa a mano:

```bash
docker build --build-arg SERVICIO=backend -t backend:dev .
docker save backend:dev | sudo k3s ctr images import -
kubectl apply -k k8s
kubectl -n blockchain rollout status deploy/backend
kubectl -n blockchain port-forward svc/backend 8080:8080   # y los curl de arriba
```

La readiness usa `/health`: si el pod no llega a RabbitMQ o a Redis, sale del Service. La liveness usa `/metrics`, para que no lo reinicie mientras RabbitMQ o Redis están caídos.
