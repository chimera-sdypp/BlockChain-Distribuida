// Hola mundo en CUDA, siguiendo la progresión de la presentación SC11 «CUDA C/C++ Basics»:
//   1. un kernel que se ejecuta en la GPU e imprime desde cada hilo;
//   2. suma de vectores con bloques en paralelo  (add<<<N, 1>>>);
//   3. suma de vectores con hilos en paralelo    (add<<<1, N>>>);
//   4. bloques e hilos combinados, para un N cualquiera.
//
// Compilar:  nvcc -arch=sm_89 hola.cu -o hola     (RTX 4060; en Colab con T4: -arch=sm_75)
// Ejecutar:  ./hola

#include <cstdio>
#include <cstdlib>

// Toda llamada a la API de CUDA devuelve un código de error: si no se revisa, las fallas pasan en silencio.
#define VERIFICAR(llamada)                                                              \
    do {                                                                                \
        cudaError_t err = (llamada);                                                    \
        if (err != cudaSuccess) {                                                       \
            fprintf(stderr, "Error CUDA en %s:%d: %s\n", __FILE__, __LINE__,             \
                    cudaGetErrorString(err));                                           \
            exit(EXIT_FAILURE);                                                         \
        }                                                                               \
    } while (0)

// --- 1. Hola mundo -------------------------------------------------------------------------------
// __global__: corre en el device (GPU) y se llama desde el host (CPU).
__global__ void hola() {
    printf("Hola desde la GPU: bloque %d, hilo %d\n", blockIdx.x, threadIdx.x);
}

// --- 2. Paralelismo por bloques: cada bloque suma un elemento --------------------------------------
__global__ void sumar_bloques(const int *a, const int *b, int *c) {
    c[blockIdx.x] = a[blockIdx.x] + b[blockIdx.x];
}

// --- 3. Paralelismo por hilos: cada hilo de un único bloque suma un elemento ------------------------
__global__ void sumar_hilos(const int *a, const int *b, int *c) {
    c[threadIdx.x] = a[threadIdx.x] + b[threadIdx.x];
}

// --- 4. Bloques e hilos: índice global y control de borde ------------------------------------------
__global__ void sumar(const int *a, const int *b, int *c, int n) {
    int i = threadIdx.x + blockIdx.x * blockDim.x;
    if (i < n)  // el último bloque puede tener hilos de más
        c[i] = a[i] + b[i];
}

// Reserva los vectores en host y device, corre la variante pedida y compara contra la CPU.
static bool probar_suma(const char *nombre, int variante, int n, int hilos_por_bloque) {
    size_t tam = n * sizeof(int);
    int *a = (int *)malloc(tam), *b = (int *)malloc(tam), *c = (int *)malloc(tam);
    for (int i = 0; i < n; i++) {
        a[i] = rand() % 1000;
        b[i] = rand() % 1000;
    }

    int *d_a, *d_b, *d_c;  // punteros a memoria del device: no se pueden desreferenciar en el host
    VERIFICAR(cudaMalloc(&d_a, tam));
    VERIFICAR(cudaMalloc(&d_b, tam));
    VERIFICAR(cudaMalloc(&d_c, tam));
    VERIFICAR(cudaMemcpy(d_a, a, tam, cudaMemcpyHostToDevice));
    VERIFICAR(cudaMemcpy(d_b, b, tam, cudaMemcpyHostToDevice));

    int bloques = 0, hilos = 0;
    switch (variante) {
        case 2: bloques = n; hilos = 1; sumar_bloques<<<bloques, hilos>>>(d_a, d_b, d_c); break;
        case 3: bloques = 1; hilos = n; sumar_hilos<<<bloques, hilos>>>(d_a, d_b, d_c); break;
        default:
            hilos = hilos_por_bloque;
            bloques = (n + hilos - 1) / hilos;  // redondeo hacia arriba
            sumar<<<bloques, hilos>>>(d_a, d_b, d_c, n);
    }
    VERIFICAR(cudaGetLastError());  // errores de lanzamiento (p. ej. demasiados hilos por bloque)

    // cudaMemcpy espera a que termine el kernel antes de copiar el resultado de vuelta.
    VERIFICAR(cudaMemcpy(c, d_c, tam, cudaMemcpyDeviceToHost));

    int errores = 0;
    for (int i = 0; i < n; i++)
        if (c[i] != a[i] + b[i]) errores++;
    printf("%-28s N=%-8d <<<%d, %d>>>  c[0]=%d+%d=%d  -> %s\n", nombre, n, bloques, hilos, a[0], b[0],
           c[0], errores ? "ERROR" : "OK");

    VERIFICAR(cudaFree(d_a));
    VERIFICAR(cudaFree(d_b));
    VERIFICAR(cudaFree(d_c));
    free(a);
    free(b);
    free(c);
    return errores == 0;
}

int main() {
    // Datos del hardware, para el informe.
    int dispositivo;
    cudaDeviceProp p;
    VERIFICAR(cudaGetDevice(&dispositivo));
    VERIFICAR(cudaGetDeviceProperties(&p, dispositivo));
    int driver, runtime;
    VERIFICAR(cudaDriverGetVersion(&driver));
    VERIFICAR(cudaRuntimeGetVersion(&runtime));
    printf("GPU: %s | compute capability %d.%d | %d SMs | %.1f GiB | máx. %d hilos por bloque\n", p.name,
           p.major, p.minor, p.multiProcessorCount, p.totalGlobalMem / (1024.0 * 1024 * 1024),
           p.maxThreadsPerBlock);
    printf("CUDA driver %d.%d | runtime %d.%d\n\n", driver / 1000, (driver % 1000) / 10, runtime / 1000,
           (runtime % 1000) / 10);

    // 1. Dos bloques de cuatro hilos: el orden de las líneas no está garantizado.
    hola<<<2, 4>>>();
    VERIFICAR(cudaGetLastError());
    VERIFICAR(cudaDeviceSynchronize());  // sin esto, main puede terminar antes de que la GPU imprima
    printf("\n");

    srand(42);
    bool ok = true;
    ok &= probar_suma("2. bloques en paralelo", 2, 512, 0);
    ok &= probar_suma("3. hilos en paralelo", 3, 512, 0);
    ok &= probar_suma("4. bloques e hilos", 4, 1000000, 256);  // N no múltiplo de 256
    return ok ? EXIT_SUCCESS : EXIT_FAILURE;
}
