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

| Campo | Tipo | Quién lo pone |
|---|---|---|
| `id` | uuid4 hex | backend |
| `origen`, `destino` | 1 a 32 caracteres `[A-Za-z0-9_-]`; `genesis` reservado | cliente |
| `monto` | entero de 1 a 10^12 | cliente |
| `timestamp` | segundos Unix | backend |

Estados en `tx:<id>`: `recibida` (la guardó el backend) → `pendiente` o `rechazada` (con `motivo`), que pone el NCT al validar → `confirmada` (con su `bloque`), cuando entra en un bloque.

Dentro del bloque, cada transacción se serializa como `id:origen:destino:monto:timestamp`, y el bloque lleva la raíz `MD5(tx1|tx2|…)`. El texto que hashean los mineros es `previous_hash;timestamp;raíz;` más el nonce. El detalle está en el [contrato](../CONTRATO.md).

## Validación (BC-24)

El backend valida el **formato**. El NCT valida el **saldo**: a una transacción le alcanza si

```text
saldos[origen] − comprometido[origen] ≥ monto
```

`saldos` son los saldos confirmados (lo que ya está en bloques). `comprometido` es lo que el origen ya tiene en transacciones validadas que todavía no se minaron. Ese descuento es el que **evita el doble gasto**: si alice tiene 100 y manda dos transferencias de 60 seguidas, la segunda se rechaza aunque la primera no esté minada. Lo que recibe el destino recién cuenta cuando se confirma el bloque.

Todo eso pasa en **un script Lua**: leer el saldo, comparar, sumar a `comprometido` y agregar la transacción a `tx:pendientes`. Así, dos réplicas que validan a la vez dos transacciones del mismo origen no pueden gastar dos veces el mismo saldo. Si la transacción ya no está «recibida» (RabbitMQ la volvió a entregar porque un pod murió antes del ack), el script no cambia nada y sólo se repite el aviso.

## Cajas y tareas (BC-25)

- **Ticker:** cada `VENTANA_SEGUNDOS` (30), una sola réplica (lock `ticker` con `SET NX EX`) cierra cajas. Un Lua saca todo `tx:pendientes` y lo agrega a `cajas` en listas de como mucho `TX_POR_CAJA` (100) ids. No se arma un bloque por transacción, sino uno por ventana.
- **Una sola tarea a la vez:** `tarea:actual` se toma con `SET NX EX TAREA_VENCE_SEGUNDOS` (600). Quien la consigue arma el bloque candidato con la **primera caja** sobre la punta de la cadena: escribe `tarea:<id>` y publica `{tarea, previous_hash, transacciones, timestamp}` en `tareas`. El prefijo lo elige el TrP.
- **La caja no se saca de la fila hasta que su bloque se confirma.** Si el NCT muere a mitad de camino, `tarea:actual` vence y la misma caja se vuelve a armar: no se pierde ninguna transacción. La tarea vieja queda `vencida`, así los mineros cortan sus rangos en vez de seguir minando algo que ya no se espera.
- **Cuándo se arma:** en cada tic, después de confirmar un bloque y al arrancar.

## Verificación y consenso (BC-26)

**Verificación (NCT.3).** El NCT nunca confía en el minero. Con lo que guardó en `tarea:<id>`, recalcula `MD5(texto + nonce)`, y el resultado tiene que coincidir con el hash recibido y empezar con el `prefijo` de esa tarea (el que eligió el TrP). Además, el minero tiene que tener formato de usuario, porque cobra la recompensa en `saldos`.

**Consenso (NCT.4): gana el primer bloque válido que extiende la punta.** Un script Lua, en una sola escritura atómica, agrega el bloque **sólo si** la tarea sigue en curso **y** su `previous_hash` sigue siendo la punta de `cadena`. Si se cumple:

- guarda `bloque:<hash>` (con su `altura`, `prefijo`, `minero` y `recompensa`) y lo agrega a `cadena`;
- mueve los saldos (origen −, destino +) y descuenta `comprometido`;
- le paga al minero `R = 10 × largo del prefijo`;
- pasa cada transacción a «confirmada» con su bloque;
- saca la caja de la fila y libera `tarea:actual`.

Como es atómico, dos réplicas que reciben a la vez dos soluciones de la misma tarea guardan **un solo bloque**: la segunda ya no ve la tarea en curso. Una solución repetida tampoco guarda nada dos veces.

Después, `WAIT` espera a que una réplica de Redis tenga el bloque, se publica `bloque.confirmado` `{tarea, block_hash, altura}` en `eventos` y se arma la tarea siguiente. Si ninguna réplica confirma a tiempo, se avisa igual (el bloque está en el primario con AOF) y se cuenta en `blockchain_bloques_sin_replica_total`.

**Cuando no hay bloque:**

| Qué pasó | Qué hace el NCT |
|---|---|
| Todos los rangos volvieron «sin solución» | La misma caja como tarea nueva, con otro id y un timestamp mayor (cambia el hash, así que hay soluciones nuevas) |
| La solución no pasa la verificación | La tarea queda `rechazada` y se rearma igual |
| Cambió la punta (entró otro bloque antes) | El bloque no entra; la tarea queda `descartada` y la caja, que sigue primera en la fila, se arma sobre la punta nueva |

## Génesis

Al arrancar, si `cadena` está vacía, un Lua carga el bloque génesis (`previous_hash` en ceros, una transacción `genesis → usuario` por cada saldo inicial, sin minar) y los `saldos`. Aunque arranquen las 2 réplicas juntas, entra uno solo.

## Configuración

| Variable | Por defecto | Para qué |
|---|---|---|
| `SALDOS_INICIALES` | `{"alice": 1000, "bob": 1000, "carol": 1000}` | Los saldos del génesis |
| `VENTANA_SEGUNDOS` | 30 | Cada cuánto se cierran cajas |
| `TX_POR_CAJA` | 100 | Transacciones por bloque, como mucho |
| `TAREA_VENCE_SEGUNDOS` | 600 | Cuándo se libera una tarea abandonada (subirlo para prefijos largos) |
| `WAIT_REPLICAS` · `WAIT_MS` | 1 · 1000 | Cuántas réplicas de Redis esperar antes de avisar (0 en local y en el CI) |

Métricas: `blockchain_bloques_total`, `blockchain_tiempo_bloque_segundos` (desde que se publica la tarea hasta que se confirma), `blockchain_tx_pendientes`, `blockchain_tx_en_cajas` (las dos réplicas reportan lo mismo: usar `max`) y `blockchain_bloques_sin_replica_total`.

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
