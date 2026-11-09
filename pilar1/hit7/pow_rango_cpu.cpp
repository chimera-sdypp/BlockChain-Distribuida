// Pilar 1 — Hit 7: minero PoW en CPU (referencia) acotado a un rango [min, max].
//
// Misma búsqueda y mismo contrato que pow_rango.cu, pero en un solo hilo de CPU.
// Sirve para la comparación CPU vs GPU del enunciado y como modelo del minero CPU
// del Pilar 2 (BC-19).
//
// Busca el MENOR nonce N con min <= N <= max tal que MD5(cadena + N) empiece con el
// prefijo (N en decimal, sin ceros a la izquierda ni separador).
//
// Compilación (Colab o local, no necesita CUDA):
//   g++ -O3 -std=c++17 pow_rango_cpu.cpp -o pow_rango_cpu
//
// Ejecución:
//   ./pow_rango_cpu <cadena> <prefijo_hex> <min> <max>
// Ejemplo:
//   ./pow_rango_cpu "hit7_chimera" "000" 0 20000
//   -> OK nonce=4409 hash=00094898d022e691b49d68a88e947b2f evaluados=4410 segundos=... hps=...
//
// stdout: una sola línea (OK ... / SIN_SOLUCION ...). Todo lo demás va a stderr.
// Código de salida: 0 = encontró, 2 = sin solución, 1 = error.

#include <chrono>
#include <cstdint>
#include <cstdio>

#include "cli_rango.h"
#include "md5_cpu.h"

// Compara los primeros `largo` nibbles del hash contra el prefijo ya convertido a
// valores 0-15 (evita pasar el hash entero a hex en cada intento).
static inline bool coincide_prefijo(const uint8_t hash[16], const uint8_t *nibbles, uint32_t largo) {
    for (uint32_t i = 0; i < largo; i++) {
        uint8_t nib = (i % 2 == 0) ? (hash[i / 2] >> 4) : (hash[i / 2] & 0x0F);
        if (nib != nibbles[i]) return false;
    }
    return true;
}

int main(int argc, char **argv) {
    ArgsRango a;
    if (!parsear_args(argc, argv, &a)) return SALIDA_ERROR;

    uint8_t nibbles[PREFIJO_MAX];
    for (uint32_t i = 0; i < a.prefijo_len; i++) {
        char c = a.prefijo[i];
        nibbles[i] = (uint8_t)(c <= '9' ? c - '0' : c - 'a' + 10);
    }

    // total = max - min = cantidad - 1. No desborda y, como se rechaza el rango
    // completo, total + 1 tampoco.
    const uint64_t total = a.max - a.min;

    fprintf(stderr, "Minero PoW CPU (Hit 7) — 1 hilo\n");
    fprintf(stderr, "  cadena=\"%s\" prefijo=\"%s\" rango=[%llu, %llu] (%llu nonces)\n",
            a.cadena, a.prefijo, (unsigned long long)a.min, (unsigned long long)a.max,
            (unsigned long long)(total + 1));

    char buffer[CADENA_MAX + 21];
    uint8_t hash[16];
    bool encontrado = false;
    uint64_t off = 0;

    auto t_inicio = std::chrono::steady_clock::now();
    auto t_ultimo_progreso = t_inicio;

    // Se recorre por offset y se corta con `off == total` ANTES de incrementar:
    // así max = 2^64-1 no da la vuelta a 0 y el bucle termina.
    for (off = 0;; ++off) {
        uint64_t nonce = a.min + off;  // <= max, no desborda
        uint32_t largo = armar_texto(a.cadena, a.cadena_len, nonce, buffer);
        cpu_md5::calcular((const uint8_t *)buffer, largo, hash);
        if (coincide_prefijo(hash, nibbles, a.prefijo_len)) {
            encontrado = true;
            break;
        }
        if (off == total) break;

        // Progreso a stderr como mucho 1 vez por segundo (se mira el reloj cada 2^20 nonces).
        if ((off & 0xFFFFF) == 0xFFFFF) {
            auto ahora = std::chrono::steady_clock::now();
            if (ahora - t_ultimo_progreso >= std::chrono::seconds(1)) {
                double s = std::chrono::duration<double>(ahora - t_inicio).count();
                fprintf(stderr, "  [progreso] %llu / %llu nonces (%.2f MH/s)\n",
                        (unsigned long long)(off + 1), (unsigned long long)(total + 1),
                        (double)(off + 1) / s / 1e6);
                t_ultimo_progreso = ahora;
            }
        }
    }

    auto t_fin = std::chrono::steady_clock::now();
    double segundos = std::chrono::duration<double>(t_fin - t_inicio).count();

    if (!encontrado) {
        imprimir_sin_solucion(total + 1, segundos);
        return SALIDA_SIN_SOLUCION;
    }

    uint64_t nonce = a.min + off;
    char hash_hex[33];
    if (!verificar_nonce(&a, nonce, hash_hex)) return SALIDA_ERROR;
    imprimir_ok(nonce, hash_hex, off + 1, segundos);
    return SALIDA_OK;
}
