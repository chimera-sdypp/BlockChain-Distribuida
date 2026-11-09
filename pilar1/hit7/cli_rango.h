// Pilar 1 — Hit 7: interfaz de línea de comandos común (solo host).
//
// Parseo, validación y formato de salida compartidos por pow_rango.cu (GPU) y
// pow_rango_cpu.cpp (CPU), para que las dos implementaciones cumplan el mismo
// contrato sin divergir:
//
//   <programa> <cadena> <prefijo> <min> <max>
//
//   stdout (exactamente una línea):
//     OK nonce=<u64> hash=<32 hex> evaluados=<u64> segundos=<%.6f> hps=<%.0f>
//     SIN_SOLUCION evaluados=<u64> segundos=<%.6f> hps=<%.0f>
//
//   Código de salida: 0 = OK, 2 = SIN_SOLUCION, 1 = error de argumentos / CUDA / interno.
//   Todo mensaje de error, banner o progreso va a stderr.
//
// No incluye cuda_runtime.h: compila con g++ y con nvcc.

#ifndef CLI_RANGO_H
#define CLI_RANGO_H

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>

#include "md5_cpu.h"

#define CADENA_MAX 120  // bytes; con el nonce (hasta 20 dígitos) entra en un buffer de 160
#define PREFIJO_MAX 32  // un MD5 tiene 32 caracteres hex

enum {
    SALIDA_OK = 0,
    SALIDA_ERROR = 1,
    SALIDA_SIN_SOLUCION = 2,
};

struct ArgsRango {
    const char *cadena;
    uint32_t cadena_len;
    char prefijo[PREFIJO_MAX + 1];  // normalizado a minúsculas
    uint32_t prefijo_len;
    uint64_t min;
    uint64_t max;
};

// Convierte un decimal sin signo a uint64_t. Acepta solo [0-9]+ (sin signo, sin
// espacios, sin 0x). Devuelve false si está vacío, tiene otro carácter o pasa 2^64-1.
// Se hace a mano porque strtoull acepta '-' (y "-1" da 2^64-1) y espacios iniciales.
inline bool parsear_u64(const char *s, uint64_t *out) {
    if (s == nullptr || *s == '\0') return false;
    uint64_t v = 0;
    for (const char *p = s; *p; p++) {
        if (*p < '0' || *p > '9') return false;
        uint64_t d = (uint64_t)(*p - '0');
        if (v > (UINT64_MAX - d) / 10) return false;  // v * 10 + d no entra en 64 bits
        v = v * 10 + d;
    }
    *out = v;
    return true;
}

inline void imprimir_uso(const char *prog) {
    fprintf(stderr, "Uso: %s <cadena> <prefijo_hex> <min> <max>\n", prog);
    fprintf(stderr, "Ejemplo: %s \"hit7_chimera\" \"000\" 0 20000\n", prog);
}

// Valida los argumentos según el contrato. Si algo falla escribe el motivo en
// stderr y devuelve false (el llamador sale con código 1 sin tocar stdout).
inline bool parsear_args(int argc, char **argv, ArgsRango *a) {
    const char *prog = (argc > 0 && argv[0]) ? argv[0] : "pow_rango";
    if (argc != 5) {
        fprintf(stderr, "Error: se esperan exactamente 4 argumentos (se recibieron %d).\n", argc - 1);
        imprimir_uso(prog);
        return false;
    }

    // Cadena base: 0 a 120 bytes.
    a->cadena = argv[1];
    size_t cadena_len = strlen(argv[1]);
    if (cadena_len > CADENA_MAX) {
        fprintf(stderr, "Error: la cadena base no puede superar los %d bytes (tiene %zu).\n",
                CADENA_MAX, cadena_len);
        return false;
    }
    a->cadena_len = (uint32_t)cadena_len;

    // Prefijo: 1 a 32 caracteres hex, normalizado a minúsculas.
    const char *pref = argv[2];
    size_t prefijo_len = strlen(pref);
    if (prefijo_len == 0 || prefijo_len > PREFIJO_MAX) {
        fprintf(stderr, "Error: el prefijo debe tener entre 1 y %d caracteres hexadecimales.\n",
                PREFIJO_MAX);
        return false;
    }
    for (size_t i = 0; i < prefijo_len; i++) {
        char c = pref[i];
        if (c >= 'A' && c <= 'F') c = (char)(c - 'A' + 'a');  // sin tolower: no depende del locale
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
            fprintf(stderr, "Error: carácter no hexadecimal en el prefijo: '%c'.\n", pref[i]);
            return false;
        }
        a->prefijo[i] = c;
    }
    a->prefijo[prefijo_len] = '\0';
    a->prefijo_len = (uint32_t)prefijo_len;

    // Rango [min, max], enteros sin signo de 64 bits.
    if (!parsear_u64(argv[3], &a->min)) {
        fprintf(stderr, "Error: min inválido \"%s\" (solo dígitos, 0 a 18446744073709551615).\n", argv[3]);
        return false;
    }
    if (!parsear_u64(argv[4], &a->max)) {
        fprintf(stderr, "Error: max inválido \"%s\" (solo dígitos, 0 a 18446744073709551615).\n", argv[4]);
        return false;
    }
    if (a->min > a->max) {
        fprintf(stderr, "Error: min (%llu) es mayor que max (%llu).\n",
                (unsigned long long)a->min, (unsigned long long)a->max);
        return false;
    }
    // La cantidad de [0, 2^64-1] es 2^64 y no entra en 64 bits. Al rechazarlo,
    // max - min (cantidad - 1) nunca vale 2^64-1 y se puede usar de centinela.
    if (a->min == 0 && a->max == UINT64_MAX) {
        fprintf(stderr, "Error: no se admite el rango completo [0, 18446744073709551615]; "
                        "partirlo en sub-rangos.\n");
        return false;
    }
    return true;
}

