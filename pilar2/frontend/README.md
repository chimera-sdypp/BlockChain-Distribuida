# Frontend

La página del sistema: hacer transferencias y ver la cadena en vivo. El servicio sólo sirve el HTML y reenvía por SSE los avisos de `eventos`; los datos los pide la página a la API del backend (`/api/*`), en el mismo dominio gracias al Ingress. No usa Redis.

## Qué hace

| Ruta | Qué devuelve |
|---|---|
| `GET /` | La página: un HTML con JS y CSS, sin frameworks ni build |
| `GET /eventos` | SSE: cada aviso de `eventos` como `event: <clave>` y `data: <JSON>`, más un comentario de keep-alive cada 15 s |
| `GET /health` · `GET /metrics` | Los de `comun/` (sólo RabbitMQ), más `blockchain_sse_clientes` y `blockchain_eventos_reenviados_total{evento}` |

- Cada pod se suscribe a `bloque.confirmado` y `transaccion.estado` con su propia cola exclusiva (`Rabbit.suscribir`). Así cada navegador recibe todos los avisos, esté conectado al pod que esté.
- La página tiene cuatro paneles:
  1. **Transferir y «mis transferencias»:** las últimas 20 de este navegador, en `localStorage`.
  2. **Esperando bloque:** `/api/cajas`.
  3. **Mineros vivos:** `/api/mineros`.
  4. **Cadena:** `/api/bloques`.
- Cómo se actualiza:
  - Con `transaccion.estado`: cambia el estado de una transferencia propia.
  - Con `bloque.confirmado`: agrega el bloque arriba, consulta las transferencias propias que siguen abiertas y recarga cajas y mineros.
  - Al reconectarse el SSE: recarga todo, porque pudo haberse perdido algún aviso. No hay polling.

## Configuración

Además de las de `comun/`: `SSE_KEEPALIVE` (segundos, 15 por defecto).

## Ejecutar y probar (desde `pilar2/`)

```bash
python -m frontend                       # sirve / y /eventos en el 8080
curl -N localhost:8080/eventos           # queda escuchando
python -c "from comun import Rabbit, config
r = Rabbit(config.cargar('prueba')); r.conectar()
r.avisar('eventos', 'bloque.confirmado', {'tarea': 't', 'block_hash': '00aa', 'altura': 1})"
```

Para usar la página hace falta el backend en el mismo dominio, o sea, el Ingress (ver abajo). Tests: `python -m pytest frontend`; con `INTEGRACION=1`, también contra RabbitMQ (cada test usa un exchange propio).

## Desplegar en k3s

`k8s/frontend.yaml` (Deployment ×2 + Service) y `k8s/ingress.yaml` (Traefik: `/api` al backend; `/` y `/eventos` al frontend) están en el kustomization.

```bash
docker build --build-arg SERVICIO=backend -t backend:dev .
docker build --build-arg SERVICIO=frontend -t frontend:dev .
# Si k3s usa containerd (lo normal): importarlas. Con el runtime de Docker no hace falta.
docker save backend:dev frontend:dev | sudo k3s ctr images import -
kubectl apply -k k8s
```

La página queda en el puerto 80 del nodo. Si otro proceso ocupa el 80 (por ejemplo, un Apache local), se entra por el NodePort de Traefik: `kubectl -n kube-system get svc traefik`.
