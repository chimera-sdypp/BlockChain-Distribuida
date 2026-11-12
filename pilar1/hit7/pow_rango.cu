// Pilar 1 — Hit 7: minero PoW en GPU (CUDA) acotado a un rango [min, max].
//
// Busca el MENOR nonce N con min <= N <= max tal que MD5(cadena + N) empiece con el
// prefijo (N en decimal, sin ceros a la izquierda ni separador). Si no hay ninguno
// en el rango, lo informa. Basado en el minero del Hit 5 (pilar1/hit5/pow.cu) y en el
// MD5 de GPU del Hit 4 (pilar1/hit4/md5.cuh).
//
// Compilación (Google Colab - Tesla T4):
//   nvcc -O3 -arch=sm_75 pow_rango.cu -o pow_rango
// Compilación (Local - RTX 4060):
//   nvcc -O3 -arch=sm_89 pow_rango.cu -o pow_rango
//
// Ejecución:
//   ./pow_rango <cadena> <prefijo_hex> <min> <max>
// Ejemplo:
//   ./pow_rango "hit7_chimera" "000" 0 20000
//   -> OK nonce=4409 hash=00094898d022e691b49d68a88e947b2f evaluados=4410 segundos=... hps=...
//
// stdout: una sola línea (OK ... / SIN_SOLUCION ...), pensada para leerla desde Python
// (BC-27). Banner, progreso y verificación van a stderr.
// Código de salida: 0 = encontró, 2 = sin solución, 1 = error de argumentos o de CUDA.
//
// Cómo se recorre el rango sin desbordar 64 bits:
//   - Se trabaja con offsets: total = max - min (cantidad - 1). Como se rechaza el rango
//     completo [0, 2^64-1], total < 2^64-1 y total + 1 entra en 64 bits.
//   - Tandas de BATCH = 4096 x 256 = 2^20 offsets. La última se recorta a lo que falta
//     (se lanzan solo los bloques necesarios y los hilos sobrantes salen con idx >= cantidad).
//   - El bucle corta ANTES de hacer base_off += BATCH si esta era la última tanda, así
//     nunca se pasa del máximo (ni da la vuelta cuando max = 2^64-1).
//
// Por qué gana el MENOR nonce (salida determinística, igual a CPU y Python):
//   - Cada hilo que encuentra solución hace atomicMin(g_mejor_off, su_offset).
//   - Las tandas van en orden creciente: si una tanda encontró algo, las siguientes
//     solo tienen offsets mayores y se corta.
//   - El hash del ganador se recalcula en el host con md5_cpu.h (atomicMin no puede
//     copiarlo junto con el offset), lo que además sirve de verificación independiente.
//
// Revisión de memoria: compute-sanitizer ./pow_rango hit7_chimera 000 0 20000

#include <chrono>
#include <climits>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cuda_runtime.h>

#include "../hit4/md5.cuh"
#include "cli_rango.h"
#include "md5_cpu.h"

#define VERIFICAR_CUDA(llamada)                                                     \
    do {                                                                            \
        cudaError_t err = (llamada);                                                \
        if (err != cudaSuccess) {                                                   \
            fprintf(stderr, "Error CUDA en %s:%d: %s\n", __FILE__, __LINE__,         \
                    cudaGetErrorString(err));                                       \
            exit(SALIDA_ERROR);                                                     \
        }                                                                           \
    } while (0)

// Centinela de "todavía no hay solución". Nunca es un offset válido porque el
// offset máximo es total = max - min < 2^64 - 1 (se rechaza el rango completo).
#define SIN_GANADOR ULLONG_MAX

// Dimensiones de cada tanda (igual que el Hit 5).
static const uint32_t HILOS_POR_BLOQUE = 256;
static const uint32_t BLOQUES = 4096;
static const uint32_t BATCH = HILOS_POR_BLOQUE * BLOQUES;  // 1.048.576 = 2^20