// Escribe `cadena` + decimal(`nonce`) en `buffer` (debe tener al menos cadena_len + 21
// bytes) y devuelve el largo total, sin '\0'. Misma conversión que hace la GPU.
inline uint32_t armar_texto(const char *cadena, uint32_t cadena_len, uint64_t nonce, char *buffer) {
    memcpy(buffer, cadena, cadena_len);
    char tmp[20];
    int n = 0;
    do {
        tmp[n++] = (char)('0' + (nonce % 10));
        nonce /= 10;
    } while (nonce > 0);
    for (int i = 0; i < n; i++) buffer[cadena_len + i] = tmp[n - 1 - i];
    return cadena_len + (uint32_t)n;
}

// Verificación independiente del ganador: arma el texto con snprintf (otra vía que
// la del minero), calcula el MD5 en CPU y comprueba el prefijo. Deja el hash en hex.
inline bool verificar_nonce(const ArgsRango *a, uint64_t nonce, char hash_hex[33]) {
    char texto[CADENA_MAX + 32];
    int largo = snprintf(texto, sizeof(texto), "%s%llu", a->cadena, (unsigned long long)nonce);
    if (largo < 0 || (size_t)largo >= sizeof(texto)) {
        fprintf(stderr, "Error interno: no se pudo armar el texto del nonce %llu.\n",
                (unsigned long long)nonce);
        return false;
    }
    md5_hex(texto, (size_t)largo, hash_hex);
    if (strncmp(hash_hex, a->prefijo, a->prefijo_len) != 0) {
        fprintf(stderr, "Error interno: MD5(\"%s\") = %s no empieza con \"%s\".\n",
                texto, hash_hex, a->prefijo);
        return false;
    }
    fprintf(stderr, "[verificación CPU] MD5(\"%s\") = %s -> prefijo \"%s\" OK\n",
            texto, hash_hex, a->prefijo);
    return true;
}

// Redondea los segundos a la misma precisión que se imprime (%.6f). Así hps sale del
// mismo número que ve quien lee la línea: si se imprime segundos=0.000000, hps=0 (y
// nadie que recalcule evaluados/segundos divide por cero).
inline double redondear_segundos(double segundos) {
    return segundos > 0.0 ? (double)(long long)(segundos * 1e6 + 0.5) / 1e6 : 0.0;
}

inline double calcular_hps(uint64_t evaluados, double segundos) {
    return segundos > 0.0 ? (double)evaluados / segundos : 0.0;
}

// Única línea de stdout cuando hay solución.
inline void imprimir_ok(uint64_t nonce, const char hash_hex[33], uint64_t evaluados, double segundos) {
    double s = redondear_segundos(segundos);
    printf("OK nonce=%llu hash=%s evaluados=%llu segundos=%.6f hps=%.0f\n",
           (unsigned long long)nonce, hash_hex, (unsigned long long)evaluados, s,
           calcular_hps(evaluados, s));
    fflush(stdout);
}

// Única línea de stdout cuando no hay solución en el rango.
inline void imprimir_sin_solucion(uint64_t evaluados, double segundos) {
    double s = redondear_segundos(segundos);
    printf("SIN_SOLUCION evaluados=%llu segundos=%.6f hps=%.0f\n",
           (unsigned long long)evaluados, s, calcular_hps(evaluados, s));
    fflush(stdout);
}

#endif
