# TrP: Trabajador de Particionamiento y Pool (BC-21, BC-22, BC-60)

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

### 2. Techo inmediato por potencia viva (P5)
- `largo_prefijo(potencia_viva, SEGUNDOS_POR_BLOQUE, PREFIJO_BASE)` calcula el mayor $p$ tal que $16^p \le P \times T$.
- El prefijo final es $\min(\text{por\_tiempo}, \text{techo})$.
- **Cuándo actúa:** en el bloque inicial (si no hay bloques previos y `PREFIJO_INICIAL=0`), y cada vez que cae la potencia viva. Si la GPU se desconecta, el prefijo baja de inmediato al rearmar la tarea (por ejemplo tras `sin_solucion` o al vencer `tarea:actual` como `vencida` en el NCT), sin tener que esperar `VENTANA_AJUSTE` bloques lentos.

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
| `ESPACIO_NONCES` | `2**32` | Espacio de búsqueda `[0, ESPACIO - 1]` |
| `FRAGMENTO_PORCENTAJE` | `10` | Tamaño de cada rango en `%` del espacio (`1` a `50`) |
| `VIVO_SEGUNDOS` | `15` | Segundos desde el último keep-alive para considerar vivo a un minero |

---

## Métricas en Prometheus y Grafana

Expuestas en `/metrics` (puerto `8080`):
- `blockchain_prefijo`: largo del prefijo elegido en la última tarea.
- `blockchain_prefijo_elecciones_total{motivo}`: contador de elecciones por motivo (`inicial`, `tiempo`, `techo`, `fijo`).
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
