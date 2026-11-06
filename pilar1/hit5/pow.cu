// Pilar 1 — Hit 5: HASH por fuerza bruta con CUDA (Proof of Work)
//
// Busca un nonce N tal que MD5(cadena + N) empiece con el prefijo dado.
// Utiliza la implementación de md5.cuh (Hit 4) ejecutada masivamente en paralelo.
//
// Compilación (Google Colab - Tesla T4):
//   nvcc -O3 -arch=sm_75 pow.cu -o pow
// Compilación (Local - RTX 4060):
//   nvcc -O3 -arch=sm_89 pow.cu -o pow
//
// Ejecución:
//   ./pow "cadena_base" "prefijo"
// Ejemplo:
//   ./pow "bloque_inicial" "0000"

#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <cctype>
#include <chrono>
#include <cuda_runtime.h>

#include "../hit4/md5.cuh"

#define VERIFICAR_CUDA(llamada)                                                         \
    do {                                                                                \
        cudaError_t err = (llamada);                                                    \
        if (err != cudaSuccess) {                                                       \
            fprintf(stderr, "Error CUDA en %s:%d: %s\n", __FILE__, __LINE__,             \
                    cudaGetErrorString(err));                                           \
            exit(EXIT_FAILURE);                                                         \
        }                                                                               \
    } while (0)

// Constantes copiadas a memoria __constant__ para broadcast rápido y sin contención
__constant__ uint8_t d_cadena[128];
__constant__ uint32_t d_cadena_len;
__constant__ char d_prefijo[33];
__constant__ uint32_t d_prefijo_len;

// Convierte un uint64_t a string decimal ASCII directamente en memoria local
__device__ inline int uint64_a_str(uint64_t val, uint8_t *dest) {
    if (val == 0) {
        dest[0] = '0';
        return 1;
    }
    uint8_t temp[24];
    int len = 0;
    while (val > 0) {
        temp[len++] = (uint8_t)('0' + (val % 10));
        val /= 10;
    }
    for (int i = 0; i < len; i++) {
        dest[i] = temp[len - 1 - i];
    }
    return len;
}

// Compara el hash MD5 (16 bytes) contra el prefijo hex (en minúsculas)
__device__ inline bool coincide_prefijo(const uint8_t hash[16], const char *prefijo, uint32_t prefijo_len) {
#pragma unroll 4
    for (uint32_t i = 0; i < prefijo_len; i++) {
        uint8_t nibble = (i % 2 == 0) ? (hash[i / 2] >> 4) : (hash[i / 2] & 0x0F);
        char c = (nibble < 10) ? ('0' + nibble) : ('a' + (nibble - 10));
        if (c != prefijo[i]) {
            return false;
        }
    }
    return true;
}

// Kernel CUDA: cada hilo prueba un nonce determinístico (base_nonce + idx)
__global__ void minar_pow_kernel(uint64_t base_nonce, uint32_t cantidad,
                                 unsigned long long *g_encontrado,
                                 unsigned long long *g_nonce_ganador,
                                 uint8_t *g_hash_ganador) {
    uint32_t idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= cantidad) return;

    // Corte rápido: abortar si otro hilo de esta tanda ya encontró la solución
    if (*g_encontrado != 0) return;

    uint64_t nonce = base_nonce + idx;

    // Buffer local por hilo para concatenar: cadena + nonce
    uint8_t buffer[160];
    for (uint32_t i = 0; i < d_cadena_len; i++) {
        buffer[i] = d_cadena[i];
    }
    int num_len = uint64_a_str(nonce, buffer + d_cadena_len);
    uint32_t total_len = d_cadena_len + num_len;

    uint8_t hash[16];
    md5(buffer, total_len, hash);

    if (coincide_prefijo(hash, d_prefijo, d_prefijo_len)) {
        // atomicCAS asegura que solo un único hilo resulte ganador
        if (atomicCAS(g_encontrado, 0ULL, 1ULL) == 0ULL) {
            *g_nonce_ganador = (unsigned long long)nonce;
            for (int i = 0; i < 16; i++) {
                g_hash_ganador[i] = hash[i];
            }
            __threadfence();
        }
    }
}

