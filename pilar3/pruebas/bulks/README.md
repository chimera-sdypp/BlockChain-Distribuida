# Prueba de bulks: de 1 a 100.000 transacciones (BC-38)

Manda bulks de transacciones al backend por el Ingress público y mide dos cosas:

- **Lado HTTP** (k6): latencia de cada pedido de lote (p50, p95 y p99), pedidos por segundo y porcentaje de errores.
- **Lado del sistema:** cuánto tarda en confirmarse el bulk entero (hasta que la última transacción aparece en un bloque), en cuántos bloques entró y cuántas transacciones por bloque.

| Archivo | Qué hace |
|---|---|
| `bulk.js` | Script de k6: parte el bulk en lotes de hasta 1000 (`POST /api/transacciones/lote`, el máximo del backend) y los reparte entre los usuarios virtuales |
| `correr.py` | Por cada tamaño y repetición: anota las condiciones, corre k6, sigue los bloques nuevos hasta ver todo confirmado y agrega una fila a `resultados.csv` |

## Antes de correrla

1. **k6** en el PATH ([instalación](https://grafana.com/docs/k6/latest/set-up/install-k6/)) y Python 3.
2. **Saldo para el bulk más grande.** El NCT acepta una transacción si al origen le alcanza el saldo *confirmado* menos lo que ya tiene comprometido; lo que está por recibir no cuenta. Con los saldos por defecto (alice, bob y carol con 1000 cada uno) entran unas 3000 transacciones de monto 1, y el resto se rechaza. Para 100.000, cada origen necesita al menos 33.334.
   - Los saldos iniciales salen del génesis, que el NCT carga **solo cuando la cadena está vacía**. Por eso hay que poner `SALDOS_INICIALES` altos en el NCT **antes del primer despliegue** del ambiente donde se mide, por ejemplo `{"alice": 1000000000, "bob": 1000000000, "carol": 1000000000}`. Después no se pueden subir sin vaciar la cadena.
   - `correr.py` lo comprueba con `/api/saldos` antes de empezar y no corre si no alcanza (salvo con `--permitir-rechazos`).
3. **Mismas condiciones en todas las corridas.** Fijar el prefijo (en el TrP, `AJUSTAR_PREFIJO=0` y `PREFIJO_BASE`), el `FRAGMENTO_PORCENTAJE` y la cantidad de mineros, y anotarlos. El CSV guarda en cada fila los mineros vivos por tipo y el prefijo del último bloque al empezar.

## Correrla

```bash
cd pilar3/pruebas/bulks
python3 correr.py --url http://<IP del Ingress>                  # 1, 10, 100, 1000, 10000 y 100000; 3 veces cada uno
python3 correr.py --url http://<IP del Ingress> --tamanos 1,10,100 --repeticiones 1   # prueba rápida
```

| Opción | Por defecto | Qué es |
|---|---|---|
| `--tamanos` | `1,10,100,1000,10000,100000` | Tamaños del bulk |
| `--repeticiones` | 3 | Corridas por tamaño |
| `--vus` | 10 | Usuarios virtuales de k6 (nunca más que lotes) |
| `--lote` | 1000 | Transacciones por pedido (máximo del backend: 1000) |
| `--origenes` | `alice,bob,carol` | Quiénes mandan; se reparten en ronda |
| `--timeout` | 900 | Segundos máximos esperando la confirmación (si se pasa, la fila queda con `completa=no`) |
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

Los gráficos salen de este CSV (BC-42).
