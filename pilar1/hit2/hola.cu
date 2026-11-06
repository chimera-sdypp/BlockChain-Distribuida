// ==============================================================================
// Hit 2 — Hola Mundo y Suma de Vectores en CUDA
// Siguiendo la progresión pedagógica del tutorial SC11 «CUDA C/C++ Basics»:
//   1. Hola Mundo con múltiples bloques e hilos concurrentes.
//   2. Suma de vectores con paralelismo por bloques (add<<<N, 1>>>).
//   3. Suma de vectores con paralelismo por hilos   (add<<<1, N>>>).
//   4. Bloques e hilos combinados con cálculo de índice global y control de borde.
//
// Compilación en Google Colab (Tesla T4):
//   nvcc -arch=sm_75 hola.cu -o hola
// Compilación en local (CachyOS con RTX 4060):
//   nvcc -arch=sm_89 hola.cu -o hola
//
// Ejecución:
//   ./hola
// ==============================================================================

// Inclusión de librerías estándar de C/C++ para E/S, gestión de memoria y utilidades generales.
#include <cstdio>
#include <cstdlib>

// ==============================================================================
// Macro de Control y Verificación de Errores en la API de CUDA
//
// Casi todas las funciones del runtime de CUDA (cudaMalloc, cudaMemcpy, etc.)
// retornan un código de tipo 'cudaError_t'. Si ocurre un error (por ejemplo,
// falta de memoria VRAM o parámetros inválidos) y no se evalúa el retorno,
// el programa continuará en estado inconsistente o fallará en silencio.
// Esta macro evalúa la llamada: si hay error, imprime el archivo, la línea,
// el mensaje descriptivo traducido por 'cudaGetErrorString' y finaliza la ejecución.
// ==============================================================================
#define VERIFICAR(llamada)                                                              \
    do {                                                                                \
        cudaError_t err = (llamada);                                                    \
        if (err != cudaSuccess) {                                                       \
            fprintf(stderr, "Error CUDA en %s:%d: %s\n", __FILE__, __LINE__,             \
                    cudaGetErrorString(err));                                           \
            exit(EXIT_FAILURE);                                                         \
        }                                                                               \
    } while (0)

// ==============================================================================
// 1. Kernel Hola Mundo Multibloque y Multihilo
//
// Calificador '__global__':
//   - Indica que es un kernel de CUDA que se ejecuta en la GPU (Device).
//   - Se invoca directamente desde la CPU (Host).
//   - Siempre tiene tipo de retorno 'void'.
//
// Variables integradas de CUDA disponibles en cada hilo:
//   - 'blockIdx.x':  Identificador del bloque dentro de la grilla (0 .. gridDim.x - 1).
//   - 'threadIdx.x': Identificador del hilo dentro de su bloque (0 .. blockDim.x - 1).
// ==============================================================================
__global__ void hola() {
    // Cada hilo individual ejecuta este printf e imprime sus coordenadas (bloque e hilo).
    // Notar que el orden en que los mensajes aparecen en la terminal no es determinístico,
    // ya que los hilos y bloques se planifican en paralelo sobre los Streaming Multiprocessors (SMs).
    printf("Hola desde la GPU: bloque %d, hilo %d\n", blockIdx.x, threadIdx.x);
}

// ==============================================================================
// 2. Variante 2: Paralelismo por Bloques (add<<<N, 1>>>)
//
// Esquema: Se lanzan N bloques, donde cada bloque tiene exactamente 1 solo hilo.
// Cada bloque se encarga de sumar un único elemento del vector usando su índice de bloque.
// ==============================================================================
__global__ void sumar_bloques(const int *a, const int *b, int *c) {
    // blockIdx.x actúa como el índice del elemento a procesar en el vector
    c[blockIdx.x] = a[blockIdx.x] + b[blockIdx.x];
}

// ==============================================================================
// 3. Variante 3: Paralelismo por Hilos (add<<<1, N>>>)
//
// Esquema: Se lanza 1 solo bloque con N hilos concurrentes.
// Cada hilo suma un elemento del vector indexado por su identificador de hilo.
// Limitación de hardware: En CUDA, un bloque no puede exceder los 1024 hilos (maxThreadsPerBlock).
// Por lo tanto, este esquema solo funciona para vectores pequeños (N <= 1024).
// ==============================================================================
__global__ void sumar_hilos(const int *a, const int *b, int *c) {
    // threadIdx.x actúa como el índice del elemento en el vector
    c[threadIdx.x] = a[threadIdx.x] + b[threadIdx.x];
}

