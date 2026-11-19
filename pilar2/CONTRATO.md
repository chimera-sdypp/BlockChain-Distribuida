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
- El `minero` del cuerpo tiene que ser el de `X-Minero`. Firma mala: 401; minero no aprobado en `cuentas`: 403, y queda pedido en `altas:pendientes` con el `alias` de su `/hello` (si lo anotó antes otro pedido, el `/hello` le corrige el nombre). Ante un 403 el minero repite sólo el `/hello` hasta que lo aprueben.
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

## Reparto según la potencia

Cada minero recibe trabajo según su potencia medida (contrato 4, BC-59):

- **El prefijo** lo elige el TrP con la **suma** de los `bench` de los mineros vivos (los que avisaron hace menos de `VIVO_SEGUNDOS`): el mayor p con 16^p ≤ suma × `SEGUNDOS_POR_BLOQUE`, entre 1 y `PREFIJO_BASE`. La suma, porque minan todos y el bloque tarda 16^p intentos dividido la potencia total del pool. `CRITERIO_POTENCIA` (promedio, mínimo o máximo) existe sólo para la prueba.
- **El reparto** es por pull: todos los rangos miden lo mismo y cada minero pide otro cuando termina el suyo. El que tiene más potencia termina antes y se lleva más rangos. El gateway los cuenta en `historial:<minero>` (`rangos`).
- **Fuente:** el vardiff del protocolo Stratum (pools de Bitcoin), que ajusta la dificultad de los shares de cada minero según su potencia. Acá no hay vardiff: los rangos y los shares tienen el mismo tamaño y la misma dificultad para todos. El porqué, en el [README del TrP](trp/README.md).

## Tarea modelo (bench)

Iguales para todos: `bloque = "bench;"`, rango `[0, 2^24 − 1]` y prefijo `f` × 32, que no aparece nunca, así el minero recorre el rango entero. En la RTX 4060 tarda ~7 ms y en CPU con Python ~15 s (1,1 MH/s, un solo núcleo). El minero manda los segundos y los hashes/s, y queda clasificado en `nodo:<id>`.

## Mensajes

