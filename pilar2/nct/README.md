# NCT: nodo coordinador de tareas (P4)

Define y valida las transacciones, arma los bloques que resuelven los mineros y decide qué bloque entra en la cadena. Corre con 2 réplicas, que se coordinan **sólo por Redis**: scripts Lua (atómicos) y `SET NX`. Ninguna guarda estado en memoria que importe.

```text
transacciones ──► validar ──► tx:pendientes ──► ticker (cada 30 s) ──► cajas
                     │                                                   │
                     └─ aviso transaccion.estado     armar_si_libre ◄────┘
                                                          │
                                                          ▼
                                                       tareas ──► TrP ──► mineros ──► TrP
                                                                                       │
            aviso bloque.confirmado ◄── WAIT ◄── consenso (Lua) ◄── verificar ◄── soluciones
```

## La transacción

Firmada por su origen (contrato v2, BC-56): `{id, origen, destino, monto, nonce, timestamp, firma}`, con las cuentas como claves públicas Ed25519. El `id` es el hash del contenido. Las de origen `emision` crean plata: génesis, coinbase, carga del organizador y participación. El detalle está en el [contrato](../CONTRATO.md).

Estados en `tx:<id>`: `recibida` (la guardó el backend) → `pendiente` o `rechazada` (con `motivo`), que pone el NCT al validar → `confirmada` (con su `bloque`), cuando entra en un bloque.

## Validación (BC-24 y BC-56)

El backend valida el formato y la firma. El NCT nunca confía y valida, en este orden:

1. **La firma**, otra vez (en Python, antes del Lua): si no sirve, `firma inválida`. Una carga tiene que estar firmada por `ORGANIZADOR_CLAVE`.
2. **La cuenta:** el origen tiene que estar aprobado en `cuentas`; si no, `cuenta no aprobada`. Las cargas no miran cuenta ni saldo.
3. **El nonce:** tiene que ser `nonces[origen] + 1`. Uno menor o igual es `nonce repetido` (así una transacción firmada no se puede mandar dos veces). Uno mayor llegó antes que el anterior: las dos réplicas consumen en paralelo y no hay orden garantizado. Espera en `espera:<origen>` y, cuando entra el que falta, el mismo Lua lo destraba. Si a los `ESPERA_NONCE_SEGUNDOS` (60) el que falta no llegó, el ticker lo rechaza como `nonce salteado`.
4. **El saldo:** le alcanza si

```text
saldos[origen] − comprometido[origen] ≥ monto
```

`saldos` son los saldos confirmados (lo que ya está en bloques). `comprometido` es lo que el origen ya tiene en transacciones validadas que todavía no se minaron. Ese descuento es el que **evita el doble gasto**: si alice tiene 100 y manda dos transferencias de 60 seguidas, la segunda se rechaza aunque la primera no esté minada. Lo que recibe el destino recién cuenta cuando se confirma el bloque.

Los pasos 2 a 4 pasan en **un script Lua**: así, dos réplicas que validan a la vez dos transacciones del mismo origen no pueden gastar dos veces el mismo saldo ni aceptar dos veces el mismo nonce. Una rechazada no gasta el nonce. Si la transacción ya no está «recibida» (RabbitMQ la volvió a entregar porque un pod murió antes del ack), el script no cambia nada y sólo se repite el aviso.

## Cajas y tareas (BC-25)

- **Ticker:** cada `VENTANA_SEGUNDOS` (30), una sola réplica (lock `ticker` con `SET NX EX`) cierra cajas. Un Lua saca todo `tx:pendientes` y lo agrega a `cajas` en listas de como mucho `TX_POR_CAJA` (100) ids. No se arma un bloque por transacción, sino uno por ventana.
- **Una sola tarea a la vez:** `tarea:actual` se toma con `SET NX EX` por `EMERGENCIA_SEGUNDOS` (240; si es 0, `TAREA_VENCE_SEGUNDOS`, 600). Quien la consigue arma el bloque candidato con la **primera caja** sobre la punta de la cadena: escribe `tarea:<id>` y publica `{tarea, previous_hash, altura, transacciones, timestamp, armada}` en `tareas`. El prefijo lo elige el TrP.
- **Cuándo se armó la caja (BC-73):** `caja:armada` guarda la hora del primer armado; al rearmar la misma caja se conserva. Va en la tarea como `armada`: de ahí sale la `duracion` del bloque (con los rearmados adentro) y la bajada de emergencia del TrP, que le saca un carácter al prefijo por cada 240 s sin bloque.
- **Participación (BC-63):** en el primer bloque de cada época (10, 20…) van primero los pagos de la anterior, `part-<época>-<minero>`: el fondo (`FONDO_EPOCA`, 100) repartido según `shares:<época>` y la racha de cada minero. El detalle queda en `epoca:<n>`. Rearmar la tarea da los mismos pagos, y un pago que ya está en la cadena no se vuelve a proponer.
- **La caja no se saca de la fila hasta que su bloque se confirma.** Si el NCT muere a mitad de camino, `tarea:actual` vence y la misma caja se vuelve a armar: no se pierde ninguna transacción. La tarea vieja queda `vencida`, así los mineros cortan sus rangos en vez de seguir minando algo que ya no se espera.
- **Cuándo se arma:** en cada tic, después de confirmar un bloque y al arrancar.

## Verificación y consenso (BC-26)

**Verificación (NCT.3).** El NCT nunca confía en el minero. Con lo que guardó en `tarea:<id>` arma el texto **de quien mandó el nonce**, con su coinbase en la raíz y la recompensa que corresponde al prefijo (`texto_para`, BC-62). Recalcula `MD5(texto + nonce)`, y el resultado tiene que coincidir con el hash recibido y empezar con el `prefijo` de esa tarea (el que eligió el TrP). Un nonce robado a otro minero, o minado con otra recompensa, no da el hash.