// -----------------------------------------------------------------------------
// Verificación independiente en CPU (Host)
// Implementación pura del RFC 1321 para auditar el resultado hallado por la GPU
// -----------------------------------------------------------------------------
namespace cpu_md5 {
    static const uint32_t K[64] = {
        0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501,
        0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821,
        0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, 0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8,
        0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, 0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a,
        0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70,
        0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,
        0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
        0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, 0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391
    };

    static const uint32_t S[64] = {
        7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
        5,  9, 14, 20, 5,  9, 14, 20, 5,  9, 14, 20, 5,  9, 14, 20,
        4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
        6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21
    };

    inline void md5_bloque(uint32_t estado[4], const uint8_t bloque[64]) {
        uint32_t M[16];
        for (int i = 0; i < 16; i++)
            M[i] = (uint32_t)bloque[4 * i] | ((uint32_t)bloque[4 * i + 1] << 8) |
                   ((uint32_t)bloque[4 * i + 2] << 16) | ((uint32_t)bloque[4 * i + 3] << 24);

        uint32_t a = estado[0], b = estado[1], c = estado[2], d = estado[3];
        for (int i = 0; i < 64; i++) {
            uint32_t f;
            int g;
            if (i < 16)      { f = (b & c) | (~b & d); g = i; }
            else if (i < 32) { f = (d & b) | (~d & c); g = (5 * i + 1) % 16; }
            else if (i < 48) { f = b ^ c ^ d;          g = (3 * i + 5) % 16; }
            else             { f = c ^ (b | ~d);       g = (7 * i) % 16; }
            f += a + K[i] + M[g];
            a = d;
            d = c;
            c = b;
            b += (f << S[i]) | (f >> (32 - S[i]));
        }
        estado[0] += a;
        estado[1] += b;
        estado[2] += c;
        estado[3] += d;
    }

    inline void calcular(const uint8_t *datos, uint64_t largo, uint8_t hash[16]) {
        uint32_t estado[4] = {0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476};
        uint64_t i = 0;
        for (; i + 64 <= largo; i += 64)
            md5_bloque(estado, datos + i);

        uint8_t bloque[64];
        uint32_t resto = (uint32_t)(largo - i);
        for (uint32_t j = 0; j < resto; j++) bloque[j] = datos[i + j];
        bloque[resto] = 0x80;
        for (uint32_t j = resto + 1; j < 64; j++) bloque[j] = 0;
        if (resto >= 56) {
            md5_bloque(estado, bloque);
            for (int j = 0; j < 56; j++) bloque[j] = 0;
        }
        uint64_t bits = largo * 8;
        for (int j = 0; j < 8; j++) bloque[56 + j] = (uint8_t)(bits >> (8 * j));
        md5_bloque(estado, bloque);

        for (int j = 0; j < 16; j++) hash[j] = (uint8_t)(estado[j / 4] >> (8 * (j % 4)));
    }
}

