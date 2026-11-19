# TrP: Trabajador de Particionamiento y Pool (BC-21, BC-22, BC-59, BC-60, BC-73)

Servicio sin estado que corre con 2 réplicas coordinadas por Redis (`HSETNX` y conjuntos). Tiene dos responsabilidades:

1. **Particionar tareas (NCT.2, `tareas` → `rangos`):** elige el largo del prefijo, parte `[0, ESPACIO_NONCES - 1]` en rangos cerrados contiguos y publica uno por mensaje.
2. **Recibir resultados (`resultados` → `soluciones`):** valida el hash con la coinbase de ese minero (`texto_para`) y pasa sólo el primer ganador al NCT (o avisa `sin_solucion` si todos los rangos volvieron vacíos).

---

## Ajuste dinámico de dificultad (Contrato 5 · BC-60)

El prefijo se ajusta automáticamente combinando **dos mecanismos**:

### 1. Ajuste por tiempo de los últimos bloques
Inspirado en el algoritmo de **Bitcoin**:
- **Satoshi Nakamoto (2008)**, *Bitcoin: A Peer-to-Peer Electronic Cash System*, **Sección 4 (*Proof-of-Work*)**: ajusta la dificultad con un promedio móvil para mantener una tasa promedio constante de generación de bloques.
- **Bitcoin Core (`src/pow.cpp`, `CalculateNextWorkRequired`)**: cada 2016 bloques compara el tiempo real contra el tiempo esperado (`nueva_dificultad = dificultad_actual × tiempo_esperado / tiempo_real`), limitando el cambio máximo a un factor $\times 4$ o $\div 4$ por ventana.

En Chimera la dificultad es discreta (largo $p$ del prefijo hexadecimal, donde cada carácter adicional multiplica por 16 los intentos esperados):
- Cada `VENTANA_AJUSTE` bloques minados (por defecto `5`), se promedia el tiempo `real` de los últimos `VENTANA_AJUSTE` bloques leídos de Redis (`cadena` y `bloque:<hash>`, usando `duracion` si está guardada para evitar tiempo ocioso sin transacciones, o la diferencia de `timestamp` como respaldo).
- El salto en caracteres es:
  $$\Delta p = \text{redondeo}\!\left(\log_{16}\!\left(\frac{\text{esperado}}{\text{real}}\right)\right)$$
  acotado a $\pm\text{AJUSTE\_MAXIMO}$ ($\pm 1$ carácter).
- Como $\log_{16}(4) = 0{,}5$, el prefijo cambia recién cuando el promedio de los bloques es al menos **4 veces más rápido** ($\Delta p = +1$) o **4 veces más lento** ($\Delta p = -1$) que `SEGUNDOS_POR_BLOQUE`, manteniendo la misma cota $\times 4$ de Bitcoin Core.

### 2. La potencia, sólo al arrancar y en cambios grandes (BC-73)
- `largo_prefijo(potencia_viva, SEGUNDOS_POR_BLOQUE, PREFIJO_BASE)` es el **techo**: el mayor $p$ con $16^p \le P \times T$.
- Se usa **al arrancar** (cadena sin bloques minados) y cuando la potencia viva **cambia 4 veces o más** desde el último recálculo (`evento_potencia`, referencia en `prefijo:referencia`). Bajar es en el acto; subir pide un bloque minado desde el último recálculo, para que un minero que parpadea no mueva el prefijo.
- El resto del tiempo **no limita** al ajuste por tiempo. Hasta BC-73 el prefijo era $\min(\text{por\_tiempo}, \text{techo})$ en cada tarea: un `bench` que decía de menos (la RTX medida en frío, 689 MH/s en vez de 2.100) frenaba al tiempo para siempre, y uno inflado sostenía el techo aunque se fuera la GPU.

### 3. Bajada de emergencia (BC-73)
- Si la caja lleva `EMERGENCIA_SEGUNDOS` (240) sin bloque, el NCT deja vencer la tarea y la rearma; el TrP baja **un carácter por cada 240 s** desde que se armó la caja (`armada`, que manda el NCT), o desde el último recálculo por potencia si fue después. Hasta 1.
- Es lo que reemplaza al techo cuando se va la GPU: con prefijo 9 y sólo CPUs, baja a 6 en unos 12 minutos. No le cree a nadie: lo que mira es que no sale un bloque.
- Fuente: la regla de la testnet de Bitcoin (si pasan 20 minutos sin bloque, se permite la dificultad mínima).

