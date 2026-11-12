// Recibe un string y calcula su MD5 en la GPU.
//
// Compilar:  nvcc -arch=sm_89 md5.cu -o md5     (RTX 4060; en Colab con T4: -arch=sm_75)
// Ejecutar:  ./md5 "texto"

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "md5.cuh"

#define VERIFICAR(llamada)                                                              \
    do {                                                                                \
        cudaError_t err = (llamada);                                                    \
        if (err != cudaSuccess) {                                                       \
            fprintf(stderr, "Error CUDA en %s:%d: %s\n", __FILE__, __LINE__,             \
                    cudaGetErrorString(err));                                           \
            exit(EXIT_FAILURE);                                                         \
        }                                                                               \
    } while (0)

// Un solo hash no se puede repartir entre hilos (cada bloque depende del anterior): lo hace un hilo.
__global__ void calcular_md5(const uint8_t *texto, uint64_t largo, uint8_t *hash) {
    md5(texto, largo, hash);
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "Uso: %s \"texto\"\n", argv[0]);
        return 1;
    }
    const char *texto = argv[1];
    size_t largo = strlen(texto);

    uint8_t *d_texto, *d_hash, hash[16];
    VERIFICAR(cudaMalloc(&d_texto, largo + 1));  // +1: el string vacío también es válido
    VERIFICAR(cudaMalloc(&d_hash, sizeof hash));
    VERIFICAR(cudaMemcpy(d_texto, texto, largo, cudaMemcpyHostToDevice));

    calcular_md5<<<1, 1>>>(d_texto, largo, d_hash);
    VERIFICAR(cudaGetLastError());
    VERIFICAR(cudaMemcpy(hash, d_hash, sizeof hash, cudaMemcpyDeviceToHost));

    for (int i = 0; i < 16; i++) printf("%02x", hash[i]);
    printf("\n");

    VERIFICAR(cudaFree(d_texto));
    VERIFICAR(cudaFree(d_hash));
    return 0;
}