// ==============================================================================
// 4. Variante 4: Bloques e Hilos Combinados (Escalabilidad Total)
//
// Esquema general de producción en CUDA para cualquier tamaño de vector N:
// Se utiliza una grilla de múltiples bloques, y cada bloque contiene múltiples hilos
// (típicamente múltiplos del tamaño de warp de 32, ej: 128, 256 o 512).
// ==============================================================================
__global__ void sumar(const int *a, const int *b, int *c, int n) {
    // Fórmula fundamental de mapeo 1D en CUDA:
    // Calcula el índice lineal global único del hilo a lo largo de toda la grilla:
    //   - 'blockIdx.x * blockDim.x': cantidad de hilos en todos los bloques anteriores.
    //   - '+ threadIdx.x': desplazamiento (offset) del hilo dentro del bloque actual.
    int i = threadIdx.x + blockIdx.x * blockDim.x;

    // Control de límite de frontera (Boundary Check):
    // Como el número total de hilos lanzados es un múltiplo del tamaño del bloque,
    // casi siempre se lanzan más hilos que el tamaño del vector (bloques * hilos >= n).
    // Esta condición asegura que los hilos sobrantes no lean ni escriban fuera de memoria asignada.
    if (i < n) {
        c[i] = a[i] + b[i];
    }
}

// ==============================================================================
// Función Auxiliar de Prueba y Verificación
//
// Realiza el ciclo de vida completo de un cómputo en CUDA:
//   1. Asignación e inicialización de memoria en Host (CPU).
//   2. Asignación de memoria en Device (GPU) mediante cudaMalloc.
//   3. Transferencia de datos Host -> Device vía bus PCIe mediante cudaMemcpy.
//   4. Configuración del lanzamiento del kernel (bloques e hilos).
//   5. Ejecución del kernel en la GPU y captura de errores de lanzamiento.
//   6. Transferencia de resultados Device -> Host mediante cudaMemcpy.
//   7. Verificación de exactitud matemática comparando contra la CPU.
//   8. Liberación de recursos tanto en GPU (cudaFree) como en CPU (free).
// ==============================================================================
static bool probar_suma(const char *nombre, int variante, int n, int hilos_por_bloque) {
    // Paso 1: Cálculo del tamaño en bytes para almacenar n enteros.
    size_t tam = n * sizeof(int);

    // Paso 2: Reserva de memoria en el Host (RAM del sistema).
    int *a = (int *)malloc(tam);
    int *b = (int *)malloc(tam);
    int *c = (int *)malloc(tam);

    // Paso 3: Inicialización de los vectores de entrada con valores pseudoaleatorios (0 a 999).
    for (int i = 0; i < n; i++) {
        a[i] = rand() % 1000;
        b[i] = rand() % 1000;
    }

    // Paso 4: Declaración de punteros para la memoria del Device (VRAM de la GPU).
    // Estos punteros apuntan a direcciones físicas en la GPU; desreferenciarlos
    // directamente desde el código de la CPU causaría una violación de segmento (Segmentation Fault).
    int *d_a, *d_b, *d_c;

    // Paso 5: Asignación de memoria en la VRAM de la GPU mediante 'cudaMalloc'.
    // Toma la dirección del puntero y la cantidad de bytes requerida.
    VERIFICAR(cudaMalloc(&d_a, tam));
    VERIFICAR(cudaMalloc(&d_b, tam));
    VERIFICAR(cudaMalloc(&d_c, tam));

    // Paso 6: Copia de datos desde la memoria del Host (RAM) a la memoria del Device (VRAM).
    // La bandera 'cudaMemcpyHostToDevice' especifica el sentido del copiado a través del bus PCIe.
    VERIFICAR(cudaMemcpy(d_a, a, tam, cudaMemcpyHostToDevice));
    VERIFICAR(cudaMemcpy(d_b, b, tam, cudaMemcpyHostToDevice));

    // Paso 7: Configuración y lanzamiento del kernel según la variante a probar.
    int bloques = 0, hilos = 0;
    switch (variante) {
        case 2:
            // Variante 2: N bloques de 1 hilo
            bloques = n;
            hilos = 1;
            sumar_bloques<<<bloques, hilos>>>(d_a, d_b, d_c);
            break;

        case 3:
            // Variante 3: 1 bloque de N hilos (solo válido para n <= 1024)
            bloques = 1;
            hilos = n;
            sumar_hilos<<<bloques, hilos>>>(d_a, d_b, d_c);
            break;

        default:
            // Variante 4: Combinación de bloques e hilos
            hilos = hilos_por_bloque;
            // Cálculo del número de bloques con redondeo hacia arriba:
            // División entera equivalente a: ceil((double)n / hilos)
            // Asegura que (bloques * hilos) sea mayor o igual a n para cubrir todo el arreglo.
            bloques = (n + hilos - 1) / hilos;
            sumar<<<bloques, hilos>>>(d_a, d_b, d_c, n);
            break;
    }

    // Paso 8: Detección de errores en la configuración o lanzamiento del kernel.
    // 'cudaGetLastError' captura si los parámetros de la grilla excedían los límites del hardware
    // o si hubo problemas de compilación en el dispatch del kernel.
    VERIFICAR(cudaGetLastError());

    // Paso 9: Transferencia del resultado de vuelta desde la GPU hacia la CPU.
    // La función 'cudaMemcpy' con 'cudaMemcpyDeviceToHost' es síncrona con respecto al host:
    // bloquea la ejecución de la CPU hasta que la GPU termine de calcular y se complete la copia.
    VERIFICAR(cudaMemcpy(c, d_c, tam, cudaMemcpyDeviceToHost));

    // Paso 10: Verificación de corrección en la CPU.
    // Compara elemento por elemento el resultado computado en paralelo por la GPU
    // contra la suma secuencial calculada en el Host.
    int errores = 0;
    for (int i = 0; i < n; i++) {
        if (c[i] != a[i] + b[i]) {
            errores++;
        }
    }

    // Paso 11: Mostrar resultados formateados por consola.
    printf("%-28s N=%-8d <<<%d, %d>>>  c[0]=%d+%d=%d  -> %s\n",
           nombre, n, bloques, hilos, a[0], b[0], c[0],
           errores ? "ERROR" : "OK");

    // Paso 12: Liberación de memoria en la GPU (VRAM) mediante 'cudaFree'.
    // Es indispensable para evitar pérdidas de memoria (memory leaks) en la GPU.
    VERIFICAR(cudaFree(d_a));
    VERIFICAR(cudaFree(d_b));
    VERIFICAR(cudaFree(d_c));

    // Paso 13: Liberación de la memoria en la CPU (RAM del sistema).
    free(a);
    free(b);
    free(c);

    // Retorna true si todos los elementos coincidieron exactamente.
    return errores == 0;
}