---

## Reparto según la potencia (Contrato 4 · BC-59)

Qué potencia se usa, para qué y por qué.

### Dos decisiones distintas

El contrato pide que cada minero reciba trabajo según su potencia: más una GPU, menos una CPU. Hay dos cosas que dependen de la potencia, y se resuelven distinto:

| Qué | Cómo | Con qué potencia |
|---|---|---|
| **Cuánto cuesta el bloque** (largo del prefijo) | El TrP, al partir cada tarea | La **suma** de los `bench` de los mineros vivos |
| **Cuánto trabajo se lleva cada minero** | El gateway presta un rango a quien lo pide (pull) | La de cada uno, sin calcular nada: el que termina antes pide otro |

### El prefijo: la suma de los `bench`

Con p caracteres en 0 hacen falta, en promedio, 16^p intentos. Con la potencia viva P y el objetivo T (`SEGUNDOS_POR_BLOQUE`, 60 s), el techo es el mayor p con 16^p ≤ P × T, entre 1 y `PREFIJO_BASE` (`particion.largo_prefijo`). Sin mineros vivos, p = 1, para que el primero que llegue lo resuelva enseguida. Desde BC-73 el techo se usa al arrancar y cuando la potencia cambia 4 veces o más (sección anterior).

P es la **suma** de los `bench` de los mineros que avisaron hace menos de `VIVO_SEGUNDOS` (`particion.potencia_viva`). En clase dijimos «el promedio»: estaba mal, el código usó siempre la suma. Por qué la suma y no otro:

- **Suma:** es la que corresponde. Minan **todos** a la vez, cada uno su rango, así que el bloque tarda 16^p intentos dividido la potencia **total** del pool. Es la única que apunta a T.
- **Mínimo:** el prefijo lo elige el minero más lento. Con una GPU y una CPU, la GPU lo resuelve en milisegundos: se desaprovecha la potencia y los bloques salen mucho antes de T.
- **Máximo:** es como si sólo existiera el más potente. Con varios mineros fuertes el prefijo queda corto (con 2 GPU, elige como si hubiera una), y el resto del pool no cuenta.
- **Promedio:** es la suma dividida la cantidad de mineros, así que depende de **cuántos** hay y no de **cuánto** pueden. Sumar 15 CPU a una GPU *baja* el prefijo, cuando el pool tiene más potencia.

