#include <stdio.h>

__global__ void helloCUDA() {
    printf("Hola mundo desde la GPU, hilo %d!\n", threadIdx.x);
}

int main() {
    printf("Hola mundo desde la CPU!\n");
    helloCUDA<<<1, 5>>>();
    cudaDeviceSynchronize();
    return 0;
}
