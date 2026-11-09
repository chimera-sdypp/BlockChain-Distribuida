# Hit 5 — HASH por Fuerza Bruta con CUDA (Proof of Work)

Implementación del algoritmo de **Prueba de Trabajo (Proof of Work - PoW)** en GPU utilizando CUDA. El programa recibe una cadena base y un prefijo hexadecimal objetivo, y busca masivamente en paralelo un número entero (**nonce**) tal que:

$$\text{MD5}(\text{cadena} + \text{nonce}) \text{ comience con } \text{prefijo}$$

Reutiliza la implementación de función de hash `__device__ md5()` desarrollada en el **Hit 4** ([`pilar1/hit4/md5.cuh`](../hit4/md5.cuh)), optimizada a nivel de hilo con constantes en `__constant__` y bucles desenrollados.

---

## Archivos del Hit 5

- **`pow.cu`**: Código fuente principal en C++/CUDA. Contiene:
  - Kernel masivo `minar_pow_kernel` con asignación determinística de nonces.
  - Sincronización y corte atómico mediante `atomicCAS`.
  - Medición de tiempo de ejecución y cómputo de tasa de hash ($MH/s$).
  - Verificación estricta e independiente en CPU (Host) antes de finalizar.
- **`probar.sh`**: Script de pruebas automatizadas que compila y evalúa casos de prueba con prefijos de 1 a 5 caracteres, contrastando contra `md5sum` del sistema operativo.
- **`README.md`**: Este documento, con la explicación técnica requerida para la sección del informe.

---

## Compilación y Ejecución

### En Google Colab (Tesla T4)
```bash
!cd BlockChain-Distribuida/pilar1/hit5 && nvcc -O3 -arch=sm_75 pow.cu -o pow
!cd BlockChain-Distribuida/pilar1/hit5 && ./pow "bloque_inicial" "0000"
```

### En Entorno Local (RTX 4060)
```bash
nvcc -O3 -arch=sm_89 pow.cu -o pow
./pow "bloque_inicial" "0000"
```

### Ejecutar Batería de Pruebas
```bash
chmod +x probar.sh
./probar.sh
```

---

## Ejemplo de Ejecución (Tesla T4 en Colab / RTX 4060)

> [!NOTE]
> Una versión anterior de este README mostraba una salida con `MD5("bloque_inicial58412") = cafe3b18...`. Ese ejemplo **no era una ejecución real**: el MD5 verdadero de `bloque_inicial58412` es `10eba3db7b5c10a885ef500dc0395f14`, que no empieza con `cafe`. Abajo va, en su lugar, la salida de una ejecución real en Colab.

```bash
!cd BlockChain-Distribuida/pilar1/hit5 && nvcc -O3 -arch=sm_75 pow.cu -o pow && ./pow "bloque_inicial" "cafe"
```

```text
<<COMPLETAR CON SALIDA REAL DE COLAB>>
```

**Qué esperar (verificado con `hashlib` de Python, no con la GPU):** en la primera tanda (nonces 0 a 1.048.575) hay **19** nonces cuyo MD5 empieza con `cafe`; el menor es `88080` (`cafe0a9800028fb078bbe9328dda1d98`). Como este minero usa `atomicCAS` (gana el primer hilo que llega, no el de menor nonce), la GPU puede devolver **cualquiera** de esos 19 y el nonce puede cambiar entre ejecuciones; por la misma razón, "Hashes evaluados" (`nonce + 1`) es solo una aproximación. El Hit 7 (`pilar1/hit7`) resuelve esto con `atomicMin` y devuelve siempre el menor.

---

## Sección para el Informe: Decisiones de Diseño y Arquitectura

### 1. ¿Cómo se reparte la búsqueda entre bloques e hilos?
- **Configuración de grilla:** Se utiliza una grilla de **4096 bloques** con **256 hilos por bloque**, lo que totaliza **1.048.576 hilos concurrentes por tanda** ($2^{20}$).
- **Mapeo determinístico del nonce:** Cada hilo calcula su identificador global:
  $$\text{idx} = \text{blockIdx.x} \times \text{blockDim.x} + \text{threadIdx.x}$$
  Y evalúa exactamente el nonce:
  $$\text{nonce} = \text{base\_nonce} + \text{idx}$$