El promedio, el mínimo y el máximo son siempre ≤ la suma: nunca eligen un prefijo más largo, sólo más corto. Como p sube de a un carácter (×16), muchas veces coinciden. La tabla teórica y la medición están en [`pilar3/pruebas`](../../pilar3/pruebas/README.md#criterio-de-potencia-bc-59).

`CRITERIO_POTENCIA` (`suma`, `promedio`, `minimo` o `maximo`) existe **sólo para esa prueba**. Por defecto es `suma`, los manifiestos no lo ponen, y con otro valor el TrP no arranca. El log de cada tarea dice con qué criterio y qué potencia se eligió el prefijo: `Tarea X: … rangos del 10 %, criterio suma, potencia 1,82 GH/s, motivo tiempo` (el motivo es el de BC-60).

#### `bench` y no el hashrate en vivo

Cada minero manda dos medidas: el `bench` del `/hello` (la tarea modelo, igual para todos, ver el [contrato](../CONTRATO.md#tarea-modelo-bench)) y `hashes_por_segundo` en cada keep-alive. El prefijo usa el `bench`:

- es la misma tarea para todos, así que los números se pueden comparar;
- no oscila: el hashrate en vivo baja si la máquina hace otra cosa (la RTX 4060 es la PC de un integrante, compartida) o entre rangos, y el prefijo cambiaría de una tarea a otra sin que cambie el pool.

Grafana sí muestra el hashrate en vivo (`blockchain_hashes_por_segundo`), que es lo que sirve para ver qué está pasando.

### El reparto: pull

Todos los rangos miden lo mismo (`FRAGMENTO_PORCENTAJE` del espacio de nonces). El minero pide uno (`GET /tasks/next`), lo recorre y pide otro. La GPU termina antes, entonces pide más seguido y se lleva más rangos: el trabajo queda repartido en proporción a la potencia **real** de cada uno en ese momento, sin que el TrP ni el gateway tengan que clasificar a nadie. Si un minero se cae, su préstamo vence y el rango vuelve a la cola para otro.

Se ve en Grafana, en la fila «Reparto del trabajo»: «Rangos por minero por minuto» y «Rangos por tipo» (`blockchain_minero_rangos_total{minero, tipo}`, que cuenta el gateway en `historial:<minero>`).

### Por qué no hay vardiff

La fuente del contrato es el **vardiff** de Stratum, el protocolo de los pools de Bitcoin. Ojo con lo que hace: el pool ajusta a cada minero la **dificultad de los shares** según su potencia, para que todos manden shares a un ritmo parecido (unos pocos por minuto) sin saturar al pool con los de una GPU. No cambia el tamaño del trabajo.

Lo parecido acá sería que el tamaño del rango dependa del `bench`, para que cada préstamo dure parecido (por ejemplo, unos 10 s). Hoy, con el 10 % de 2^32, una CPU de 8,6 MH/s tarda unos 50 s por rango y la GPU, menos de un cuarto de segundo. Decidimos no hacerlo:

- el pull ya reparte en proporción a la potencia, que es lo que pide el contrato;
- el gateway tendría que partir o juntar rangos al prestar, y eso complica los préstamos, los vencimientos y los cortes, que hoy son por rango fijo;
- un rango largo en una CPU no frena el bloque: cuando otro minero lo resuelve, el gateway le dice que corte en el próximo keep-alive, y los shares que mandó en los keep-alive anteriores ya se contaron.

Nuestros shares tienen dificultad fija (el prefijo menos un carácter) y un tope de 64 por préstamo. Lo fiel a la fuente, si algún día hace falta, sería un `prefijo_share` por minero según su `bench`.

---

## Variables de entorno

| Variable | Por defecto | Qué es |
|---|---:|---|
| `SEGUNDOS_POR_BLOQUE` | `60` | Tiempo esperado por bloque en segundos (1 minuto, alineado con el contrato) |
| `VENTANA_AJUSTE` | `5` | Cantidad de bloques minados entre cada ajuste por tiempo |
| `AJUSTE_MAXIMO` | `1` | Cambio máximo de caracteres hexadecimales por ajuste |
| `PREFIJO_BASE` | `6` | Tope máximo de largo de prefijo permitido en el sistema |
| `PREFIJO_INICIAL` | `0` | `0`: arranca según potencia viva; `> 0`: fuerza ese largo inicial para pruebas de convergencia |
| `AJUSTAR_PREFIJO` | `1` | `1`: ajuste dinámico; `0`: fijo en `PREFIJO_BASE` (para pruebas de prefijos) |
| `ESPACIO_NONCES` | `2**32` | Espacio de búsqueda `[0, ESPACIO - 1]`. Al menos `16^PREFIJO_BASE × 4`: si no, el TrP no arranca (BC-73) |
| `EMERGENCIA_SEGUNDOS` | `240` | Un carácter menos por cada tantos segundos sin bloque (`0` la apaga). El mismo valor que en el NCT |
| `FRAGMENTO_PORCENTAJE` | `10` | Tamaño de cada rango en `%` del espacio (`1` a `50`) |
| `VIVO_SEGUNDOS` | `15` | Segundos desde el último keep-alive para considerar vivo a un minero |
| `CRITERIO_POTENCIA` | `suma` | Cómo se junta el `bench` de los vivos para el techo (`suma`, `promedio`, `minimo` o `maximo`). Fuera de la suma, sólo para la prueba de BC-59 |

---

## Métricas en Prometheus y Grafana

Expuestas en `/metrics` (puerto `8080`):
- `blockchain_prefijo`: largo del prefijo elegido en la última tarea.
- `blockchain_prefijo_elecciones_total{motivo}`: contador de elecciones por motivo (`inicial`, `tiempo`, `potencia`, `emergencia`, `fijo`). Hasta BC-73 existía `techo`.
- `blockchain_tiempo_bloque_esperado_segundos`: valor configurado de `SEGUNDOS_POR_BLOQUE`, graficado junto al tiempo real (`blockchain_tiempo_bloque_segundos`) en Grafana.
- `blockchain_rangos_publicados_total`, `blockchain_rangos_sin_solucion_total`, `blockchain_resultados_descartados_total{motivo}`.

---

## Pruebas

Desde `pilar2/`:

```bash
python -m pytest trp -v
# Con RabbitMQ y Redis locales:
INTEGRACION=1 python -m pytest trp -v
```
