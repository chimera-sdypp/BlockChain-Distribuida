# Contrato del Pilar 2

Qué datos viajan entre los servicios. El código está en [`comun/contrato.py`](comun/contrato.py).

**Versión 2** (BC-53, el contrato del 13/11): cuentas con clave Ed25519, transacciones firmadas con nonce por cuenta, emisión, coinbase adentro de la raíz y participación por shares. La usan todos los servicios desde BC-55 y BC-61. Cambió el formato de la cadena: con la v2 se arranca de un génesis nuevo, en k3s y en GKE.

## Cuenta

- Una cuenta es una **clave pública Ed25519** (RFC 8032): 32 bytes en hex, 64 caracteres en minúscula (`cuenta_valida`). La privada es la semilla de 32 bytes, también en hex.
- El nombre (alias) va aparte, en `cuentas`, y sirve sólo para mostrar.
- `emision` está reservada: es el origen de la plata nueva.
- **Sólo operan las cuentas aprobadas** (`cuentas`): un usuario sin aprobar no puede transferir y un minero sin aprobar no entra al gateway. Las aprueba el organizador en su panel (ver [Altas](#altas)). Las cuentas del génesis entran aprobadas.
- Python: [`cryptography`](https://cryptography.io). Navegador: WebCrypto, con `Ed25519` en Chrome 137, Firefox 129 y Safari 17 o más nuevos; la privada se importa como JWK (RFC 8037: `{kty: "OKP", crv: "Ed25519", d, x}`, en base64url).

## Transacción

| Campo | Tipo | Quién lo pone |
|---|---|---|
| `id` | MD5 en hex de `origen:destino:monto:nonce:timestamp:firma` (`id_transaccion`) | se calcula |
| `origen`, `destino` | cuenta | cliente |
| `monto` | entero de 1 a 10^12 | cliente |
| `nonce` | entero desde 1: el número de transacción de la cuenta origen | cliente |
| `timestamp` | entero, segundos Unix | cliente (va firmado) |
| `firma` | Ed25519 del origen sobre `origen:destino:monto:nonce:timestamp`, 128 caracteres en hex | cliente |

- **Formato y firma** (backend, `validar_transaccion_firmada`): campos completos, origen ≠ destino, la firma es del origen y, si viene `id`, es el hash. Si no, 400 con los errores. La misma transacción reenviada da el mismo id: 409 (BC-56).
- **Nonce** (NCT): vale si es `nonces[origen] + 1`, y al aceptarla el NCT lo sube. Una rechazada no gasta el nonce. Así una transacción firmada no se puede mandar dos veces. Uno menor o igual es `nonce repetido`. Uno mayor llegó antes que el anterior (las dos réplicas del NCT consumen en paralelo, y los lotes de un bulk pueden llegar desordenados): espera en `espera:<cuenta>` hasta que entra el que falta, y si no llega en `ESPERA_NONCE_SEGUNDOS` (60) queda `nonce salteado`.
- **Cuenta** (NCT): el origen tiene que estar en `cuentas`; si no, `cuenta no aprobada`. El NCT vuelve a verificar la firma (`firma inválida`).
- **Saldo** (NCT): al origen le alcanza descontando sus transacciones pendientes; si no, queda `rechazada`.
- **Estados** en `tx:<id>`: `recibida` → `pendiente` → `confirmada` (con su bloque), o `rechazada` con el motivo: `firma inválida`, `cuenta no aprobada`, `nonce repetido`, `nonce salteado` o `saldo insuficiente`.
- El destino puede no existir: se lo crea con saldo 0.
- El monto es entero para que el texto del bloque salga igual en Python y en C++. El tope es para que los saldos no desborden el entero de 64 bits de Redis (`HINCRBY`).
- `transaccion(privada, destino, monto, nonce)` arma y firma una: es lo que hace la herramienta de claves (BC-55).

## Emisión

Las transacciones con origen `emision` crean plata. Cada una tiene un id fijo, que no se puede repetir, y su propia regla:

| Tipo | `id` | `nonce` | `firma` | La arma | Vale si |
|---|---|---|---|---|---|
| Génesis | `genesis-<cuenta>` | 0 | vacía | el NCT, en el bloque 0 | está en el génesis |
| Carga de saldo | `carga-<n>` | `n` | del organizador | el panel del organizador | la firma es de `ORGANIZADOR_CLAVE` (`validar_carga`) y `n` es `nonces[emision] + 1`. No mira saldo |
| Coinbase | `cb-<altura>-<minero>` | 0 | vacía | el gateway en el texto (`texto_para`) y el NCT en el bloque | es la primera del bloque y es igual a `coinbase(altura, minero que mandó el nonce, recompensa(prefijo), timestamp)` |
| Participación | `part-<época>-<minero>` | 0 | vacía | el NCT, al cerrar la época | está en la primera caja de la época siguiente con el monto del reparto (BC-63) |

## Bloque

Campos de `bloque:<hash>`: `previous_hash`, `nonce`, `timestamp`, `transactions` y `block_hash`, más `prefijo` y `altura` (posición en `cadena`; el génesis es 0). El minero y la recompensa no se guardan aparte: salen de la coinbase, que está adentro del hash (la API los agrega como `minero` y `recompensa`).

Cómo se calcula el hash:

```
tx serializada = id:origen:destino:monto:nonce:timestamp:firma
transacciones  = la coinbase del minero, primero, y después las de la caja en orden
raíz           = MD5(tx1|tx2|...)
texto          = previous_hash;timestamp;raíz;  (77 caracteres)
block_hash     = MD5(texto + nonce)             (nonce en decimal, entero de 64 bits)
```

- `texto_para(tarea, minero)` arma ese texto; la tarea necesita `previous_hash`, `timestamp`, `altura`, `prefijo` y `transacciones`. Como la coinbase está en la raíz, **cada minero mina un texto distinto**: el nonce de uno no le sirve a otro, así que una solución no se puede robar.
- Los mineros siguen recibiendo el texto en `bloque` y le concatenan el nonce; no cambian. Mide siempre 77 caracteres (el minero CUDA acepta hasta 120).
- Un bloque es válido si `block_hash` empieza con el prefijo, `previous_hash` es la punta de la cadena y la primera transacción es la coinbase de quien mandó el nonce.
- **Génesis:** `previous_hash` en 32 ceros, una transacción `genesis-<cuenta>` por cada saldo inicial, sin coinbase, nonce 0 y sin prefijo (no se mina). En la red no trae saldos: todo entra por cargas (BC-58); los saldos iniciales quedan para los tests.

## Recompensa

- **Coinbase:** la cobra el minero que encontró el nonce, `R = 10 × largo del prefijo`, y está adentro del bloque. El NCT acredita desde la coinbase (BC-62).
- **Participación por shares** (BC-63): un **share** es un hash con el prefijo menos un carácter (`prefijo_share`), sobre el texto de ese minero y en un rango que tiene prestado. Con prefijo de 1 no hay shares.
  - El minero junta hasta `SHARES_POR_RANGO` (64) por rango y los manda en `/keepalive` (los que juntó desde el aviso anterior) y en `/results` (los que quedan). Así un minero lento al que le cortan el rango no pierde lo que trabajó.
  - El gateway verifica cada uno (un MD5 y que esté en el rango), descarta los repetidos y los suma en `shares:<época>` y en `historial:<minero>`, sólo mientras la tarea sigue en curso y hasta 64 por préstamo.
  - La época es `altura // BLOQUES_POR_EPOCA` (10). En el primer bloque de cada época (10, 20…) el NCT paga la anterior: reparte `FONDO_EPOCA` (100) en proporción a shares × (1 + 10 % por época seguida con shares, hasta +50 %), con montos enteros (`repartir`). Van como `part-<época>-<minero>`, con el timestamp del último bloque de la época, y el detalle queda en `epoca:<n>`.
- Un keep-alive no paga nada: sin shares no hay participación.

## Pedidos firmados de los mineros

Reemplazan a `MINER_TOKEN` (BC-57). Cada pedido al gateway lleva:

| Encabezado | Qué es |
|---|---|
| `X-Minero` | la cuenta del minero |
| `X-Tiempo` | segundos Unix |
| `X-Firma` | Ed25519 sobre `MÉTODO\nruta\ntiempo\n` seguido del cuerpo tal cual va (bytes). La ruta lleva la query, como en el pedido |

- `firmar_pedido` arma los encabezados y `verificar_pedido` los controla.
- Si la hora está corrida más de 30 s (`VENTANA_PEDIDO`), se rechaza: un pedido grabado no se puede volver a mandar después.
- El `minero` del cuerpo tiene que ser el de `X-Minero`. Firma mala: 401; minero no aprobado en `cuentas`: 403, y queda pedido en `altas:pendientes` con el `alias` de su `/hello`.
- El panel del organizador usa lo mismo, con `X-Organizador` en vez de `X-Minero` (`quien=ORGANIZADOR`).

## Altas

La red es privada (BC-58): sólo opera quien aprueba el organizador, como los validadores de confianza de XRP Ledger o las identidades que admite una red permisionada de Hyperledger Fabric.

- **Pedido** (`POST /api/altas`, público): `{tipo: usuario | minero, clave_publica, nombre, firma}`, con la firma de `clave_publica` sobre `alta:<tipo>:<clave_publica>:<nombre>` (`pedido_alta`, `validar_pedido_alta`). Demuestra que quien pide tiene la privada. Queda en `altas:pendientes`, con un tope de 1000 pedidos (`ALTAS_MAXIMAS`). Un minero firmado y sin aprobar queda pedido solo, al conectarse al gateway.
- **Panel** (`/api/organizador/*`): cada pedido firmado por `ORGANIZADOR_CLAVE` en `X-Organizador`. Si no, 403.
  - aprobar: la cuenta pasa a `cuentas` con su fecha de alta. A un usuario se le puede mandar, en el mismo pedido, una carga firmada por el organizador para esa cuenta.
  - rechazar: se borra el pedido.
  - dar de baja: la cuenta sale de `cuentas`. El gateway deja de atender a ese minero y el NCT rechaza las transferencias de ese usuario (`cuenta no aprobada`). El saldo queda donde está, y las transacciones que ya estaban validadas entran igual en su bloque.
- **Saldo:** entra sólo por cargas del organizador (modelo exchange: la red opera la plata que se cargó adentro). El génesis de la red no trae saldos; `SALDOS_INICIALES` es sólo para las cuentas de prueba (alice, bob y carol) de los tests y las pruebas de carga.
- Los pasos son Lua (`comun/altas.py`): dos réplicas del backend que aprueban lo mismo a la vez no se pisan.

## Tarea modelo (bench)

Iguales para todos: `bloque = "bench;"`, rango `[0, 2^24 − 1]` y prefijo `f` × 32, que no aparece nunca, así el minero recorre el rango entero. En la RTX 4060 tarda ~7 ms y en CPU con Python ~15 s (1,1 MH/s, un solo núcleo). El minero manda los segundos y los hashes/s, y queda clasificado en `nodo:<id>`.

## Mensajes

| Cola | Mensaje |
|---|---|
| `transacciones` | `{id, origen, destino, monto, nonce, timestamp, firma}` (también las cargas) |
| `tareas` | `{tarea, previous_hash, transacciones, timestamp, altura}` (el prefijo lo elige el TrP) |
| `rangos` | `{tarea, rango, desde, hasta, prefijo}` (rango cerrado). Sin `bloque`: el gateway le agrega a cada minero su texto al prestarlo |
| `resultados` | `{tarea, rango, minero, nonce, hash}` o `{tarea, rango, minero, sin_solucion: true}`. Los shares se quedan en el gateway |
| `soluciones` | `{tarea, nonce, hash, prefijo, minero}` o `{tarea, sin_solucion: true}` |
| exchange `eventos` (topic), `bloque.confirmado` | `{tarea, block_hash, altura}` |
| exchange `eventos` (topic), `transaccion.estado` | `{id, estado: "pendiente" \| "rechazada", motivo?}` |

Todas son colas quorum durables con mensajes JSON persistentes. No hay cola `keepalive`: el gateway anota cada aviso en `nodo:<id>`.

Los avisos de `eventos` los recibe cada suscriptor con su propia cola exclusiva (`Rabbit.suscribir`): cada pod del gateway se ata a `bloque.confirmado`, y cada pod del frontend a `bloque.confirmado` y `transaccion.estado`. Se publican con `Rabbit.avisar`; si no hay nadie suscripto, el aviso se pierde sin error.

- `bloque.confirmado`: lo publica el NCT después de guardar el bloque y de que una réplica de Redis lo tenga (`WAIT 1`).
- `transaccion.estado`: lo publica el NCT apenas valida una transacción, cuando la pasa a `pendiente` o `rechazada` (con `motivo`). La confirmación no usa este aviso: la anuncia `bloque.confirmado`.

## API del gateway

Todos firmados por el minero ([Pedidos firmados](#pedidos-firmados-de-los-mineros)); `minero` es su cuenta y tiene que coincidir con `X-Minero`.

- Cada rango entregado es un **préstamo** a nombre del minero, que vence a los `TASK_LEASE_SECONDS` (30). Cada keep-alive del dueño lo renueva; si vence, el gateway vuelve a publicar el rango en `rangos`.
- Si la tarea del rango ya se cerró (`estado` de `tarea:<id>` distinto de `en_curso`), el keep-alive contesta `cortar: true` y el gateway borra el préstamo: el minero deja el rango sin mandar resultado. Los rangos de tareas cerradas que queden en la cola se descartan al entregarlos.

| Pedido | Envía el minero | Contesta el gateway |
|---|---|---|
| `POST /hello` | `{minero, tipo: gpu \| cpu, placa}` | 200 `{registrado: true}`; si es nuevo, 200 con la tarea modelo |
| `POST /bench` | `{minero, segundos, hashes_por_segundo}` | 204 |
| `GET /tasks/next` | — (el minero sale de `X-Minero`: el préstamo queda a su nombre) | 200 `{tarea, rango, bloque, desde, hasta, prefijo}`, con `bloque = texto_para(tarea, minero)`, o 204 sin trabajo |
| `POST /results` | lo mismo que `resultados`, más `shares` (lista de nonces) | 202 `{shares: los que sumó}` |
| `POST /keepalive` | `{minero, hashes_por_segundo, rango: {tarea, rango} \| null, shares?}` (cada 5 s) | 200 `{cortar: true \| false, shares?}` |

## Claves de Redis

| Clave | Tipo | Contenido |
|---|---|---|
| `cadena` | lista | Los `block_hash` en orden; la posición es la altura y el último es la punta |
| `bloque:<hash>` | hash | Los campos del bloque (`transactions` en JSON), con su `altura` |
| `saldos` | hash | cuenta → saldo |
| `tx:<id>` | hash | origen, destino, monto, nonce, timestamp, firma, estado, motivo y bloque |
| `cuentas` | hash | cuenta → JSON `{tipo: usuario \| minero \| organizador, nombre, alta}` (`alta` en segundos Unix). Sólo operan las que están acá |
| `altas:pendientes` | hash | cuenta → JSON `{tipo, nombre, pedida}`: los pedidos de alta que el organizador todavía no aprobó ni rechazó |
| `nonces` | hash | cuenta → último nonce aceptado (pendiente o confirmado). `emision` → la última carga |
| `espera:<cuenta>` | sorted set | Las transacciones de la cuenta que llegaron antes que un nonce anterior (score = nonce). Las destraba el NCT al aceptar el que falta |
| `tx:en_espera` | sorted set | id → cuándo entró en espera; el ticker rechaza las que esperan de más (`nonce salteado`) |
| `tx:pendientes` | lista | Las transacciones validadas que esperan caja |
| `comprometido` | hash | cuenta → suma de sus transacciones validadas que todavía no entraron en un bloque. Lo suma el NCT al validar y lo resta al confirmar: lo disponible es `saldos − comprometido` |
| `cajas` | lista | Cada elemento, una caja: una lista JSON de ids (`["a1…", "b2…"]`), de como mucho `TX_POR_CAJA`. La primera es la que se está minando o la próxima: el NCT la saca recién al confirmar su bloque |
| `tarea:<id>` | hash | Lo escribe el NCT al armarla: `previous_hash`, `timestamp`, `altura`, `transacciones` (JSON, completas y en orden), `caja` (JSON con los ids), `estado` y `publicada` (segundos Unix). Lo agrega el TrP: `prefijo` (el que eligió; el NCT verifica con éste), `rangos` (cuántos) y `ganador` (JSON {minero, nonce, hash} o `sin_solucion`). Al confirmar, el NCT agrega `block_hash`. Estados: `en_curso`, `confirmada`, `descartada` (cambió la punta), `sin_solucion`, `rechazada` (no pasó la verificación) y `vencida` (venció `tarea:actual` sin bloque: se armó otra con la misma caja) |
| `tarea:<id>:sin_solucion` | conjunto | Los rangos que volvieron sin solución (lo maneja el TrP) |
| `tarea:<id>:shares` | conjunto | `<minero>:<nonce>` de los shares aceptados de la tarea, para descartar los repetidos (gateway) |
| `shares:<época>` | sorted set | minero → shares aceptados en la época (`ZINCRBY`, gateway) |
| `epoca:<n>` | hash | El detalle del reparto de la época, para la auditoría: el fondo y, por minero, shares, racha y monto (NCT, BC-63) |
| `historial:<minero>` | hash, permanente | `alta`, `bloques`, `recompensas`, `shares`, `rangos`, `segundos_conectado`, `ultimo_aviso` y `hashes_por_segundo` (gateway y NCT, BC-54) |
| `tarea:actual` | string, con vencimiento | El id de la tarea en curso: hay una sola a la vez entre las réplicas del NCT (`SET NX EX`). Si el NCT muere, vence y se vuelve a armar la misma caja |
| `tarea:ultima` | string | El id de la última tarea publicada. Al publicar otra, si ésta sigue `en_curso` pasa a `vencida`: el gateway deja de prestar sus rangos y les dice «cortá» a los mineros |
| `nodo:<cuenta>` | hash, permanente | `tipo` (`gpu` o `cpu`), `alias` (el `nombre` de `cuentas`), `placa`, `bench` (hashes/s en la tarea modelo), `hashes_por_segundo` (del último aviso) y `ultimo_aviso` (segundos Unix) |
| `prestamos` | sorted set | `<tarea>:<rango>` → cuándo vence (segundos Unix). Lo maneja el gateway |
| `prestamo:<tarea>:<rango>` | hash | `mensaje` (el JSON del rango, sin texto, para volver a publicarlo si vence), `minero` (el dueño: sólo él lo renueva) y `shares` (cuántos le sumó) |
| `escalador:claves` | hash | nombre de cada VM minero-cpu → su clave privada: el escalador se la genera, la aprueba y se la pasa por metadata (BC-57) |
| `escalador:vms` | string | Las VMs pedidas cuando el escalador corre con `VMS_ACTIVAS=0` (simuladas); con VMs de verdad, la cantidad está en el state de OpenTofu |

## Vector de prueba

[`comun/vector_prueba.json`](comun/vector_prueba.json): un génesis, una tarea con 2 transacciones firmadas (alice → bob y bob → carol), la coinbase del minero, la raíz, el texto de ese minero y el nonce que da el prefijo `0000`. Trae también el texto que le tocaría a alice con la misma tarea, que es otro. Lo prueba `comun/tests/test_contrato.py`.

Las claves son las de [`comun/tests/claves.py`](comun/tests/claves.py): la privada de cada una es `sha256("chimera-prueba-<nombre>")`, públicas a propósito. Las firmas dan lo mismo con `openssl pkeyutl -sign -rawin`. En CUDA, con el MD5 del Hit 4:

```bash
T=$(python3 -c "import json; v = json.load(open('pilar2/comun/vector_prueba.json')); print(v['texto'] + str(v['nonce']))")
pilar1/hit4/md5 "$T"    # 0000838af561f709b423629230de3bab
```

## Herramientas

- **Claves** ([`claves/`](claves/)): `python -m claves generar | publica | exportar | firmar | cargar | aprobar | rafaga`. Es el «servicio que genera las claves» (BC-55).
- **Auditoría** ([`herramientas/auditar.py`](herramientas/auditar.py)): `python -m herramientas.auditar` recorre la cadena desde el génesis, verifica cada hash, raíz, firma, nonce y transacción de emisión, recalcula los saldos y los compara con Redis (BC-61).
