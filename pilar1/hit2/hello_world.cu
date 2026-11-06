// ==============================================================================
// Hit 2 — Hola Mundo en CUDA (Ejemplo mínimo)
//
// Muestra el lanzamiento más simple posible de un kernel en GPU:
// 1 bloque con 5 hilos en paralelo.
//
// Compilación en Google Colab (Tesla T4):
//   nvcc -arch=sm_75 hello_world.cu -o hello_world
// Compilación en local (RTX 4060):
//   nvcc -arch=sm_89 hello_world.cu -o hello_world
//
// Ejecución:
//   ./hello_world
// ==============================================================================

// Paso 1: Inclusión de bibliotecas estándar de C/C++.
// Se incluye <stdio.h> para tener acceso a la función printf tanto en CPU como en GPU.
#include <stdio.h>

// Paso 2: Declaración del Kernel de CUDA.
// El calificador '__global__' indica al compilador nvcc que:
//   a) Esta función se compila para ejecutarse en el dispositivo (Device = GPU).
//   b) Esta función es el punto de entrada y se invoca desde el procesador central (Host = CPU).
//   c) Siempre debe retornar 'void'.
__global__ void helloCUDA() {
    // Cada hilo (thread) que ejecuta este kernel corre una copia independiente de este código.
    // 'threadIdx' es una variable intrínseca (predefinida por CUDA en cada hilo).
    // 'threadIdx.x' contiene el índice numérico (0, 1, 2, ...) del hilo actual dentro de su bloque.
    // La función printf aquí se ejecuta en la GPU y almacena la salida en un buffer interno
    // que luego es transferido por el runtime de CUDA hacia la consola del Host.
    printf("Hola mundo desde la GPU, hilo %d!\n", threadIdx.x);
}

// Paso 3: Punto de entrada principal en la CPU (Host).
int main() {
    // 3.1: Ejecución normal y secuencial en la CPU.
    printf("Hola mundo desde la CPU!\n");

    // 3.2: Lanzamiento del kernel en la GPU con sintaxis de configuración de ejecución:
    // La sintaxis '<<< B, T >>>' define la geometría de la grilla de procesamiento:
    //   - Primer parámetro (1): Cantidad de bloques (Grid Dimensions). Aquí usamos 1 bloque.
    //   - Segundo parámetro (5): Cantidad de hilos por bloque (Block Dimensions). Aquí usamos 5 hilos.
    // En total, la GPU planifica y ejecuta 1 * 5 = 5 hilos concurrentes de la función helloCUDA.
    helloCUDA<<<1, 5>>>();

    // 3.3: Sincronización explícita entre Host (CPU) y Device (GPU).
    // Las llamadas a kernels en CUDA son asíncronas y no bloqueantes:
    // La CPU envía la orden de ejecución a la cola de la GPU e inmediatamente continúa con la siguiente línea.
    // Si no sincronizamos, la función main() retornaría y el proceso finalizaría antes de que la GPU
    // haya tenido tiempo de ejecutar los hilos y vaciar su buffer de printf en pantalla.
    // 'cudaDeviceSynchronize()' detiene la CPU hasta que todas las operaciones previas en la GPU terminen.
    cudaDeviceSynchronize();

    // 3.4: Finalización del programa con código de salida 0 (éxito).
    return 0;
}
