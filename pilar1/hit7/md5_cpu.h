// Pilar 1 — Hit 7: MD5 en CPU (solo host), extraído del Hit 5.
//
// Implementación pura del RFC 1321 (https://www.rfc-editor.org/rfc/rfc1321), sin
// dependencias de CUDA: compila tanto con g++ como con nvcc.
//
// La usan:
//   - pow_rango.cu      → verificación en host del nonce que encontró la GPU
//                         (y para obtener su hash, que atomicMin no puede devolver).
//   - pow_rango_cpu.cpp → el minero CPU de referencia.
//
// Va en un namespace propio (cpu_md5) para no chocar con md5() / md5_bloque()
// de ../hit4/md5.cuh, que son __device__.

#ifndef MD5_CPU_H
#define MD5_CPU_H

#include <cstddef>
#include <cstdint>

namespace cpu_md5 {

// K[i] = parte entera de |sin(i + 1)| * 2^32 (RFC 1321, §3.4).
static const uint32_t K[64] = {
    0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501,
    0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821,
    0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, 0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8,
    0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, 0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a,
    0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70,
    0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,
    0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
    0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, 0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391,
};

// Cuántos bits rota cada uno de los 64 pasos.
static const uint32_t S[64] = {
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
    5,  9, 14, 20, 5,  9, 14, 20, 5,  9, 14, 20, 5,  9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
};

// Procesa un bloque de 64 bytes: 4 rondas de 16 pasos sobre el estado (A, B, C, D).
inline void md5_bloque(uint32_t estado[4], const uint8_t bloque[64]) {
    uint32_t M[16];  // el bloque como 16 palabras de 32 bits, little-endian
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

// Calcula el MD5 de `largo` bytes y deja los 16 bytes del resultado en `hash`.
inline void calcular(const uint8_t *datos, uint64_t largo, uint8_t hash[16]) {
    uint32_t estado[4] = {0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476};

    uint64_t i = 0;
    for (; i + 64 <= largo; i += 64)
        md5_bloque(estado, datos + i);

    // Relleno: los bytes que sobran + 0x80 + ceros + el largo en bits (64 bits, little-endian).
    uint8_t bloque[64];
    uint32_t resto = (uint32_t)(largo - i);
    for (uint32_t j = 0; j < resto; j++) bloque[j] = datos[i + j];
    bloque[resto] = 0x80;
    for (uint32_t j = resto + 1; j < 64; j++) bloque[j] = 0;
    if (resto >= 56) {  // no entra el largo: hace falta un bloque más
        md5_bloque(estado, bloque);
        for (int j = 0; j < 56; j++) bloque[j] = 0;
    }
    uint64_t bits = largo * 8;
    for (int j = 0; j < 8; j++) bloque[56 + j] = (uint8_t)(bits >> (8 * j));
    md5_bloque(estado, bloque);

    for (int j = 0; j < 16; j++) hash[j] = (uint8_t)(estado[j / 4] >> (8 * (j % 4)));
}

// Pasa los 16 bytes del hash a 32 caracteres hex en minúsculas (+ '\0').
inline void a_hex(const uint8_t hash[16], char out[33]) {
    static const char HEX[] = "0123456789abcdef";
    for (int i = 0; i < 16; i++) {
        out[2 * i]     = HEX[hash[i] >> 4];
        out[2 * i + 1] = HEX[hash[i] & 0x0F];
    }
    out[32] = '\0';
}

}  // namespace cpu_md5

// Atajo: MD5 de `largo` bytes de `datos` directamente en hex (32 caracteres + '\0').
inline void md5_hex(const char *datos, size_t largo, char out[33]) {
    uint8_t hash[16];
    cpu_md5::calcular((const uint8_t *)datos, (uint64_t)largo, hash);
    cpu_md5::a_hex(hash, out);
}

#endif
