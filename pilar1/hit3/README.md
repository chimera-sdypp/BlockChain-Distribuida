# Hit 3 — CCCL y Thrust

`vectors.cu` es el primer ejemplo de la sección *Vectors* de Thrust (`thrust/examples/basic_vector.cu` en [NVIDIA/cccl](https://github.com/NVIDIA/cccl)): un `host_vector` en la CPU que se copia a un `device_vector` en la GPU con una asignación.

## Ejecución

```bash
nvcc -arch=sm_89 vectors.cu -o vectors && ./vectors   # en Colab (T4): -arch=sm_75
```

No hace falta instalar nada ni pasar flags: Thrust es *header-only* y viene con el CUDA Toolkit. En CachyOS el paquete `cuda` trae `cccl` 3.5.0 como dependencia.

## Salida (RTX 4060)

```
H has size 4
H[0] = 14
H[1] = 20
H[2] = 38
H[3] = 46
H now has size 2
D[0] = 99
D[1] = 88
```
