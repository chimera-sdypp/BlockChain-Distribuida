# Prueba de bulks: de 1 a 100.000 transacciones (BC-38)

Manda bulks de transacciones al backend por el Ingress público y mide dos cosas:

- **Lado HTTP** (k6): latencia de cada pedido de lote (p50, p95 y p99), pedidos por segundo y porcentaje de errores.
- **Lado del sistema:** cuánto tarda en confirmarse el bulk entero (hasta que la última transacción aparece en un bloque), en cuántos bloques entró y cuántas transacciones por bloque.

| Archivo | Qué hace |
|---|---|
| `bulk.js` | Script de k6: manda los lotes ya firmados (hasta 1000 por `POST /api/transacciones/lote`, el máximo del backend) y los reparte entre los usuarios virtuales |
| `correr.py` | Por cada tamaño y repetición: anota las condiciones, firma las transacciones de la corrida (antes de medir), corre k6, sigue los bloques nuevos hasta ver todo confirmado y agrega una fila a `resultados.csv` |

## Antes de correrla

1. **k6** en el PATH ([instalación](https://grafana.com/docs/k6/latest/set-up/install-k6/)) y el Python de `pilar2` (firma con `cryptography`).
   - **Contrato v2:** cada transacción va firmada por su origen y con el nonce siguiente de su cuenta. `correr.py` las firma antes de medir (con las cuentas de prueba, `../firmar.py`), así el tiempo de firmar no entra en la latencia. Los destinos son 100 cuentas nuevas por corrida: así se reconocen sus transacciones en los bloques. Si dos lotes del mismo origen llegan desordenados, el NCT espera el nonce que falta (hasta 60 s).
2. **Saldo para el bulk más grande.** El NCT acepta una transacción si al origen le alcanza el saldo *confirmado* menos lo que ya tiene comprometido; lo que está por recibir no cuenta. Con los saldos por defecto (alice, bob y carol con 1000 cada uno) entran unas 3000 transacciones de monto 1, y el resto se rechaza. Para 100.000, cada origen necesita al menos 33.334.
   - Los saldos iniciales salen del génesis, que el NCT carga **solo cuando la cadena está vacía**. En GKE el NCT tiene saldos de 1.000.000.000 ([`nct-saldos.yaml`](../../k8s/apps/nct-saldos.yaml)), pero para que los tome una cadena que ya existe hay que reiniciarla (ver [`../README.md`](../README.md)).
   - `correr.py` lo comprueba con `/api/saldos` antes de empezar y no corre si no alcanza (salvo con `--permitir-rechazos`).
3. **Mismas condiciones en todas las corridas.** Prefijo fijo (`AJUSTAR_PREFIJO=0` y `PREFIJO_BASE`), el mismo `FRAGMENTO_PORCENTAJE` y la misma cantidad de mineros, con el escalador pausado para que no cambie las VMs en el medio. Todo eso lo deja el workflow «Pilar 3 · Preparar prueba» ([`../README.md`](../README.md)). El CSV guarda en cada fila los mineros vivos por tipo y el prefijo del último bloque al empezar.
4. **Un prefijo bajo.** Entran como mucho 100 transacciones por bloque (`TX_POR_CAJA` del NCT) y los bloques salen de a uno: 100.000 transacciones son 1000 bloques. Con el prefijo ajustado a 30 s por bloque serían más de 8 horas por corrida; con un prefijo fijo de 4 o 5 cada bloque tarda segundos. Ese techo de transacciones por bloque es parte de lo que mide la prueba.

## Correrla

La IP del Ingress (Traefik) la muestra el workflow «Pilar 3 · Preparar prueba» en su resumen.

```bash
cd pilar3/pruebas/bulks
P=../../../pilar2/.venv/bin/python   # firma con cryptography
$P correr.py --url http://<IP del Ingress> --tamanos 1,10,100 --repeticiones 1   # prueba rápida: cuánto tarda un bloque
$P correr.py --url http://<IP del Ingress> --tamanos 1,10,100,1000,10000         # 3 veces cada uno
$P correr.py --url http://<IP del Ingress> --tamanos 100000                      # la más larga, aparte
```

| Opción | Por defecto | Qué es |
|---|---|---|
| `--tamanos` | `1,10,100,1000,10000,100000` | Tamaños del bulk |
| `--repeticiones` | 3 | Corridas por tamaño |
| `--vus` | 10 | Usuarios virtuales de k6 (nunca más que lotes) |
| `--lote` | 1000 | Transacciones por pedido (máximo del backend: 1000) |
| `--origenes` | `alice,bob,carol` | Las cuentas de prueba que firman; se reparten en ronda |
| `--timeout` | según el tamaño | Segundos máximos esperando la confirmación (si se pasa, la fila queda con `completa=no`). Si no se da: 120 + `--segundos-por-bloque` × bloques necesarios (de 135 s para 1 tx a unas 4 h para 100.000) |
| `--segundos-por-bloque` | 15 | Tope de segundos por bloque para el timeout automático |
| `--tx-por-bloque` | 100 | Transacciones por bloque como mucho (`TX_POR_CAJA` del NCT) |
| `--intervalo` | 1 | Segundos entre consultas de bloques (la resolución del tiempo de confirmación) |
| `--pausa` | 5 | Segundos entre corridas |
| `--csv` | `resultados.csv` | Se agrega al final, así se puede seguir otro día |

### Carga de k6 (lo que pide documentar BC-38)

- **Usuarios virtuales:** `--vus` (10). Cada uno manda lotes hasta que no quedan (executor `shared-iterations`). Un bulk de 1 a 1000 es un solo pedido, así que lo manda un solo VU.
- **Ramp-up:** ninguno. Los VUs arrancan juntos, porque se mide el sistema recibiendo el bulk de golpe.
- **Duración:** la que tarde en mandar todos los lotes (columna `segundos_envio`). No hay una duración fija.

## El CSV

Cada transacción de una corrida va a un destino `b38-<corrida>-<n>`: así se reconocen en los bloques sin consultar cada id.

| Columna | Qué es |
|---|---|
| `corrida`, `inicio`, `tamano`, `repeticion` | Identifican la corrida (`inicio` en UTC) |
| `vus`, `lote`, `lotes` | La carga de k6 |
| `mineros_gpu`, `mineros_cpu`, `prefijo`, `altura_inicial` | Condiciones al empezar |
| `p50_ms`, `p95_ms`, `p99_ms` | Latencia de cada pedido de lote |
| `pedidos`, `pedidos_por_s`, `errores_http_pct` | Pedidos hechos, ritmo y porcentaje de fallidos |
| `aceptadas` | Transacciones de lotes que respondieron 202 |
| `segundos_envio` | Desde que arranca k6 hasta que termina de mandar |
| `confirmadas`, `segundos_confirmacion` | Cuántas aparecieron en bloques y cuándo apareció la última, contado desde que arrancó k6 (resolución: `--intervalo`) |
| `bloques`, `tx_por_bloque_prom`, `tx_por_bloque_max` | En cuántos bloques entró la corrida |
| `completa` | `si` si se confirmaron todas las aceptadas antes del timeout |

Los gráficos comparativos salen de este CSV ([BC-42](https://distribuido.atlassian.net/browse/BC-42)).

## Resultados (17/11 en GKE)

Corridas guardadas en [`resultados.csv`](resultados.csv) sobre `http://34.72.252.133` con condiciones fijadas en el cluster (`PREFIJO_BASE=5`, `AJUSTAR_PREFIJO=0`, `FRAGMENTO_PORCENTAJE=10`, `VENTANA_SEGUNDOS=30`, `TX_POR_CAJA=100`, `escalador` pausado en KEDA; sin reiniciar la cadena entre tamaños).

- **Mineros:** `3` GPU (`minero-gpu-0` RTX 4060, `gpu-colab-tomas` Tesla T4 y `gpu-colab-mateo` Tesla T4) desde `1` hasta `1.000`; `gpu-colab-mateo` se desconectó antes de `10.000` y el resto corrió con `2` GPU (`minero-gpu-0` + `gpu-colab-tomas`), `0` CPU.
- **Carga de k6:** `ramp-up = 0`, lotes de hasta `1000` transacciones firmadas (`alice, bob, carol` en ronda). Primero `VUS=10` (por defecto); luego `VUS=1` en `10.000` y `100.000` (con `MAX_DURACION=60m`).

| Tamaño | VUs | GPUs | Rep. ok | HTTP `p50` (media / rango) | HTTP `p99` (media) | Envío HTTP (media) | Confirmación sistema | Bloques | tx/bloque | Estado |
|---:|---:|---:|---|---:|---:|---:|---|---|---:|---|
| `1` | 10 | 3 | 3/3 | `185,3 ms` (`182,3–188,9`) | `185,3 ms` | `0,80 s` | `23,70 s` (`23,23–24,54 s`) | `1` | `1,0` | `si` |
| `10` | 10 | 3 | 3/3 | `241,2 ms` (`235,4–244,6`) | `241,2 ms` | `0,78 s` | `23,26 s` (`23,16–23,34 s`) | `1` | `10,0` | `si` |
| `100` | 10 | 3 | 3/3 | `1101,2 ms` (`971,1–1243,0`) | `1101,2 ms` | `1,70 s` | `23,49 s` (`22,73–24,33 s`) | `1` | `100,0` | `si` |
| `1.000` | 10 | 3 | 3/3 | `9,92 s` (`8,99–10,75 s`) | `9,92 s` | `10,59 s` | `24,06 s` (`21,41–26,75 s`) | `10` | `100,0` | `si` |
| `10.000` | 10 | 2 | 0/3 | `57,64 s` (`48,87–66,74 s`) | `58,35 s` | `59,15 s` | `6667`, `1000` y `4667 / 10000` (timeout `1620 s`) | `68` / `12` / `49` | `83,3–98,0` | `no` |
| `10.000` | 1 | 2 | 1/1 | `8,35 s` | `11,52 s` | `87,32 s` | `106,47 s` (`93,9` tx/s) | `102` | `98,0` | `si` |
| `100.000` | 1 | 2 | 2/3* | `10,42 s` (`8,55–12,29 s`) | `15,86 s` | `1078,65 s` | `1139,54 s` y `1481,89 s` (`87,8` y `67,5` tx/s) | `1016` / `1024` | `98,4` / `97,7` | `si` |

\* La 3.ª repetición de `100.000` (corrida `b5da9e`) falló al acumular ~`280k` tx en la cadena (ver abajo; no se guardó fila porque `/api/bloques` devolvió `503` antes de cerrar). Sus métricas HTTP de k6 fueron `p50 10033,6 ms`, `p95 16059,2 ms`, `p99 16727,5 ms`, `pedidos 100 (0,1/s)`, `46,0 %` fallidos.

### Qué muestran las corridas

- **Latencia HTTP lineal (~`10 ms/tx`):** el backend publica cada transacción del lote de a una en RabbitMQ esperando confirmación de quórum (`confirm_delivery`) bajo un `RLock` por pod (`pilar2/comun/rabbit.py`), por eso `100` tx tardan `~1,1 s` y `1.000` tx `~9,9 s` con `0 %` de errores HTTP.
- **Piso de confirmación de `1` a `1.000` (~`23–24 s`):** mientras entren en `VENTANA_SEGUNDOS=30` del NCT, domina el ciclo de cierre de cajas; `1.000` tx (`10` bloques de `100`) termina casi igual de rápido que `1` tx.
- **Tope 1 — Concurrencia sobre las mismas cuentas (`10.000` con `VUS=10`):** con 10 VUs en paralelo contra 2 pods de backend, los 10 lotes se serializan e intercalan durante `49–67 s` y mandan nonces altos de `alice, bob, carol` antes de que terminen los primeros lotes. Al pasar `ESPERA_NONCE = 60 s`, el NCT rechaza las adelantadas como **`nonce salteado`** (`9.184` rechazos en métricas de una réplica) y traba los nonces siguientes. Con **`--vus 1`** los lotes entran en orden y confirman `10.000/10.000` (`106,5 s`) y `100.000/100.000` (`19–25 min`) con el minado ocurriendo en paralelo al envío.
- **Tope 2 — Límite de RAM en Redis (`512Mi`):** como la cadena no se reinició entre corridas, al llegar a ~`280k` transacciones (`~2.740` bloques) en la 3.ª repetición de `100.000`, Redis alcanzó su `limits.memory: 512Mi` (`pilar2/k8s/redis.yaml`) y cayó por **`OOMKilled` (código `137`)**, generando `network:BusyLoadingError`, `46,0 %` de lotes fallidos y `HTTP 503`. Con reinicio previo entre corridas grandes o mayor límite de memoria (el nodo `infra` tiene `8 GiB`), cada `100.000` entra completo.
