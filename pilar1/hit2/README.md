# Hit 2 — Hola mundo CUDA

`hola.cu` sigue la presentación SC11 *CUDA C/C++ Basics*: un kernel que imprime `blockIdx`/`threadIdx`, y la suma de vectores con bloques, con hilos y con los dos combinados.

## Ejecución

```bash
nvcc -arch=sm_89 hola.cu -o hola   # RTX 4060
./hola
```

Sin placa NVIDIA, en Google Colab (entorno T4):

```bash
!git clone https://github.com/chimera-sdypp/BlockChain-Distribuida.git
!cd BlockChain-Distribuida/pilar1/hit2 && nvcc -arch=sm_75 hola.cu -o hola && ./hola
```

## Salida (RTX 4060, CUDA 13.4)

```
GPU: NVIDIA GeForce RTX 4060 | compute capability 8.9 | 24 SMs | 7.7 GiB | máx. 1024 hilos por bloque
CUDA driver 13.4 | runtime 13.4

Hola desde la GPU: bloque 1, hilo 0
...
Hola desde la GPU: bloque 0, hilo 3

2. bloques en paralelo       N=512      <<<512, 1>>>  c[0]=166+740=906  -> OK
3. hilos en paralelo         N=512      <<<1, 512>>>  c[0]=330+339=669  -> OK
4. bloques e hilos           N=1000000  <<<3907, 256>>>  c[0]=864+795=1659  -> OK
```

## Decisiones

- `-arch` igual a la compute capability de la placa: `sm_89` (RTX 4060) o `sm_75` (T4).
- El bloque 1 imprime antes que el 0: el orden entre bloques no está garantizado.
- `VERIFICAR` revisa cada llamada a la API y `cudaGetLastError()` cada lanzamiento; si no, los errores pasan en silencio.
- La versión 4 calcula el índice global con `threadIdx.x + blockIdx.x * blockDim.x` y descarta los hilos de más con `if (i < n)`.
