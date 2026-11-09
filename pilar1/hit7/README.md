# Hit 7 — HASH por fuerza bruta con CUDA (con límites)

Implementación del algoritmo de **Prueba de Trabajo (Proof of Work - PoW)** en GPU y CPU acotado estrictamente a un rango numérico $[min, max]$.

A diferencia del **Hit 5** (que buscaba sin cota superior desde 0 hasta encontrar cualquier colisión mediante `atomicCAS`), este programa:
1. Recibe dos parámetros enteros de 64 bits adicionales: límite inferior ($min$) y límite superior ($max$).
2. Busca exhaustivamente dentro del intervalo $[min, max]$. Si ninguna combinación genera un hash válido dentro del rango, reporta formalmente `SIN_SOLUCION`.
3. Garantiza la obtención del **menor nonce** mediante sincronización con `atomicMin`, lo que convierte la búsqueda en determinística e idéntica entre implementaciones de GPU, CPU y Python.
4. Provee una interfaz estructurada de salida en una única línea por `stdout` diseñada específicamente para la integración del servicio de minería del **Pilar 2 (BC-27)**.

---

## Archivos del Hit 7

- **`pow_rango.cu`**: Código principal en CUDA (C++). Kernel `minar_rango_kernel` con asignación determinística por offsets, `atomicMin`, recorte de última tanda y verificación cruzada en Host.
- **`pow_rango_cpu.cpp`**: Implementación de referencia en CPU (C++17, monohilo) que cumple exactamente el mismo contrato de línea de comandos y validaciones que la versión de GPU.
- **`cli_rango.h`**: Header común para Host que centraliza el parseo estricto de enteros de 64 bits, validación de argumentos y formateo de la línea de salida única.
- **`md5_cpu.h`**: Implementación independiente del algoritmo MD5 (RFC 1321) en C++ para Host, utilizada para verificar el resultado ganador y en el minero de CPU.
- **`referencia.py`**: Oráculo de referencia en Python basado exclusivamente en la librería estándar `hashlib`. Utilizado para contrastar la corrección de los algoritmos.
- **`probar.sh`**: Suite de pruebas automatizadas que evalúa 12 vectores determinísticos (con y sin solución, bordes de 64 bits), 7 casos de error de entrada y auditoría contra `md5sum`.
- **`benchmark.sh`**: Script de medición comparativa CPU vs GPU sobre rangos completos ($10^5$ a $10^{10}$) utilizando un prefijo imposible (`ffffffffffffffff`) y generando `resultados_benchmark.csv`.
- **`README.md`**: Este documento técnico.

---

## Compilación y Ejecución

### En Google Colab (Tesla T4)

Secuencia de celdas para la notebook (`SDyPP.ipynb`):

```bash
# Celda 1: Actualizar a la rama de trabajo
!cd BlockChain-Distribuida && git fetch origin && git checkout feat/hit7 && git pull origin feat/hit7

# Celda 2: Compilación de ejecutables GPU y CPU
!cd BlockChain-Distribuida/pilar1/hit7 && nvcc -O3 -arch=sm_75 pow_rango.cu -o pow_rango && g++ -O3 -std=c++17 pow_rango_cpu.cpp -o pow_rango_cpu

# Celda 3: Batería de pruebas automatizadas
!cd BlockChain-Distribuida/pilar1/hit7 && chmod +x probar.sh && ./probar.sh

# Celda 4: Benchmark comparativo CPU vs GPU y generación de CSV
!cd BlockChain-Distribuida/pilar1/hit7 && chmod +x benchmark.sh && ./benchmark.sh
!cat BlockChain-Distribuida/pilar1/hit7/resultados_benchmark.csv
```

### En Entorno Local (RTX 4060)

```bash
cd pilar1/hit7
nvcc -O3 -arch=sm_89 pow_rango.cu -o pow_rango
g++ -O3 -std=c++17 pow_rango_cpu.cpp -o pow_rango_cpu
./pow_rango "hit7_chimera" "000" 0 20000
```

---

## Interfaz de Línea de Comandos (Contrato con Pilar 2)

Las tres implementaciones (`pow_rango`, `pow_rango_cpu`, `referencia.py`) comparten idéntica interfaz CLI:

```bash
./pow_rango <cadena> <prefijo_hex> <min> <max>
```

### Reglas de Validación
- Exactamente 4 argumentos posicionales.
- `cadena`: 0 a 120 bytes (garantiza que `cadena + nonce` quepa en el buffer local de 160 bytes del hilo).
- `prefijo_hex`: 1 a 32 caracteres hexadecimales (`[0-9a-fA-F]`), insensible a mayúsculas/minúsculas.
- `min` y `max`: enteros decimales sin signo de 64 bits (`[0-9]+`), en el rango $[0, 18446744073709551615]$.
- $min \le max$. Se prohíbe el rango completo $[0, 2^{64}-1]$ debido a que la cantidad de elementos ($2^{64}$) desborda un tipo `uint64_t`.
- Ante argumentos inválidos: mensaje descriptivo en `stderr`, código de salida `1` y **stdout completamente vacío**.