**Consenso (NCT.4): gana el primer bloque válido que extiende la punta.** Un script Lua, en una sola escritura atómica, agrega el bloque **sólo si** la tarea sigue en curso, su `previous_hash` sigue siendo la punta de `cadena` **y** ninguna de sus transacciones está ya confirmada. Si se cumple:

- guarda `bloque:<hash>` (con su `altura` y su `prefijo`; las transacciones empiezan por la coinbase) y lo agrega a `cadena`;
- mueve los saldos desde las transacciones del bloque: una transferencia le resta al origen, le suma al destino y descuenta `comprometido`; una de emisión (la coinbase, la participación o una carga) sólo le suma al destino. El minero cobra `R = 10 × largo del prefijo` desde su coinbase, no desde un campo aparte;
- pasa cada transacción a «confirmada» con su bloque y le saca a `tx:<id>` lo que ya está en el bloque: queda `{estado, bloque}` (BC-70; la coinbase y la participación nacen así), y suma `bloques` y `recompensas` en `historial:<minero>`;
- saca la caja de la fila y libera `tarea:actual`.

Como es atómico, dos réplicas que reciben a la vez dos soluciones de la misma tarea guardan **un solo bloque**: la segunda ya no ve la tarea en curso. Una solución repetida tampoco guarda nada dos veces.

Después, `WAIT` espera a que una réplica de Redis tenga el bloque, se publica `bloque.confirmado` `{tarea, block_hash, altura}` en `eventos` y se arma la tarea siguiente. Si ninguna réplica confirma a tiempo, se avisa igual (el bloque está en el primario con AOF) y se cuenta en `blockchain_bloques_sin_replica_total`.

**Cuando no hay bloque:**

| Qué pasó | Qué hace el NCT |
|---|---|
| Todos los rangos volvieron «sin solución» | La misma caja como tarea nueva, con otro id y un timestamp mayor (cambia el hash, así que hay soluciones nuevas) |
| La solución no pasa la verificación | La tarea queda `rechazada` y se rearma igual |
| Cambió la punta (entró otro bloque antes) | El bloque no entra; la tarea queda `descartada` y la caja, que sigue primera en la fila, se arma sobre la punta nueva |
| Alguna transacción ya está en otro bloque | El bloque no entra (sería un doble gasto) y la tarea queda `descartada`. Al armar una tarea se saltean las transacciones ya confirmadas, y si una caja no tiene ninguna nueva sale de la fila |

## Génesis

Al arrancar, si `cadena` está vacía, un Lua carga el bloque génesis (`previous_hash` en ceros, una transacción `genesis-<cuenta>` desde `emision` por cada saldo inicial, sin minar), los `saldos` y esas cuentas aprobadas como usuarios. Aunque arranquen las 2 réplicas juntas, entra uno solo.

## Configuración

| Variable | Por defecto | Para qué |
|---|---|---|
| `SALDOS_INICIALES` | `{}` | Los saldos del génesis: `{cuenta: saldo}` o `{cuenta: {saldo, nombre}}`. En k3s y GKE, las cuentas de prueba alice, bob y carol |
| `ORGANIZADOR_CLAVE` | — | La cuenta del organizador: sólo sus cargas entran |
| `ESPERA_NONCE_SEGUNDOS` | 60 | Cuánto espera una transacción con el nonce adelantado |
| `FONDO_EPOCA` | 100 | Lo que se reparte por época entre los que mandaron shares |
| `VENTANA_SEGUNDOS` | 30 | Cada cuánto se cierran cajas |
| `TX_POR_CAJA` | 100 | Transacciones por bloque, como mucho |
| `TAREA_VENCE_SEGUNDOS` | 600 | Cuándo se libera una tarea abandonada si la emergencia está apagada |
| `EMERGENCIA_SEGUNDOS` | 240 | La tarea sin bloque vence a este tiempo y se rearma; el TrP baja un carácter (BC-73). `0` la apaga (pruebas de prefijo fijo) |
| `TAREA_GUARDA_SEGUNDOS` | 3600 | Cuánto queda en Redis una tarea terminada, con sus conjuntos (BC-70) |
| `WAIT_REPLICAS` · `WAIT_MS` | 1 · 1000 | Cuántas réplicas de Redis esperar antes de avisar (0 en local y en el CI) |

Métricas: `blockchain_bloques_total`, `blockchain_tiempo_bloque_segundos` (desde que se publica la tarea hasta que se confirma), `blockchain_tx_pendientes`, `blockchain_tx_en_cajas` (las dos réplicas reportan lo mismo: usar `max`) `blockchain_bloques_sin_replica_total` y `blockchain_tx_rechazadas_total{motivo}`.

## Ejecutar y probar (desde `pilar2/`)

```bash
WAIT_REPLICAS=0 python -m nct
INTEGRACION=1 python -m pytest nct     # contra Redis real, en la base 15
```

Los tests prueban cada función contra Redis real (los scripts Lua no tienen un buen fake), con un publicador falso. Los de concurrencia usan varios hilos como si fueran réplicas: no hay doble gasto, ni dos bloques por tarea, ni cajas repetidas.

## Desplegar en k3s

`k8s/nct.yaml` (2 réplicas, sin Service) está en el kustomization:

```bash
docker build --build-arg SERVICIO=nct -t nct:dev .
docker save nct:dev | sudo k3s ctr images import -   # no hace falta si k3s usa Docker
kubectl apply -k k8s
```

Hasta que exista el TrP (BC-21 y BC-22), nadie escribe el `prefijo` en `tarea:<id>` ni publica en `soluciones`. Para probar a mano, se escribe el prefijo, se mina el nonce y se publica la solución (ver `nct/tests/escenario.py`).