// ==============================================================================
// Función Principal (main)
// ==============================================================================
int main() {
    // --------------------------------------------------------------------------
    // Parte 1: Inspección y Diagnóstico del Hardware de la GPU (Host Side)
    // --------------------------------------------------------------------------
    int dispositivo;
    cudaDeviceProp p;

    // Obtener el identificador del dispositivo GPU activo
    VERIFICAR(cudaGetDevice(&dispositivo));

    // Consultar las propiedades y capacidades de cómputo del dispositivo
    VERIFICAR(cudaGetDeviceProperties(&p, dispositivo));

    // Consultar las versiones instaladas del driver y del runtime de CUDA
    int driver, runtime;
    VERIFICAR(cudaDriverGetVersion(&driver));
    VERIFICAR(cudaRuntimeGetVersion(&runtime));

    // Mostrar métricas clave del hardware (útiles para el informe):
    // Nombre, Compute Capability (major.minor), cantidad de SMs, VRAM total y límite de hilos por bloque.
    printf("GPU: %s | compute capability %d.%d | %d SMs | %.1f GiB | máx. %d hilos por bloque\n",
           p.name, p.major, p.minor, p.multiProcessorCount,
           p.totalGlobalMem / (1024.0 * 1024 * 1024),
           p.maxThreadsPerBlock);
    printf("CUDA driver %d.%d | runtime %d.%d\n\n",
           driver / 1000, (driver % 1000) / 10,
           runtime / 1000, (runtime % 1000) / 10);

    // --------------------------------------------------------------------------
    // Parte 2: Ejecución de Hola Mundo con 2 bloques de 4 hilos (8 hilos en total)
    // --------------------------------------------------------------------------
    hola<<<2, 4>>>();
    VERIFICAR(cudaGetLastError());

    // 'cudaDeviceSynchronize' obliga a la CPU a pausar hasta que la GPU termine de imprimir.
    // Sin esta sincronización, el programa podría finalizar antes de que los hilos vacíen su buffer.
    VERIFICAR(cudaDeviceSynchronize());
    printf("\n");

    // --------------------------------------------------------------------------
    // Parte 3: Batería de Pruebas de Suma de Vectores (SC11 Progression)
    // --------------------------------------------------------------------------
    // Inicializar la semilla de números aleatorios para reproducibilidad
    srand(42);
    bool ok = true;

    // Test 1: N bloques de 1 hilo (N = 512)
    ok &= probar_suma("2. bloques en paralelo", 2, 512, 0);

    // Test 2: 1 bloque de N hilos (N = 512)
    ok &= probar_suma("3. hilos en paralelo", 3, 512, 0);

    // Test 3: Bloques e hilos combinados (N = 1.000.000 elementos, 256 hilos por bloque)
    // Demuestra el cálculo dinámico de bloques y la protección contra desbordamiento con 'i < n'.
    ok &= probar_suma("4. bloques e hilos", 4, 1000000, 256);

    // Salida final del programa
    return ok ? EXIT_SUCCESS : EXIT_FAILURE;
}
