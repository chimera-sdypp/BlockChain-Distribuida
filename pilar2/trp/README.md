# TrP: pool de transacciones (P5)

Toma cada tarea del NCT de `tareas`, elige el prefijo según la potencia de los mineros vivos, la parte en rangos de nonce y publica uno por mensaje en `rangos` (NCT.2). Después recibe lo que devuelven los mineros en `resultados` y le pasa al NCT el ganador por `soluciones` (BC-22). El resumen, las variables y el detalle de los resultados están en el [README de `pilar2`](../README.md#trp-trp).

Este README explica **cómo se reparte el trabajo según la potencia** (contrato 4, BC-59): qué potencia se usa, para qué y por qué.

## Dos decisiones distintas

El contrato pide que cada minero reciba trabajo según su potencia: más una GPU, menos una CPU. Hay dos cosas que dependen de la potencia, y se resuelven distinto:

| Qué | Cómo | Con qué potencia |
|---|---|---|
| **Cuánto cuesta el bloque** (largo del prefijo) | El TrP, al partir cada tarea | La **suma** de los `bench` de los mineros vivos |
| **Cuánto trabajo se lleva cada minero** | El gateway presta un rango a quien lo pide (pull) | La de cada uno, sin calcular nada: el que termina antes pide otro |

## El prefijo: la suma de los `bench`

Con p caracteres en 0 hacen falta, en promedio, 16^p intentos. Con la potencia viva P y el objetivo T (`SEGUNDOS_POR_BLOQUE`, 30 s), el TrP elige el mayor p con 16^p ≤ P × T, entre 1 y `PREFIJO_BASE` (`particion.largo_prefijo`). Sin mineros vivos, p = 1, para que el primero que llegue lo resuelva enseguida.

P es la **suma** de los `bench` de los mineros que avisaron hace menos de `VIVO_SEGUNDOS` (`particion.potencia_viva`). En clase dijimos «el promedio»: estaba mal, el código usó siempre la suma. Por qué la suma y no otro:

- **Suma:** es la que corresponde. Minan **todos** a la vez, cada uno su rango, así que el bloque tarda 16^p intentos dividido la potencia **total** del pool. Es la única que apunta a T.
- **Mínimo:** el prefijo lo elige el minero más lento. Con una GPU y una CPU, la GPU lo resuelve en milisegundos: se desaprovecha la potencia y los bloques salen mucho antes de T.
- **Máximo:** es como si sólo existiera el más potente. Con varios mineros fuertes el prefijo queda corto (con 2 GPU, elige como si hubiera una), y el resto del pool no cuenta.
- **Promedio:** es la suma dividida la cantidad de mineros, así que depende de **cuántos** hay y no de **cuánto** pueden. Sumar 15 CPU a una GPU *baja* el prefijo, cuando el pool tiene más potencia.

El promedio, el mínimo y el máximo son siempre ≤ la suma: nunca eligen un prefijo más largo, sólo más corto. Como p sube de a un carácter (×16), muchas veces coinciden. La tabla teórica y la medición están en [`pilar3/pruebas`](../../pilar3/pruebas/README.md#criterio-de-potencia-bc-59).

`CRITERIO_POTENCIA` (`suma`, `promedio`, `minimo` o `maximo`) existe **sólo para esa prueba**. Por defecto es `suma`, los manifiestos no lo ponen, y con otro valor el TrP no arranca. El log de cada tarea dice con qué criterio y qué potencia se eligió el prefijo: `Tarea X: … rangos del 10 %, criterio suma, potencia 1,82 GH/s`.

### `bench` y no el hashrate en vivo

Cada minero manda dos medidas: el `bench` del `/hello` (la tarea modelo, igual para todos, ver el [contrato](../CONTRATO.md#tarea-modelo-bench)) y `hashes_por_segundo` en cada keep-alive. El prefijo usa el `bench`:

- es la misma tarea para todos, así que los números se pueden comparar;
- no oscila: el hashrate en vivo baja si la máquina hace otra cosa (la RTX 4060 es la PC de un integrante, compartida) o entre rangos, y el prefijo cambiaría de una tarea a otra sin que cambie el pool.

Grafana sí muestra el hashrate en vivo (`blockchain_hashes_por_segundo`), que es lo que sirve para ver qué está pasando.

## El reparto: pull

Todos los rangos miden lo mismo (`FRAGMENTO_PORCENTAJE` del espacio de nonces). El minero pide uno (`GET /tasks/next`), lo recorre y pide otro. La GPU termina antes, entonces pide más seguido y se lleva más rangos: el trabajo queda repartido en proporción a la potencia **real** de cada uno en ese momento, sin que el TrP ni el gateway tengan que clasificar a nadie. Si un minero se cae, su préstamo vence y el rango vuelve a la cola para otro.

Se ve en Grafana, en la fila «Reparto del trabajo»: «Rangos por minero por minuto» y «Rangos por tipo» (`blockchain_minero_rangos_total{minero, tipo}`, que cuenta el gateway en `historial:<minero>`).

## Por qué no hay vardiff

La fuente del contrato es el **vardiff** de Stratum, el protocolo de los pools de Bitcoin. Ojo con lo que hace: el pool ajusta a cada minero la **dificultad de los shares** según su potencia, para que todos manden shares a un ritmo parecido (unos pocos por minuto) sin saturar al pool con los de una GPU. No cambia el tamaño del trabajo.

Lo parecido acá sería que el tamaño del rango dependa del `bench`, para que cada préstamo dure parecido (por ejemplo, unos 10 s). Hoy, con el 10 % de 2^32, una CPU de 8,6 MH/s tarda unos 50 s por rango y la GPU, menos de un cuarto de segundo. Decidimos no hacerlo:

- el pull ya reparte en proporción a la potencia, que es lo que pide el contrato;
- el gateway tendría que partir o juntar rangos al prestar, y eso complica los préstamos, los vencimientos y los cortes, que hoy son por rango fijo;
- un rango largo en una CPU no frena el bloque: cuando otro minero lo resuelve, el gateway le dice que corte en el próximo keep-alive, y los shares que mandó en los keep-alive anteriores ya se contaron.

Nuestros shares tienen dificultad fija (el prefijo menos un carácter) y un tope de 64 por préstamo. Lo fiel a la fuente, si algún día hace falta, sería un `prefijo_share` por minero según su `bench`.