- **Memoria constante (`__constant__`):** La cadena base y el prefijo residen en memoria constante con caché broadcast para toda la grilla, evitando lecturas redundantes a memoria global por parte de los millones de hilos.
- **Conversión de enteros a texto en registros:** Cada hilo convierte su nonce a ASCII decimal en su propio buffer local (`uint64_a_str`) sin intermediación del Host ni dependencias externas.

### 2. ¿Cómo se corta la búsqueda al encontrar la solución?
- Se utiliza una variable en memoria global compartida `d_encontrado` inicializada en `0ULL`.
- **Corte temprano:** Al inicio de cada tanda, los hilos verifican `if (*g_encontrado != 0) return;`.
- **Operación atómica (`atomicCAS`):** Cuando un hilo halla un hash que cumple con el prefijo, ejecuta:
  ```cpp
  if (atomicCAS(g_encontrado, 0ULL, 1ULL) == 0ULL) {
      *g_nonce_ganador = nonce;
      // copia los 16 bytes del hash a g_hash_ganador
      __threadfence();
  }
  ```
  - `atomicCAS` (Compare-And-Swap) compara el valor actual de `g_encontrado` contra `0ULL`. Si es cero, escribe atómicamente `1ULL` y retorna `0ULL`, coronando a ese hilo como **único ganador**.
  - Si otros hilos de la misma tanda hallan una coincidencia al mismo tiempo, su llamada a `atomicCAS` retornará `1ULL` y no sobrescribirán el nonce ni el hash.
  - La barrera `__threadfence()` asegura la visibilidad de los datos en toda la GPU antes de que la CPU lea los resultados.
- La CPU consulta el estado de `d_encontrado` al término de cada tanda de 1 millón de hilos; si es distinto de cero, finaliza el ciclo sin lanzar más kernels.

### 3. Justificación: Recorrido Secuencial vs Aleatorio
Aunque el enunciado menciona informalmente probar combinaciones «aleatoriamente», se optó por un **recorrido secuencial determinístico (un número único y contiguo por hilo)** por las siguientes razones técnicas:
1. **Eliminación de colisiones y trabajo duplicado:** En una búsqueda aleatoria pura, diferentes hilos o tandas pueden generar el mismo nonce (problema del cumpleaños), desperdiciando ciclos de cómputo en hashes ya descartados. El recorrido secuencial garantiza una eficiencia del 100% en el aprovechamiento de cada intento.
2. **Exhaustividad:** Garantiza que no queda ningún número sin evaluar en el espacio explorado $[0, \text{nonce\_ganador}]$.
3. **Cero overhead de PRNG:** Evita el costo computacional y de memoria de inicializar estados pseudoaleatorios (`curandState`) para millones de hilos.
4. **Fundamento para el Hit #7:** El Hit 7 exige delimitar la búsqueda a un rango específico $[min, max]$. La arquitectura secuencial implementada aquí se adapta de forma directa y natural a la partición de rangos y a la distribución de trabajo entre múltiples nodos (Pilar 2).

### 4. Complejidad y espacio de búsqueda
La comparación se realiza sobre la representación hexadecimal del hash (base 16). Por ende, cada carácter adicional en el prefijo incrementa exponencialmente el espacio de combinaciones esperadas en un factor de 16:
- **1 carácter:** $16^1 = 16$ intentos promedio.
- **2 caracteres:** $16^2 = 256$ intentos promedio.
- **3 caracteres:** $16^3 = 4.096$ intentos promedio.
- **4 caracteres:** $16^4 = 65.536$ intentos promedio.
- **5 caracteres:** $16^5 = 1.048.576$ intentos promedio ($\approx 1$ tanda GPU).
- **6 caracteres:** $16^6 = 16.777.216$ intentos promedio ($\approx 16$ tandas GPU).

### 5. Verificación independiente en CPU
Antes de devolver código de salida 0, el programa ejecuta una rutina en CPU que recalcula el MD5 del string `cadena + nonce` y valida tanto la igualdad exacta contra los 16 bytes de la GPU como la coincidencia del prefijo hexadecimal, garantizando la fidelidad criptográfica del resultado.