### Formato de Salida en stdout
Se emite **exactamente una única línea** sin banners ni texto extra (todo diagnóstico y progreso se canaliza a `stderr`):

- **Si se encontró solución:**
  ```text
  OK nonce=<u64> hash=<32 hex> evaluados=<u64> segundos=<%.6f> hps=<%.0f>
  ```
- **Si no existe solución en el rango:**
  ```text
  SIN_SOLUCION evaluados=<u64> segundos=<%.6f> hps=<%.0f>
  ```

### Códigos de Retorno
| Código | Estado | Significado |
|:---:|:---|:---|
| `0` | `OK` | Se halló al menos una solución en el rango. Se retorna el menor nonce. |
| `2` | `SIN_SOLUCION` | Se evaluó la totalidad del rango sin hallar coincidencias. |
| `1` | `ERROR` | Argumentos inválidos, fallo de CUDA o error interno. |

### Integración en Python (Consumo desde BC-27)
```python
import subprocess

proceso = subprocess.run(
    ["./pow_rango", bloque_str, prefijo_str, str(desde_u64), str(hasta_u64)],
    capture_output=True,
    text=True
)

if proceso.returncode == 0:
    campos = proceso.stdout.strip().split()
    datos = dict(c.split("=", 1) for c in campos[1:])
    nonce_ganador = int(datos["nonce"])
    hash_ganador = datos["hash"]
    # Enviar resultado al Gateway: POST /results
elif proceso.returncode == 2:
    # Informar fin de rango sin solución: POST /results
    pass
else:
    # Manejar error de ejecución
    pass
```

---

## Decisiones de Diseño y Arquitectura

### 1. Representación por Offsets y Aritmética de 64 bits
Para evitar condiciones de desbordamiento (*integer overflow*) cuando el límite superior alcanza valores cercanos o iguales a $2^{64}-1$:
- La búsqueda opera en base a un **desplazamiento relativo** (*offset*):
  $$\text{total} = max - min$$
- Dado que se rechaza la entrada $[0, 2^{64}-1]$, la cantidad total de offsets a evaluar ($\text{total} + 1$) siempre está acotada y no desborda.
- Cada hilo $i$ dentro de una tanda calcula su nonce sumando:
  $$\text{nonce} = min + \text{base\_off} + i$$
  Dado que $\text{base\_off} + i \le \text{total}$, se garantiza que jamás supere $max$.

### 2. Recorrido en Tandas y Recorte de la Última Tanda
- La búsqueda se fragmenta en tandas concurrentes de tamaño $\text{BATCH} = 4096 \times 256 = 1.048.576$ hilos ($2^{20}$).
- En la última tanda, la cantidad restante de nonces por evaluar puede ser menor a $\text{BATCH}$. El host calcula la cantidad exacta pendiente:
  $$\text{pendientes} = \text{total} - \text{base\_off} + 1$$
- Se configuran los bloques estrictamente necesarios en la grilla y el kernel incorpora una guarda:
  ```cuda
  if (idx >= cantidad) return;
  ```
- El bucle en el Host finaliza antes de avanzar el puntero `base_off` si se alcanzó el límite del rango, evitando ciclos infinitos al operar en el límite de $2^{64}-1$.

### 3. Determinismo y Menor Nonce (`atomicMin`)
En el Hit 5 se empleaba `atomicCAS` para registrar el primer hilo que alcanzaba la instrucción. En una arquitectura masivamente paralela con miles de núcleos concurrentes, el orden de llegada de los hilos es no determinístico, por lo que distintas ejecuciones sobre el mismo rango podían devolver diferentes nonces válidos.

Para el Hit 7 se utiliza una variable global en GPU inicializada en `SIN_GANADOR` (`ULLONG_MAX`):
- Cuando un hilo halla un hash coincidente, ejecuta:
  ```cuda
  atomicMin(g_mejor_off, (unsigned long long)(base_off + idx));
  ```
- Dado que las tandas se ejecutan secuencialmente en orden ascendente de offsets, si al finalizar una tanda `g_mejor_off != SIN_GANADOR`, se interrumpe la iteración, ya que cualquier tanda posterior contendría offsets estrictamente mayores.
- El valor devuelto es **estrictamente el menor nonce del rango**, idéntico al obtenido por una búsqueda secuencial en CPU o Python.

### 4. Recálculo y Verificación en CPU
Debido a que `atomicMin` opera de forma escalar sobre un entero de 64 bits y no puede transferir atómicamente un bloque de memoria contiguo (los 16 bytes del hash MD5), el host:
1. Lee `g_mejor_off` ganador desde la GPU.
2. Calcula $\text{nonce} = min + \text{g\_mejor\_off}$.
3. Recalcula el hash en el Host utilizando la implementación de auditoría `md5_cpu.h` y verifica que comience con el prefijo esperado.
4. Emite la línea final con el hash verificado.

---

## Batería de Pruebas Automatizadas

El script `probar.sh` verifica 19 casos de prueba exhaustivos con la cadena base `hit7_chimera`:

| Identificador | Caso Evaluado | Prefijo | Min | Max | Retorno | Salida Esperada |
|:---:|:---|:---:|:---:|:---:|:---:|:---|
| **V1** | Contiene varias soluciones $\rightarrow$ Menor nonce | `000` | 0 | 20000 | 0 | `OK nonce=4409 hash=00094898d022e691b49d68a88e947b2f evaluados=4410` |
| **V2** | Intervalo entre dos soluciones | `000` | 4410 | 11381 | 2 | `SIN_SOLUCION evaluados=6972` |
| **V3** | Un solo valor numérico (es solución) | `000` | 11382 | 11382 | 0 | `OK nonce=11382 hash=000059b92efbd22f05f0389e5c4da5d8 evaluados=1` |
| **V4** | Un solo valor numérico (no es solución) | `000` | 11383 | 11383 | 2 | `SIN_SOLUCION evaluados=1` |
| **V5** | Extremo inferior absoluto (nonce = 0) | `8e99` | 0 | 0 | 0 | `OK nonce=0 hash=8e991d9c530d4fa1d5d253ba4cf6ab88 evaluados=1` |
| **V6** | Solución en el borde de tanda ($> 2^{20}$) | `00000` | 1048576 | 1437713 | 0 | `OK nonce=1437713 hash=00000aac6f881c98e26f44f9b23dbf78 evaluados=389138` |
| **V7** | Rango truncado antes de la solución | `00000` | 0 | 1437712 | 2 | `SIN_SOLUCION evaluados=1437713` |
| **V8** | Solución ubicada exactamente en `min` | `00000` | 1437713 | 2106361 | 0 | `OK nonce=1437713 hash=00000aac6f881c98e26f44f9b23dbf78 evaluados=1` |
| **V9** | Intervalo entre soluciones de 5 ceros | `00000` | 1437714 | 2106360 | 2 | `SIN_SOLUCION evaluados=668647` |
| **V10** | Nonce en rango de 64 bits | `00` | $2^{64}-5001$ | $2^{64}-1$ | 0 | `OK nonce=18446744073709546645 hash=003e50fab74c09ac4aa081cd03ced284 evaluados=31` |
| **V11** | Límite máximo absoluto $max = 2^{64}-1$ | `68ad` | $2^{64}-16$ | $2^{64}-1$ | 0 | `OK nonce=18446744073709551615 hash=68ad91213fc460ec8edf04cc5c084c73 evaluados=16` |
| **V12** | Normalización de prefijos en mayúsculas | `8E99` | 0 | 0 | 0 | `OK nonce=0 hash=8e991d9c530d4fa1d5d253ba4cf6ab88 evaluados=1` |

Casos de error de argumentos (E1 a E7, verifican código de salida `1` y `stdout` vacío):
- **E1**: $min > max$ (`20 > 10`).
- **E2**: Entero negativo en $min$ (`-1`).
- **E3**: Desbordamiento superior a 64 bits ($2^{64}$).
- **E4**: Carácter no hexadecimal en prefijo (`0g0`).
- **E5**: Rango total no particionado $[0, 2^{64}-1]$.
- **E6**: Cantidad insuficiente de parámetros (3 argumentos).
- **E7**: Caracteres alfanuméricos en parámetro numérico (`1a`).

---

## Comparación de Rendimiento: CPU vs GPU

### Metodología de Medición
Para comparar equitativamente la capacidad de procesamiento de la CPU (C++) y de la GPU (CUDA) sin sesgos aleatorios derivados de la posición del nonce ganador:
1. Se utiliza la cadena fija `hit7_bench` y un prefijo imposible de hallar en los rangos testeados (`ffffffffffffffff`).
2. Se obliga a cada implementación a recorrer **el 100% de los nonces del rango**, midiendo el tiempo total de cómputo y la tasa de hash sostenida ($H/s$).
3. Se ejecutan 3 repeticiones por cada tamaño de rango, computando los valores promedio.

### Resultados Experimentales (Google Colab - Tesla T4 vs Intel Xeon)

| Tamaño de Rango | Tiempo CPU (s) | Hashes/s CPU | Tiempo GPU (s) | Hashes/s GPU (MH/s) | Speedup (GPU / CPU) |
|:---:|:---:|:---:|:---:|:---:|:---:|
| **$10^5$ (100.000)** | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` |
| **$10^6$ (1.000.000)** | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` |
| **$10^7$ (10.000.000)** | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` |
| **$10^8$ (100.000.000)** | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` |
| **$10^9$ (1.000.000.000)** | *(Inviable por tiempo)* | *(Inviable por tiempo)* | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | — |
| **$10^{10}$ (10.000.000.000)** | *(Inviable por tiempo)* | *(Inviable por tiempo)* | `<<COMPLETAR CON SALIDA REAL>>` | `<<COMPLETAR CON SALIDA REAL>>` | — |

---

## Ejemplo de Ejecución (Google Colab - Tesla T4)

```text
<<COMPLETAR CON SALIDA REAL DE COLAB>>
```
