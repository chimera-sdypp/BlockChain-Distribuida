# Contrato del Pilar 2

Qué datos viajan entre los servicios. El código está en [`comun/contrato.py`](comun/contrato.py).

## Transacción

| Campo | Tipo | Quién lo pone |
|---|---|---|
| `id` | string (uuid4 hex) | backend |
| `origen`, `destino` | string: 1 a 32 letras, números, `_` o `-` | cliente |
| `monto` | entero de 1 a 10^12 | cliente |
| `timestamp` | entero, segundos Unix | backend |

- **Formato** (backend, `validar_transaccion`): campos completos, monto entero de 1 a 10^12, origen ≠ destino y ninguno de los dos `genesis` (reservado). Si no, 400 con los errores.
- **Saldo** (NCT): al origen le alcanza descontando sus transacciones pendientes; si no, queda `rechazada`.
- **Estados** en `tx:<id>`: `recibida` → `pendiente` → `confirmada` (con su bloque), o `rechazada` (con el motivo).
- El destino puede no existir: se lo crea con saldo 0.
- El monto es entero para que el texto del bloque salga igual en Python y en C++. El tope es para que los saldos no desborden el entero de 64 bits de Redis (`HINCRBY`). `:` y `|` no se permiten en los usuarios porque separan campos al serializar.

## Bloque

Campos: `previous_hash`, `nonce`, `timestamp`, `transactions` y `block_hash`, más `prefijo`, `minero`, `recompensa` y `altura` (posición en `cadena`; el génesis es 0).

Cómo se calcula el hash:

```
tx serializada = id:origen:destino:monto:timestamp
raíz           = MD5(tx1|tx2|...)               (en el orden del bloque)
texto          = previous_hash;timestamp;raíz;  (77 caracteres)
block_hash     = MD5(texto + nonce)             (nonce en decimal, entero de 64 bits)
```

- Los mineros reciben `texto` y le concatenan el nonce; no necesitan saber cómo se arma. Por llevar la raíz y no las transacciones, mide siempre 77 caracteres (el minero CUDA acepta hasta 120).
- Un bloque es válido si `block_hash` empieza con el prefijo y `previous_hash` es la punta de la cadena.
- **Génesis:** `previous_hash` en 32 ceros, una transacción `genesis → usuario` por cada saldo inicial, nonce 0 y sin prefijo (no se mina).

## Recompensa

La cobra el minero que encontró el nonce: `R = 10 × largo del prefijo`, sumado a `saldos[<id del minero>]`. Con eso el minero puede transferir como cualquier usuario: por eso su id tiene el mismo formato que un usuario (`usuario_valido`).

## Tarea modelo (bench)

Iguales para todos: `bloque = "bench;"`, rango `[0, 2^24 − 1]` y prefijo `f` × 32, que no aparece nunca, así el minero recorre el rango entero. En la RTX 4060 tarda ~7 ms y en CPU con Python ~15 s (1,1 MH/s, un solo núcleo). El minero manda los segundos y los hashes/s, y queda clasificado en `nodo:<id>`.

## Mensajes

| Cola | Mensaje |
|---|---|
| `transacciones` | `{id, origen, destino, monto, timestamp}` |
| `tareas` | `{tarea, previous_hash, transacciones, timestamp}` (el prefijo lo elige el TrP) |
| `rangos` | `{tarea, rango, bloque (el texto), desde, hasta, prefijo}` (rango cerrado) |
| `resultados` | `{tarea, rango, minero, nonce, hash}` o `{tarea, rango, minero, sin_solucion: true}` |
| `soluciones` | `{tarea, nonce, hash, prefijo, minero}` o `{tarea, sin_solucion: true}` |
| exchange `eventos` (topic), `bloque.confirmado` | `{tarea, block_hash, altura}` |
| exchange `eventos` (topic), `transaccion.estado` | `{id, estado: "pendiente" \| "rechazada", motivo?}` |

Todas son colas quorum durables con mensajes JSON persistentes. No hay cola `keepalive`: el gateway anota cada aviso en `nodo:<id>`.

Los avisos de `eventos` los recibe cada suscriptor con su propia cola exclusiva (`Rabbit.suscribir`): cada pod del gateway se ata a `bloque.confirmado`, y cada pod del frontend a `bloque.confirmado` y `transaccion.estado`. Se publican con `Rabbit.avisar`; si no hay nadie suscripto, el aviso se pierde sin error.