// Parámetros de la búsqueda en memoria __constant__ (broadcast a todos los hilos).
__constant__ uint8_t d_cadena[128];
__constant__ uint32_t d_cadena_len;
__constant__ uint8_t d_prefijo_nib[PREFIJO_MAX];  // prefijo como valores 0-15
__constant__ uint32_t d_prefijo_len;

// Convierte un uint64_t a decimal ASCII (sin ceros a la izquierda) y devuelve el largo.
__device__ inline int uint64_a_str(uint64_t val, uint8_t *dest) {
    uint8_t temp[20];  // 2^64 - 1 tiene 20 dígitos
    int len = 0;
    do {
        temp[len++] = (uint8_t)('0' + (val % 10));
        val /= 10;
    } while (val > 0);
    for (int i = 0; i < len; i++) dest[i] = temp[len - 1 - i];
    return len;
}

// Compara los primeros d_prefijo_len nibbles del hash contra el prefijo.
__device__ inline bool coincide_prefijo(const uint8_t hash[16]) {
    for (uint32_t i = 0; i < d_prefijo_len; i++) {
        uint8_t nib = (i % 2 == 0) ? (hash[i / 2] >> 4) : (hash[i / 2] & 0x0F);
        if (nib != d_prefijo_nib[i]) return false;
    }
    return true;
}

// Cada hilo prueba el offset base_off + idx, es decir el nonce min + base_off + idx.
__global__ void minar_rango_kernel(uint64_t min, uint64_t base_off, uint32_t cantidad,
                                   unsigned long long *g_mejor_off) {
    uint32_t idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= cantidad) return;  // última tanda recortada: no pasarse de max

    uint64_t off = base_off + idx;

    // Corte temprano: si otro hilo ya encontró un offset menor, este no puede ganar.
    if (*(volatile unsigned long long *)g_mejor_off < (unsigned long long)off) return;

    uint64_t nonce = min + off;  // <= max, no desborda

    // cadena (<= 120 bytes) + nonce (<= 20 dígitos) <= 140 bytes.
    uint8_t buffer[160];
    for (uint32_t i = 0; i < d_cadena_len; i++) buffer[i] = d_cadena[i];
    int num_len = uint64_a_str(nonce, buffer + d_cadena_len);

    uint8_t hash[16];
    md5(buffer, d_cadena_len + num_len, hash);

    if (coincide_prefijo(hash)) {
        // Si hay varios ganadores en la tanda queda el menor offset.
        atomicMin(g_mejor_off, (unsigned long long)off);
    }
}

