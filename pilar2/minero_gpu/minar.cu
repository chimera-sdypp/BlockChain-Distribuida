// Minero GPU del sistema (BC-27): el minero del Hit 7 para bloques de cualquier largo.
//
// El Hit 7 copia la cadena entera a cada hilo (hasta 120 bytes). El texto de un bloque real
// (previous_hash + timestamp + transacciones en JSON) tiene miles de bytes, pero sólo cambia
// el nonce, que va al final. Así que se hace lo de los mineros de Bitcoin, el "midstate":
//   - el host pasa por MD5 los bloques completos de 64 bytes del texto (iguales para todo nonce);
//   - cada hilo arranca de ese estado y procesa sólo la cola (< 64 bytes) + su nonce: 1 o 2
//     bloques de MD5, sea el texto de 100 bytes o de 10 KB.
// Igual que el Hit 7: un hilo por nonce y, de haber varios, gana el MENOR.
//
// Corre como servidor para no pagar el arranque de CUDA en cada rango. Por stdin, cada pedido:
//   <prefijo_hex> <min> <max> <largo>\n<largo bytes del texto>
// y por stdout una línea por pedido (como el Hit 7):
//   OK nonce=<u64> hash=<32 hex> evaluados=<u64> segundos=<s> hps=<h/s>
//   SIN_SOLUCION evaluados=<u64> segundos=<s> hps=<h/s>
//   ERROR <motivo>
// Al arrancar escribe "LISTO <nombre de la GPU>". Con EOF en stdin, termina.
//
//   nvcc -O3 -arch=sm_89 -o minar minar.cu      (RTX 4060; ver el Dockerfile para el resto)

#include <chrono>
#include <climits>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstdarg>
#include <cstring>
#include <string>
#include <vector>
#include <cuda_runtime.h>

#include "../../pilar1/hit4/md5.cuh"
#include "../../pilar1/hit7/md5_cpu.h"

#define VERIFICAR_CUDA(llamada)                                                          \
    do {                                                                                 \
        cudaError_t err = (llamada);                                                     \
        if (err != cudaSuccess) {                                                        \
            printf("ERROR CUDA %s\n", cudaGetErrorString(err));                          \
            fflush(stdout);                                                              \
            exit(1);                                                                     \
        }                                                                                \
    } while (0)

#define SIN_GANADOR ULLONG_MAX
#define PREFIJO_MAX 32
#define LARGO_MAXIMO (4u << 20)  // 4 MB de texto: de sobra para 100 transacciones

static const uint32_t HILOS_POR_BLOQUE = 256;
static const uint32_t BLOQUES = 4096;
static const uint32_t BATCH = HILOS_POR_BLOQUE * BLOQUES;  // 2^20 nonces por tanda

__constant__ uint32_t d_estado[4];   // MD5 de los bloques completos del texto
__constant__ uint8_t d_cola[64];     // lo que sobra del texto (< 64 bytes)
__constant__ uint32_t d_cola_len;
__constant__ uint64_t d_procesados;  // bytes ya pasados por MD5 (múltiplo de 64)
__constant__ uint8_t d_prefijo_nib[PREFIJO_MAX];
__constant__ uint32_t d_prefijo_len;

__device__ inline int uint64_a_str(uint64_t val, uint8_t *dest) {
    uint8_t temp[20];
    int len = 0;
    do {
        temp[len++] = (uint8_t)('0' + (val % 10));
        val /= 10;
    } while (val > 0);
    for (int i = 0; i < len; i++) dest[i] = temp[len - 1 - i];
    return len;
}

__device__ inline bool coincide_prefijo(const uint8_t hash[16]) {
    for (uint32_t i = 0; i < d_prefijo_len; i++) {
        uint8_t nib = (i % 2 == 0) ? (hash[i / 2] >> 4) : (hash[i / 2] & 0x0F);
        if (nib != d_prefijo_nib[i]) return false;
    }
    return true;
}