- `bloque.confirmado`: lo publica el NCT después de guardar el bloque y de que una réplica de Redis lo tenga (`WAIT 1`).
- `transaccion.estado`: lo publica el NCT apenas valida una transacción, cuando la pasa a `pendiente` o `rechazada` (con `motivo`). La confirmación no usa este aviso: la anuncia `bloque.confirmado`.

## API del gateway

Todos con `Authorization: Bearer MINER_TOKEN`; sin token, 401. `minero` tiene el formato de un usuario; si no, 400.

| Pedido | Envía el minero | Contesta el gateway |
|---|---|---|
| `POST /hello` | `{minero, tipo: gpu \| cpu, placa}` | 200 `{registrado: true}`; si es nuevo, 200 con la tarea modelo |
| `POST /bench` | `{minero, segundos, hashes_por_segundo}` | 204 |
| `GET /tasks/next` | — | 200 `{tarea, rango, bloque, desde, hasta, prefijo}` o 204 sin trabajo |
| `POST /results` | lo mismo que `resultados` | 202 |
| `POST /keepalive` | `{minero, hashes_por_segundo, rango}` | 200 `{cortar: true \| false}` |

## Claves de Redis

| Clave | Tipo | Contenido |
|---|---|---|
| `cadena` | lista | Los `block_hash` en orden; la posición es la altura y el último es la punta |
| `bloque:<hash>` | hash | Los campos del bloque (`transactions` en JSON), con su `altura` |
| `saldos` | hash | usuario → saldo |
| `tx:<id>` | hash | origen, destino, monto, timestamp, estado, motivo y bloque |
| `tx:pendientes` | lista | Las transacciones validadas que esperan caja |
| `comprometido` | hash | usuario → suma de sus transacciones validadas que todavía no entraron en un bloque. Lo suma el NCT al validar y lo resta al confirmar: lo disponible es `saldos − comprometido` |
| `cajas` | lista | Cada elemento, una caja: una lista JSON de ids (`["a1…", "b2…"]`), de como mucho `TX_POR_CAJA`. La primera es la que se está minando o la próxima: el NCT la saca recién al confirmar su bloque |
| `tarea:<id>` | hash | Lo escribe el NCT al armarla: `previous_hash`, `timestamp`, `transacciones` (JSON, completas y en orden), `caja` (JSON con los ids), `estado` y `publicada` (segundos Unix). Lo agrega el TrP: `bloque` (el texto a hashear), `prefijo` (el que eligió; el NCT verifica con éste), `rangos` (cuántos) y `ganador` (JSON {minero, nonce, hash} o `sin_solucion`). Al confirmar, el NCT agrega `block_hash`. Estados: `en_curso`, `confirmada`, `descartada` (cambió la punta), `sin_solucion` y `rechazada` (no pasó la verificación) |
| `tarea:<id>:sin_solucion` | conjunto | Los rangos que volvieron sin solución (lo maneja el TrP) |
| `tarea:actual` | string, con vencimiento | El id de la tarea en curso: hay una sola a la vez entre las réplicas del NCT (`SET NX EX`). Si el NCT muere, vence y se vuelve a armar la misma caja |
| `nodo:<id>` | hash, permanente | `tipo` (`gpu` o `cpu`), `placa`, `bench` (hashes/s en la tarea modelo), `hashes_por_segundo` (del último aviso) y `ultimo_aviso` (segundos Unix). El `<id>` cumple `usuario_valido` |
| `prestamos` | sorted set | rango prestado → cuándo vence (lo maneja el gateway) |

## Vector de prueba

[`comun/vector_prueba.json`](comun/vector_prueba.json): un génesis, un bloque con 2 transacciones y el nonce que da el prefijo `0000`. Lo prueba `comun/tests/test_contrato.py`; en CUDA, con el MD5 del Hit 4:

```bash
T=$(python3 -c "import json; v = json.load(open('pilar2/comun/vector_prueba.json')); print(v['texto'] + str(v['nonce']))")
pilar1/hit4/md5 "$T"    # 0000664df63432d4884b763055877eb2
```