int main(int argc, char **argv) {
    ArgsRango a;
    if (!parsear_args(argc, argv, &a)) return SALIDA_ERROR;

    uint8_t prefijo_nib[PREFIJO_MAX];
    for (uint32_t i = 0; i < a.prefijo_len; i++) {
        char c = a.prefijo[i];
        prefijo_nib[i] = (uint8_t)(c <= '9' ? c - '0' : c - 'a' + 10);
    }

    // total = max - min = cantidad - 1 (no desborda; total + 1 tampoco).
    const uint64_t total = a.max - a.min;

    // Preparación de la GPU (no entra en el tiempo medido).
    int dispositivo = 0;
    cudaDeviceProp prop;
    VERIFICAR_CUDA(cudaGetDevice(&dispositivo));
    VERIFICAR_CUDA(cudaGetDeviceProperties(&prop, dispositivo));

    if (a.cadena_len > 0)
        VERIFICAR_CUDA(cudaMemcpyToSymbol(d_cadena, a.cadena, a.cadena_len));
    VERIFICAR_CUDA(cudaMemcpyToSymbol(d_cadena_len, &a.cadena_len, sizeof(uint32_t)));
    VERIFICAR_CUDA(cudaMemcpyToSymbol(d_prefijo_nib, prefijo_nib, a.prefijo_len));
    VERIFICAR_CUDA(cudaMemcpyToSymbol(d_prefijo_len, &a.prefijo_len, sizeof(uint32_t)));

    unsigned long long *d_mejor_off = nullptr;
    unsigned long long h_mejor_off = SIN_GANADOR;
    VERIFICAR_CUDA(cudaMalloc(&d_mejor_off, sizeof(unsigned long long)));
    VERIFICAR_CUDA(cudaMemcpy(d_mejor_off, &h_mejor_off, sizeof(unsigned long long),
                              cudaMemcpyHostToDevice));

    fprintf(stderr, "Minero PoW GPU (Hit 7) — %s (sm_%d%d)\n", prop.name, prop.major, prop.minor);
    fprintf(stderr, "  cadena=\"%s\" prefijo=\"%s\" rango=[%llu, %llu] (%llu nonces)\n",
            a.cadena, a.prefijo, (unsigned long long)a.min, (unsigned long long)a.max,
            (unsigned long long)(total + 1));
    fprintf(stderr, "  grilla: hasta %u bloques x %u hilos = %u nonces por tanda\n",
            BLOQUES, HILOS_POR_BLOQUE, BATCH);

    auto t_inicio = std::chrono::steady_clock::now();
    auto t_ultimo_progreso = t_inicio;
    uint64_t base_off = 0;
    uint64_t tandas = 0;

    for (;;) {
        // restante = offsets que quedan desde base_off, menos uno (nunca desborda).
        uint64_t restante = total - base_off;
        uint32_t cantidad = (restante >= (uint64_t)BATCH - 1) ? BATCH : (uint32_t)(restante + 1);
        uint32_t bloques = (cantidad + HILOS_POR_BLOQUE - 1) / HILOS_POR_BLOQUE;

        minar_rango_kernel<<<bloques, HILOS_POR_BLOQUE>>>(a.min, base_off, cantidad, d_mejor_off);
        VERIFICAR_CUDA(cudaGetLastError());
        // cudaMemcpy espera a que termine el kernel (y reporta errores de ejecución).
        VERIFICAR_CUDA(cudaMemcpy(&h_mejor_off, d_mejor_off, sizeof(unsigned long long),
                                  cudaMemcpyDeviceToHost));
        tandas++;

        if (h_mejor_off != SIN_GANADOR) break;  // las tandas siguientes solo tienen offsets mayores
        if (restante < BATCH) break;            // era la última tanda: no avanzar (evita desbordar)
        base_off += BATCH;                      // base_off + BATCH <= total, no desborda

        // Progreso a stderr como mucho 1 vez por segundo.
        auto ahora = std::chrono::steady_clock::now();
        if (ahora - t_ultimo_progreso >= std::chrono::seconds(1)) {
            double s = std::chrono::duration<double>(ahora - t_inicio).count();
            fprintf(stderr, "  [progreso] %llu / %llu nonces (%.2f MH/s)\n",
                    (unsigned long long)base_off, (unsigned long long)(total + 1),
                    (double)base_off / s / 1e6);
            t_ultimo_progreso = ahora;
        }
    }

    auto t_fin = std::chrono::steady_clock::now();
    double segundos = std::chrono::duration<double>(t_fin - t_inicio).count();

    VERIFICAR_CUDA(cudaFree(d_mejor_off));
    fprintf(stderr, "  tandas ejecutadas: %llu\n", (unsigned long long)tandas);

    if (h_mejor_off == SIN_GANADOR) {
        imprimir_sin_solucion(total + 1, segundos);
        return SALIDA_SIN_SOLUCION;
    }

    uint64_t off = (uint64_t)h_mejor_off;
    if (off > total) {  // no debería pasar nunca: el kernel no mira offsets > total
        fprintf(stderr, "Error interno: offset ganador %llu fuera del rango (total %llu).\n",
                (unsigned long long)off, (unsigned long long)total);
        return SALIDA_ERROR;
    }
    uint64_t nonce = a.min + off;
    char hash_hex[33];
    if (!verificar_nonce(&a, nonce, hash_hex)) return SALIDA_ERROR;
    imprimir_ok(nonce, hash_hex, off + 1, segundos);
    return SALIDA_OK;
}