__global__ void minar_kernel(uint64_t min, uint64_t base_off, uint32_t cantidad,
                             unsigned long long *g_mejor_off) {
    uint32_t idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= cantidad) return;
    uint64_t off = base_off + idx;
    if (*(volatile unsigned long long *)g_mejor_off < (unsigned long long)off) return;

    // Cola (<= 63) + nonce (<= 20) = <= 83 bytes: con el relleno, 1 o 2 bloques de 64.
    uint8_t b[128];
    uint32_t n = d_cola_len;
    for (uint32_t i = 0; i < n; i++) b[i] = d_cola[i];
    n += uint64_a_str(min + off, b + n);
    uint32_t bloques = (n + 1 + 8 <= 64) ? 1 : 2;
    b[n] = 0x80;
    for (uint32_t i = n + 1; i < bloques * 64 - 8; i++) b[i] = 0;
    uint64_t bits = (d_procesados + n) * 8;
    for (int j = 0; j < 8; j++) b[bloques * 64 - 8 + j] = (uint8_t)(bits >> (8 * j));

    uint32_t e[4] = {d_estado[0], d_estado[1], d_estado[2], d_estado[3]};
    md5_bloque(e, b);
    if (bloques == 2) md5_bloque(e, b + 64);
    uint8_t hash[16];
    for (int j = 0; j < 16; j++) hash[j] = (uint8_t)(e[j / 4] >> (8 * (j % 4)));

    if (coincide_prefijo(hash)) atomicMin(g_mejor_off, (unsigned long long)off);
}

static bool parsear_u64(const char *s, uint64_t *out) {
    if (*s == '\0') return false;
    uint64_t v = 0;
    for (const char *p = s; *p; p++) {
        if (*p < '0' || *p > '9') return false;
        uint64_t d = (uint64_t)(*p - '0');
        if (v > (UINT64_MAX - d) / 10) return false;
        v = v * 10 + d;
    }
    *out = v;
    return true;
}

static void responder(const char *formato, ...) {
    va_list args;
    va_start(args, formato);
    vprintf(formato, args);
    va_end(args);
    fflush(stdout);
}

// Atiende un pedido ya leído. El texto está en `texto` (largo `largo`).
static void minar(const char *prefijo, uint64_t min, uint64_t max, const std::vector<uint8_t> &texto,
                  unsigned long long *d_mejor_off) {
    uint32_t prefijo_len = (uint32_t)strlen(prefijo);
    uint8_t nib[PREFIJO_MAX];
    for (uint32_t i = 0; i < prefijo_len; i++) {
        char c = prefijo[i];
        nib[i] = (uint8_t)(c <= '9' ? c - '0' : c - 'a' + 10);
    }
    // Midstate: los bloques completos se hashean una vez, en el host.
    uint32_t estado[4] = {0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476};
    uint64_t completos = texto.size() / 64 * 64;
    for (uint64_t i = 0; i < completos; i += 64) cpu_md5::md5_bloque(estado, texto.data() + i);
    uint32_t cola_len = (uint32_t)(texto.size() - completos);

    VERIFICAR_CUDA(cudaMemcpyToSymbol(d_estado, estado, sizeof(estado)));
    if (cola_len) VERIFICAR_CUDA(cudaMemcpyToSymbol(d_cola, texto.data() + completos, cola_len));
    VERIFICAR_CUDA(cudaMemcpyToSymbol(d_cola_len, &cola_len, sizeof(cola_len)));
    VERIFICAR_CUDA(cudaMemcpyToSymbol(d_procesados, &completos, sizeof(completos)));
    VERIFICAR_CUDA(cudaMemcpyToSymbol(d_prefijo_nib, nib, prefijo_len));
    VERIFICAR_CUDA(cudaMemcpyToSymbol(d_prefijo_len, &prefijo_len, sizeof(prefijo_len)));
    unsigned long long mejor = SIN_GANADOR;
    VERIFICAR_CUDA(cudaMemcpy(d_mejor_off, &mejor, sizeof(mejor), cudaMemcpyHostToDevice));

    const uint64_t total = max - min;  // cantidad - 1
    auto inicio = std::chrono::steady_clock::now();
    uint64_t base_off = 0;
    for (;;) {
        uint64_t restante = total - base_off;
        uint32_t cantidad = (restante >= (uint64_t)BATCH - 1) ? BATCH : (uint32_t)(restante + 1);
        minar_kernel<<<(cantidad + HILOS_POR_BLOQUE - 1) / HILOS_POR_BLOQUE, HILOS_POR_BLOQUE>>>(
            min, base_off, cantidad, d_mejor_off);
        VERIFICAR_CUDA(cudaGetLastError());
        VERIFICAR_CUDA(cudaMemcpy(&mejor, d_mejor_off, sizeof(mejor), cudaMemcpyDeviceToHost));
        if (mejor != SIN_GANADOR || restante < BATCH) break;
        base_off += BATCH;
    }
    double segundos = std::chrono::duration<double>(std::chrono::steady_clock::now() - inicio).count();

    if (mejor == SIN_GANADOR) {
        double evaluados = (double)total + 1.0;
        responder("SIN_SOLUCION evaluados=%llu segundos=%.6f hps=%.0f\n", (unsigned long long)(total + 1),
                  segundos, segundos > 0 ? evaluados / segundos : 0.0);
        return;
    }
    // El hash del ganador se recalcula en el host con el texto completo: además verifica a la GPU.
    uint64_t nonce = min + mejor;
    std::string completo(texto.begin(), texto.end());
    completo += std::to_string(nonce);
    char hex[33];
    md5_hex(completo.data(), completo.size(), hex);
    if (strncmp(hex, prefijo, prefijo_len) != 0) {
        responder("ERROR la GPU dio el nonce %llu pero su hash %s no cumple\n", (unsigned long long)nonce, hex);
        return;
    }
    double evaluados = (double)mejor + 1.0;
    responder("OK nonce=%llu hash=%s evaluados=%llu segundos=%.6f hps=%.0f\n", (unsigned long long)nonce, hex,
              (unsigned long long)(mejor + 1), segundos, segundos > 0 ? evaluados / segundos : 0.0);
}

