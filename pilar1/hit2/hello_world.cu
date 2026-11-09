// ==============================================================================
// Hit 2 — Hola Mundo en CUDA (Ejemplo mínimo y Guía del Compilador nvcc)
//
// Muestra el lanzamiento más simple posible de un kernel en GPU:
// 1 bloque con 5 hilos en paralelo.
//
// ==============================================================================
// ¿QUÉ ES EL COMPILADOR 'nvcc'?
// ==============================================================================
//
// 1. DEFINICIÓN: ¿QUÉ ES UN "COMPILADOR ORQUESTADOR" (COMPILER DRIVER)?
//    'nvcc' significa "NVIDIA CUDA Compiler". No es un compilador monolítico que hace
//    todo el trabajo por su cuenta, sino un COMPILADOR ORQUESTADOR (o "compiler driver").
//
//    ¿Qué significa "orquestador"?:
//    Un compilador tradicional toma código fuente y lo traduce directamente a binario.
//    Un compilador orquestador actúa como un "director de orquesta" o coordinador general:
//    no compila todo él mismo, sino que analiza el código, lo divide y manda a llamar
//    por detrás a una cadena de múltiples herramientas especializadas en el orden correcto:
//      - Invoca preprocesadores para separar el código de Host y de Device.
//      - Invoca al compilador nativo de la CPU ('g++' en Linux o 'cl.exe' en Windows).
//      - Invoca a los compiladores internos de NVIDIA ('cicc', 'ptxas') para la GPU.
//      - Invoca al enlazador ('linker') del sistema para unir ambos resultados.
//    Gracias a que es un orquestador, nosotros como programadores solo ejecutamos un
//    único comando ('nvcc archivo.cu -o salida') en lugar de tener que ejecutar a mano
//    5 o 6 programas y pasos intermedios distintos.
//
// 2. COMPILACIÓN HETEROGÉNEA (CPU vs GPU):
//    En un programa CUDA conviven dos procesadores completamente distintos:
//      - El Host (CPU): procesador secuencial (arquitectura x86_64 o ARM).
//      - El Device (GPU): procesador masivamente paralelo (arquitectura NVIDIA SM).
//    Una CPU no entiende el lenguaje de una GPU, y una GPU no entiende el de una CPU.
//    En este archivo (.cu) tenemos funciones para la CPU ('main') y funciones para
//    la GPU ('helloCUDA'), además de sintaxis no estándar de C++ como '<<<1, 5>>>()'.
//
// 3. ¿CÓMO TRABAJA nvcc?:
//    nvcc toma este archivo '.cu' y lo SEPARA en dos caminos de compilación:
//      a) Código de CPU (Host):
//         nvcc extrae las funciones de CPU ('main') y se las entrega al compilador
//         nativo del sistema operativo (en Linux usa 'g++', en Windows usa 'cl.exe').
//      b) Sintaxis de lanzamiento '<<< >>>':
//         La traduce a llamadas a funciones estándar de la biblioteca CUDA Runtime
//         (como 'cudaLaunchKernel').
//      c) Código de GPU (Device):
//         nvcc compila los kernels marcados con '__global__' o '__device__' generando
//         instrucciones específicas para los núcleos de la GPU.
//      d) Enlazado final (Linker):
//         Une el código máquina de la CPU y el código de la GPU en un único archivo
//         ejecutable binario unificado (denominado "fatbinary").
//
// 4. CAPAS DE CÓDIGO DE LA GPU: ¿QUÉ SON PTX Y SASS?
//    Al compilar para GPU, nvcc utiliza dos niveles de código:
//      - PTX (Parallel Thread Execution): Es un lenguaje ensamblador VIRTUAL e
//        intermedio (como el bytecode en Java). Es independiente del modelo exacto
//        de GPU y permite que el código sea portable a futuras placas.
//      - SASS (Streaming Assembler): Es el código de máquina binario REAL y físico
//        que entiende el silicio de una GPU concreta.
//
// 5. ¿QUÉ SIGNIFICA EL FLAG '-arch=sm_XX'?
//    - 'sm' hace referencia a "Streaming Multiprocessor" (la unidad básica de cálculo en GPU).
//    - 'XX' representa la "Compute Capability" (versión de arquitectura de hardware):
//         * -arch=sm_75 : Arquitectura Turing (GPU Tesla T4 en Google Colab).
//         * -arch=sm_89 : Arquitectura Ada Lovelace (NVIDIA GeForce RTX 4060 en CachyOS).
//    - Si no se especifica el flag correspondiente o se coloca uno incorrecto, la GPU
//      no podrá decodificar las instrucciones de máquina y el kernel fallará en silencio
//      (el programa correrá, pero la GPU no imprimirá nada).
//
// 6. DISTINCIÓN: DRIVER vs TOOLKIT vs RUNTIME:
//    - Driver (ej. 615.78): Software a nivel sistema operativo que habla con el hardware físico.
//    - CUDA Toolkit (ej. 13.0 / 13.4): Paquete de herramientas del desarrollador (incluye 'nvcc').
//    - CUDA Runtime API: Funciones que llamamos en C++ ('cudaDeviceSynchronize', 'cudaMalloc').
//
// ==============================================================================
// COMANDOS DE COMPILACIÓN Y EJECUCIÓN
// ==============================================================================
// Google Colab (Tesla T4):
//   nvcc -arch=sm_75 hello_world.cu -o hello_world && ./hello_world
//
// Entorno Local (RTX 4060 en CachyOS):
//   nvcc -arch=sm_89 hello_world.cu -o hello_world && ./hello_world
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