| Cola | Mensaje |
|---|---|
| `transacciones` | `{id, origen, destino, monto, nonce, timestamp, firma}` (también las cargas) |
| `tareas` | `{tarea, previous_hash, transacciones, timestamp, altura, armada}` (el prefijo lo elige el TrP; `armada`: cuándo se armó la caja por primera vez, BC-73) |
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
- Uno a la vez por minero, y con plazo: pasado el plazo, el keep-alive también contesta `cortar: true` y el rango vuelve a la cola (BC-74, ver [Lo que mide el gateway](#lo-que-mide-el-gateway-bc-74)).

| Pedido | Envía el minero | Contesta el gateway |
|---|---|---|
| `POST /hello` | `{minero, tipo: gpu \| cpu, placa, alias}` | 200 `{registrado: true}`; si es nuevo, 200 con la tarea modelo |
| `POST /bench` | `{minero, segundos, hashes_por_segundo}` | 204 |
| `GET /tasks/next` | — (el minero sale de `X-Minero`: el préstamo queda a su nombre) | 200 `{tarea, rango, bloque, desde, hasta, prefijo}`, con `bloque = texto_para(tarea, minero)`, o 204 sin trabajo. Si ya tiene un préstamo vivo, ese mismo (BC-74) |
| `POST /results` | lo mismo que `resultados`, más `shares` (lista de nonces) | 202 `{shares: los que sumó}` |
| `POST /keepalive` | `{minero, hashes_por_segundo, rango: {tarea, rango} \| null, shares?}` (cada 5 s) | 200 `{cortar: true \| false, shares?, presentarse?}`; `presentarse: true` si su `nodo:<id>` no tiene bench (p. ej. se reinició la cadena): el minero repite `/hello` y `/bench` antes del próximo rango |

## Lo que mide el gateway (BC-74)

El minero declara su `bench` y su `tipo`, y nadie los verifica. Desde BC-74 el gateway no les cree para decidir:

- **Un rango a la vez:** si el minero ya tiene un préstamo vivo (`prestamo_de:<cuenta>`), `/tasks/next` le devuelve ese mismo (por si se reinició). Así nadie acapara los rangos de una tarea.
- **Potencia medida:** al terminar cada préstamo (resultado o «cortá»), el trabajo que se puede probar son los shares: cada uno cuesta 16^(p−1) hashes en promedio. Medido = Σ shares × 16^(p−1) / Σ segundos de los últimos 20 préstamos, como estiman el hashrate los pools.
  - `medido`: si según su bench tendría que haber mandado al menos 20 shares en esos préstamos. Puede dar 0: el que no mina.
  - `cota`: si la mitad o más de los préstamos llegaron al tope de 64 shares. Sólo se sabe que es al menos eso (una GPU con prefijo bajo).
  - `sin_datos`: si todavía no hay con qué decidir (pocos préstamos, o prefijos altos para su potencia).
- **Potencia efectiva** (`contrato.potencia_efectiva`): `min(bench, medido)` si el estado es `medido`; si no, el bench. La usan el TrP (techo del prefijo, BC-73) y el escalador. Inflar el bench no sirve; decir `tipo: gpu` sin minar, tampoco.
- **Préstamos cortos:** uno de menos de 2 s (`MEDICION_SEGUNDOS_MINIMOS`) no se mide: su tiempo es la red, no el hashing (un prefijo chico para su potencia, o una GPU que recorre el rango en décimas).
- **Plazo de cada préstamo:** 3 × (tamaño del rango / potencia) + 10 s, nunca menos de 30 s. Sin medición (`sin_datos`), como mucho 120 s. Pasado el plazo, el keep-alive contesta `cortar: true` y el rango vuelve a la cola. No es una sanción: los shares quedan.

## Los que contestan `sin_solucion` sin buscar (BC-75)

Un `sin_solucion` no se puede verificar: es el *block withholding attack* de los pools de Bitcoin (Meni Rosenfeld, *Analysis of Bitcoin Pooled Mining Reward Systems*, 2011). El gateway lo controla de dos maneras, según el prefijo:

- **Chequeo estadístico (prefijos bajos):** la probabilidad de que un rango de N nonces no tenga solución con prefijo p es e^(−N / 16^p) (`prob_sin_solucion`). Si es menor que `UMBRAL_SIN_SOLUCION` (10⁻⁶), es una mentira: el minero queda suspendido, el resultado no va al TrP y el rango vuelve a la cola. Con un rango del 10 % de 2^32 y prefijo 6 da ~7×10⁻¹²: hoy un `sin_solucion` en un rango completo es, en la práctica, una mentira. No se puede esquivar: usa los rangos reales.
- **Reverificación por muestreo (prefijos altos):** si la probabilidad no alcanza, con `MUESTREO_SIN_SOLUCION` (5 %) y mientras la tarea siga en curso, el rango se vuelve a publicar con el texto del primero (`reverifica`, y el número de rango + 1.000.000 para que el TrP no lo vea). Lo toma **otro** minero; para él es un rango común.
  - Si encuentra un nonce: el primero mintió y queda suspendido. El bloque no se usa: tiene la coinbase del que mintió.
  - Si dice `sin_solucion`: no se concluye nada.
  - Los shares se verifican con el texto del primero y se le acreditan a quien reverificó. El TrP no espera la reverificación.
- **Suspensión:** queda en `suspendidos` con el motivo y la evidencia; el gateway le contesta 403 y su préstamo vuelve a la cola. El organizador, en el panel, la **reactiva** o **confirma la baja**: un minero honesto puede fallar una vez (un bug, una GPU que se reinicia), así que la baja la decide una persona.
- **Aviso por pocos shares:** si en sus últimos 3 préstamos mandó menos de un cuarto de los shares esperados según su bench, queda una línea en el log (`blockchain_pocos_shares_total`). Es una señal, no una prueba: no suspende.
- **Descartada, la tarea trampa:** prestar un rango de un bloque ya resuelto, con el texto del ganador, donde se sabe que hay solución. Se puede detectar: el `previous_hash` de ese texto ya no es la punta, y la punta es pública en `/api/bloques`. Un tramposo contestaría bien las trampas y mentiría en el resto, y a ése lo agarran igual la estadística o la reverificación. La idea viene de los *forced errors* de Truebit y de las tareas *gold standard* del crowdsourcing.

## Claves de Redis

| Clave | Tipo | Contenido |
|---|---|---|
| `cadena` | lista | Los `block_hash` en orden; la posición es la altura y el último es la punta |
| `bloque:<hash>` | hash | Los campos del bloque (`transactions` en JSON), su `altura` y su `duracion` (segundos desde que se armó la caja, con los rearmados: BC-60 y BC-73) |
| `saldos` | hash | cuenta → saldo |
| `tx:<id>` | hash | Hasta confirmarse: origen, destino, monto, nonce, timestamp, firma, estado y, si se rechazó, motivo. **Confirmada, sólo `estado` y `bloque`** (BC-70): el contenido ya está en `bloque:<hash>` y el backend lo lee de ahí. La coinbase y la participación nacen así |
| `cuentas` | hash | cuenta → JSON `{tipo: usuario \| minero \| organizador, nombre, alta}` (`alta` en segundos Unix). Sólo operan las que están acá |
| `altas:pendientes` | hash | cuenta → JSON `{tipo, nombre, pedida}`: los pedidos de alta que el organizador todavía no aprobó ni rechazó |
| `nonces` | hash | cuenta → último nonce aceptado (pendiente o confirmado). `emision` → la última carga |
| `espera:<cuenta>` | sorted set | Las transacciones de la cuenta que llegaron antes que un nonce anterior (score = nonce). Las destraba el NCT al aceptar el que falta |
| `tx:en_espera` | sorted set | id → cuándo entró en espera; el ticker rechaza las que esperan de más (`nonce salteado`) |
| `tx:pendientes` | lista | Las transacciones validadas que esperan caja |
| `comprometido` | hash | cuenta → suma de sus transacciones validadas que todavía no entraron en un bloque. Lo suma el NCT al validar y lo resta al confirmar: lo disponible es `saldos − comprometido` |
| `cajas` | lista | Cada elemento, una caja: una lista JSON de ids (`["a1…", "b2…"]`), de como mucho `TX_POR_CAJA`. La primera es la que se está minando o la próxima: el NCT la saca recién al confirmar su bloque |
| `tarea:<id>` | hash | Lo escribe el NCT al armarla: `previous_hash`, `timestamp`, `altura`, `transacciones` (JSON, completas y en orden), `caja` (JSON con los ids), `estado`, `publicada` y `armada` (segundos Unix; `armada` es cuándo se armó la caja por primera vez, BC-73). Lo agrega el TrP: `prefijo` (el que eligió; el NCT verifica con éste), `rangos` (cuántos) y `ganador` (JSON {minero, nonce, hash} o `sin_solucion`). Al confirmar, el NCT agrega `block_hash`. Estados: `en_curso`, `confirmada`, `descartada` (cambió la punta), `sin_solucion`, `rechazada` (no pasó la verificación) y `vencida` (venció `tarea:actual` sin bloque: se armó otra con la misma caja). Terminada, vence en `TAREA_GUARDA` (1 hora; BC-70), igual que sus dos conjuntos |
| `tarea:<id>:sin_solucion` | conjunto | Los rangos que volvieron sin solución (lo maneja el TrP) |
| `tarea:<id>:shares` | conjunto | `<minero>:<nonce>` de los shares aceptados de la tarea, para descartar los repetidos (gateway) |
| `shares:<época>` | sorted set | minero → shares aceptados en la época (`ZINCRBY`, gateway) |
| `epoca:<n>` | hash | El detalle del reparto de la época, para la auditoría: el fondo y, por minero, shares, racha y monto (NCT, BC-63) |
| `historial:<minero>` | hash, permanente | `alta`, `bloques`, `recompensas`, `shares`, `rangos`, `segundos_conectado`, `ultimo_aviso`, `hashes_por_segundo` y `conectado` (1 o 0) (gateway y NCT, BC-54) |
| `mineros:conectados` | conjunto | Las cuentas con `conectado` = 1: el gateway las revisa cada 5 s para dar por desconectado al que dejó de avisar (BC-54) |
| `caja:armada` | hash | `{caja, desde}`: cuándo se armó por primera vez la caja que se está minando (BC-73). Al rearmar la misma caja se conserva |
| `prefijo:referencia` | hash | `{potencia, altura, prefijo, cuando}`: el último recálculo del prefijo por potencia (BC-73) |
| `tarea:actual` | string, con vencimiento | El id de la tarea en curso: hay una sola a la vez entre las réplicas del NCT (`SET NX EX`). Vence a los `EMERGENCIA_SEGUNDOS` (240, BC-73) o a los `TAREA_VENCE_SEGUNDOS` (600) si la emergencia está apagada: si no salió el bloque (o el NCT murió), se vuelve a armar la misma caja |
| `tarea:ultima` | string | El id de la última tarea publicada. Al publicar otra, si ésta sigue `en_curso` pasa a `vencida`: el gateway deja de prestar sus rangos y les dice «cortá» a los mineros |
| `nodo:<cuenta>` | hash, permanente | `tipo` (`gpu` o `cpu`), `alias` (el `nombre` de `cuentas`), `placa`, `bench` (hashes/s en la tarea modelo), `hashes_por_segundo` (del último aviso), `ultimo_aviso` (segundos Unix), y `medido` y `medido_estado` (`medido`, `cota` o `sin_datos`): lo que midió el gateway con los shares (BC-74) |
| `prestamos` | sorted set | `<tarea>:<rango>` → cuándo vence (segundos Unix). Lo maneja el gateway |
| `prestamo:<tarea>:<rango>` | hash | `mensaje` (el JSON del rango, sin texto, para volver a publicarlo si vence), `minero` (el dueño: sólo él lo renueva), `shares` (cuántos le sumó), `desde` y `plazo` (segundos Unix: cuándo se prestó y hasta cuándo puede tenerlo, BC-74) |
| `prestamo_de:<cuenta>` | string | El préstamo vivo de cada minero (`<tarea>:<rango>`): uno a la vez (BC-74) |
| `suspendidos` | hash | cuenta → JSON `{nombre, motivo, evidencia, cuando}`: los mineros suspendidos por contestar `sin_solucion` sin buscar (BC-75). Salen al reactivarlos o al confirmar la baja |
| `medicion:<cuenta>` | lista | Los últimos 20 préstamos terminados de cada minero, en JSON: trabajo (shares × 16^(p−1)), segundos, shares, shares esperados según su bench y si llegó al tope (BC-74) |
| `escalador:claves` | hash | nombre de cada VM minero-cpu → su clave privada: el escalador se la genera, la aprueba y se la pasa por metadata (BC-57) |
| `escalador:vms` | string | Las VMs pedidas cuando el escalador corre con `VMS_ACTIVAS=0` (simuladas); con VMs de verdad, la cantidad está en el state de OpenTofu |

## Ajuste de dificultad (BC-60 y BC-73)

El TrP elige el largo del prefijo en cada tarea. Desde BC-73 manda el **tiempo real de los bloques**; la potencia que declaran los mineros (el `bench`) se usa sólo al arrancar y cuando cambia mucho:

| Cuándo | Prefijo | Motivo |
|---|---|---|
| La cadena no tiene bloques minados (o se reinició) | El techo por potencia: el mayor p con 16^p ≤ suma de los bench vivos × `SEGUNDOS_POR_BLOQUE` (60 s), entre 1 y `PREFIJO_BASE` | `inicial` |
| La potencia viva cambió 4 veces o más desde el último recálculo | El techo con la potencia nueva | `potencia` |
| Si no | El del último bloque minado, reajustado cada `VENTANA_AJUSTE` (5) bloques por tiempo | `tiempo` |
| La caja lleva `EMERGENCIA_SEGUNDOS` (240) sin bloque | Un carácter menos por cada 240 s, hasta 1 | `emergencia` |
| `AJUSTAR_PREFIJO=0` | `PREFIJO_BASE` (pruebas de prefijo fijo) | `fijo` |

- **Ajuste por tiempo:** se promedia la `duracion` de los últimos 5 bloques y Δp = redondeo(log16(esperado / real)), acotado a ±`AJUSTE_MAXIMO` (1). Como log16(4) = 0,5, sube si los bloques salen 4 veces más rápido (15 s o menos) y baja si tardan 4 veces más (240 s o más). Hasta BC-73 el techo por potencia lo limitaba en cada tarea (`min(tiempo, techo)`): un bench que decía de menos lo frenaba para siempre.
- **Eventos de potencia:** el cambio cuenta si |log16(P / referencia)| ≥ 0,5. Bajar se recalcula en el acto: para «irse», un minero tiene que dejar de avisar, o sea, de minar. Subir pide al menos un bloque minado desde el último recálculo, así un minero que parpadea no hace ir y venir el prefijo (salvo que antes no hubiera ninguna potencia). La referencia queda en `prefijo:referencia`.
- **Emergencia:** el NCT hace que `tarea:actual` venza a los `EMERGENCIA_SEGUNDOS` y rearma la misma caja; el TrP baja un carácter por cada 240 s desde que se armó la caja (`armada`) o desde el último recálculo por potencia, si fue después. Cubre lo que antes cubría el techo cuando se va la GPU, sin creerle a nadie: lo que se mira es que no sale un bloque.
- **`duracion`:** desde que se armó la caja por primera vez (`caja:armada`), con los rearmados adentro. Antes se medía desde la última tarea y el ajuste veía bloques más rápidos de lo que fueron.
- **Espacio de nonces:** `ESPACIO_NONCES` ≥ 16^`PREFIJO_BASE` × 4 o el TrP no arranca. Con menos, muchas tareas no tienen solución y se rearman.
- **Fuentes:**
  - Satoshi Nakamoto (2008), *Bitcoin: A Peer-to-Peer Electronic Cash System*, sección 4 (*Proof-of-Work*): ajuste de dificultad mediante promedio móvil para mantener una tasa promedio constante de bloques.
  - Bitcoin Core (`src/pow.cpp`, `CalculateNextWorkRequired`): retarget cada 2016 bloques (`nTargetTimespan` / tiempo real), acotado a un factor máximo de $\times 4$ / $\div 4$ por ventana para evitar oscilaciones bruscas.
  - La regla de la testnet de Bitcoin (`fPowAllowMinDifficultyBlocks`): si pasan 20 minutos sin bloque, se permite la dificultad mínima. Es la idea de la bajada de emergencia.

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