int main() {
    cudaDeviceProp prop;
    int dispositivo = 0;
    VERIFICAR_CUDA(cudaGetDevice(&dispositivo));
    VERIFICAR_CUDA(cudaGetDeviceProperties(&prop, dispositivo));
    unsigned long long *d_mejor_off = nullptr;
    VERIFICAR_CUDA(cudaMalloc(&d_mejor_off, sizeof(unsigned long long)));
    responder("LISTO %s\n", prop.name);

    char linea[256];
    while (fgets(linea, sizeof(linea), stdin)) {
        char prefijo[64], smin[32], smax[32], slargo[32];
        uint64_t min, max, largo;
        if (sscanf(linea, "%63s %31s %31s %31s", prefijo, smin, smax, slargo) != 4 ||
            !parsear_u64(smin, &min) || !parsear_u64(smax, &max) || !parsear_u64(slargo, &largo)) {
            responder("ERROR pedido mal formado\n");
            continue;
        }
        if (largo > LARGO_MAXIMO) {
            responder("ERROR texto de más de %u bytes\n", LARGO_MAXIMO);
            return 1;  // no se puede saltear el texto sin leerlo: mejor cortar
        }
        std::vector<uint8_t> texto(largo);
        if (largo && fread(texto.data(), 1, largo, stdin) != largo) return 1;  // stdin cerrado a mitad
        size_t n = strlen(prefijo);
        bool hex = n >= 1 && n <= PREFIJO_MAX;
        for (size_t i = 0; hex && i < n; i++) hex = (prefijo[i] >= '0' && prefijo[i] <= '9') ||
                                                    (prefijo[i] >= 'a' && prefijo[i] <= 'f');
        if (!hex) { responder("ERROR el prefijo va de 1 a 32 caracteres hex en minúscula\n"); continue; }
        if (min > max || (min == 0 && max == UINT64_MAX)) { responder("ERROR rango inválido\n"); continue; }
        minar(prefijo, min, max, texto, d_mejor_off);
    }
    cudaFree(d_mejor_off);
    return 0;
}