bool verificar_en_cpu(const char *cadena, uint64_t nonce, const char *prefijo_esperado, const uint8_t hash_gpu[16]) {
    char buffer[256];
    int len = snprintf(buffer, sizeof(buffer), "%s%llu", cadena, (unsigned long long)nonce);

    uint8_t hash_cpu[16];
    cpu_md5::calcular((const uint8_t *)buffer, (uint64_t)len, hash_cpu);

    // Comparar byte a byte contra el hash devuelto por la GPU
    if (memcmp(hash_cpu, hash_gpu, 16) != 0) {
        fprintf(stderr, "\n[ERROR CRÍTICO] El hash calculado en CPU difiere del de GPU.\n");
        return false;
    }

    // Convertir a representación hexadecimal
    char hash_hex[33];
    for (int i = 0; i < 16; i++) {
        sprintf(hash_hex + (i * 2), "%02x", hash_cpu[i]);
    }
    hash_hex[32] = '\0';

    // Comprobar que efectivamente empiece con el prefijo solicitado
    size_t pref_len = strlen(prefijo_esperado);
    if (strncmp(hash_hex, prefijo_esperado, pref_len) != 0) {
        fprintf(stderr, "\n[ERROR] El hash \"%s\" NO comienza con \"%s\".\n", hash_hex, prefijo_esperado);
        return false;
    }

    printf("\n------------------------------------------------------------\n");
    printf("[Verificación en CPU]:\n");
    printf("  Texto concatenado:  \"%s\"\n", buffer);
    printf("  Hash MD5 (CPU):     %s\n", hash_hex);
    printf("  Prefijo verificado: OK (coincide con \"%s\")\n", prefijo_esperado);
    printf("  Estado:             VALIDACIÓN EXITOSA\n");
    printf("------------------------------------------------------------\n");
    return true;
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "Uso: %s <cadena> <prefijo_hex>\n", argv[0]);
        fprintf(stderr, "Ejemplo: %s \"transaccion_001\" \"0000\"\n", argv[0]);
        return 1;
    }

    const char *cadena = argv[1];
    const char *raw_prefijo = argv[2];

    uint32_t cadena_len = (uint32_t)strlen(cadena);
    uint32_t prefijo_len = (uint32_t)strlen(raw_prefijo);

    if (cadena_len > 120) {
        fprintf(stderr, "Error: La cadena base no puede exceder los 120 caracteres.\n");
        return 1;
    }
    if (prefijo_len == 0 || prefijo_len > 32) {
        fprintf(stderr, "Error: El prefijo debe tener entre 1 y 32 caracteres hexadecimales.\n");
        return 1;
    }

    // Normalizar prefijo a minúsculas y validar caracteres hexadecimales
    char prefijo[33];
    for (uint32_t i = 0; i < prefijo_len; i++) {
        char c = (char)tolower(raw_prefijo[i]);
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
            fprintf(stderr, "Error: Carácter no hexadecimal inválido en el prefijo: '%c'\n", raw_prefijo[i]);
            return 1;
        }
        prefijo[i] = c;
    }
    prefijo[prefijo_len] = '\0';

    // Copiar parámetros a memoria __constant__ de la GPU
    VERIFICAR_CUDA(cudaMemcpyToSymbol(d_cadena, cadena, cadena_len));
    VERIFICAR_CUDA(cudaMemcpyToSymbol(d_cadena_len, &cadena_len, sizeof(uint32_t)));
    VERIFICAR_CUDA(cudaMemcpyToSymbol(d_prefijo, prefijo, prefijo_len + 1));
    VERIFICAR_CUDA(cudaMemcpyToSymbol(d_prefijo_len, &prefijo_len, sizeof(uint32_t)));

    // Variables de control en memoria global (Device)
    unsigned long long *d_encontrado;
    unsigned long long *d_nonce_ganador;
    uint8_t *d_hash_ganador;

    VERIFICAR_CUDA(cudaMalloc(&d_encontrado, sizeof(unsigned long long)));
    VERIFICAR_CUDA(cudaMalloc(&d_nonce_ganador, sizeof(unsigned long long)));
    VERIFICAR_CUDA(cudaMalloc(&d_hash_ganador, 16));

    VERIFICAR_CUDA(cudaMemset(d_encontrado, 0, sizeof(unsigned long long)));
    VERIFICAR_CUDA(cudaMemset(d_nonce_ganador, 0, sizeof(unsigned long long)));
    VERIFICAR_CUDA(cudaMemset(d_hash_ganador, 0, 16));

    // Dimensiones de la grilla por tanda
    const uint32_t THREADS_PER_BLOCK = 256;
    const uint32_t BLOCKS = 4096;
    const uint32_t BATCH_SIZE = THREADS_PER_BLOCK * BLOCKS; // 1.048.576 hilos por tanda

    printf("============================================================\n");
    printf("Minero PoW CUDA (Hit 5) — Sistemas Distribuidos y Paralelos\n");
    printf("============================================================\n");
    printf("Cadena base:        \"%s\"\n", cadena);
    printf("Prefijo buscado:    \"%s\" (longitud: %u caracteres hex)\n", prefijo, prefijo_len);
    printf("Configuración GPU:  %u bloques x %u hilos = %u hilos/tanda\n", BLOCKS, THREADS_PER_BLOCK, BATCH_SIZE);
    printf("Iniciando búsqueda masiva en GPU...\n");

    unsigned long long h_encontrado = 0;
    unsigned long long h_nonce_ganador = 0;
    uint8_t h_hash_ganador[16];

    uint64_t base_nonce = 0;
    uint32_t tanda = 0;

    auto tiempo_inicio = std::chrono::high_resolution_clock::now();

    while (h_encontrado == 0) {
        minar_pow_kernel<<<BLOCKS, THREADS_PER_BLOCK>>>(base_nonce, BATCH_SIZE,
                                                        d_encontrado,
                                                        d_nonce_ganador,
                                                        d_hash_ganador);
        VERIFICAR_CUDA(cudaGetLastError());

        // Verificar si algún hilo encontró solución en esta tanda
        VERIFICAR_CUDA(cudaMemcpy(&h_encontrado, d_encontrado, sizeof(unsigned long long), cudaMemcpyDeviceToHost));

        if (h_encontrado != 0) {
            VERIFICAR_CUDA(cudaMemcpy(&h_nonce_ganador, d_nonce_ganador, sizeof(unsigned long long), cudaMemcpyDeviceToHost));
            VERIFICAR_CUDA(cudaMemcpy(h_hash_ganador, d_hash_ganador, 16, cudaMemcpyDeviceToHost));
            break;
        }

        base_nonce += BATCH_SIZE;
        tanda++;

        // Feedback visual cada 10 tandas (~10 millones de hashes)
        if (tanda % 10 == 0) {
            auto t_actual = std::chrono::high_resolution_clock::now();
            std::chrono::duration<double> lapso = t_actual - tiempo_inicio;
            double h_rate = (double)base_nonce / lapso.count() / 1e6;
            printf("  [Progreso] %llu hashes probados... (%.2f MH/s)\r", (unsigned long long)base_nonce, h_rate);
            fflush(stdout);
        }
    }

    auto tiempo_fin = std::chrono::high_resolution_clock::now();
    std::chrono::duration<double> duracion = tiempo_fin - tiempo_inicio;
    double segundos = duracion.count();
    uint64_t total_hashes_evaluados = (uint64_t)h_nonce_ganador + 1;
    double hash_rate = ((double)total_hashes_evaluados / segundos) / 1e6;

    char hash_hex_str[33];
    for (int i = 0; i < 16; i++) {
        sprintf(hash_hex_str + (i * 2), "%02x", h_hash_ganador[i]);
    }
    hash_hex_str[32] = '\0';

    printf("\n\n------------------------------------------------------------\n");
    printf("¡Solución Encontrada por GPU!\n");
    printf("  Nonce ganador:      %llu\n", (unsigned long long)h_nonce_ganador);
    printf("  Hash MD5 (GPU):     %s\n", hash_hex_str);
    printf("  Tandas ejecutadas:  %u\n", tanda + 1);
    printf("  Hashes evaluados:   %llu\n", (unsigned long long)total_hashes_evaluados);
    printf("  Tiempo total:       %.4f s\n", segundos);
    printf("  Rendimiento:        %.2f MH/s (millones de hashes/segundo)\n", hash_rate);

    // Verificación obligatoria en CPU
    bool verificacion_ok = verificar_en_cpu(cadena, h_nonce_ganador, prefijo, h_hash_ganador);

    // Liberar recursos de GPU
    VERIFICAR_CUDA(cudaFree(d_encontrado));
    VERIFICAR_CUDA(cudaFree(d_nonce_ganador));
    VERIFICAR_CUDA(cudaFree(d_hash_ganador));

    return verificacion_ok ? 0 : 1;
}

