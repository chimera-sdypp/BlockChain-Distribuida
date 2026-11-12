# Pilar 1 — Minero MD5 en CUDA

Del "hola mundo" en CUDA a un minero de Proof of Work que busca en un rango `[min, max]`, con su comparativa contra CPU. El minero del [Hit 7](hit7/) es el que usa el Pilar 2 ([`pilar2/minero_gpu`](../pilar2/minero_gpu/)).

| Hit | Qué hay | Correrlo (RTX 4060: `sm_89`, Colab T4: `sm_75`) |
|---|---|---|
| [Hit 2](hit2/) | Hola mundo CUDA y suma de vectores | `nvcc -arch=sm_89 hola.cu -o hola && ./hola` |
| [Hit 3](hit3/) | Vectores con CCCL / Thrust | `nvcc -arch=sm_89 vectors.cu -o vectors && ./vectors` |
| [Hit 4](hit4/) | MD5 propio en GPU (`md5.cuh`, lo reusan los demás) | `nvcc -arch=sm_89 md5.cu -o md5 && ./md5 "hola"` · pruebas: `bash probar.sh` |
| [Hit 5](hit5/) | Minero PoW por fuerza bruta | `nvcc -O3 -arch=sm_89 pow.cu -o pow && ./pow "bloque_inicial" "0000"` · pruebas: `bash probar.sh` |
| [Hit 6](hit6/) | Tiempo según el largo del prefijo | `python3 medir.py --largos 1-9 --repeticiones 10 && python3 graficar.py` |
| [Hit 7](hit7/) | Minero acotado a `[min, max]`, en GPU, CPU C++ y Python | `bash probar.sh` (compila y corre 27 casos en los tres) · `bash benchmark.sh` |
| [Cierre](cierre/) | Batería fija CPU vs GPU y comparativa (BC-10) | `python3 bateria.py && python3 graficar.py` |

Cada carpeta tiene su README con el detalle y las respuestas para el informe.

## Correr todo

Desde la raíz del repo, en una máquina con GPU NVIDIA, `nvcc` y `g++`:

```bash
cd pilar1
(cd hit4 && nvcc -arch=native md5.cu -o md5 && bash probar.sh)   # MD5 de la GPU contra md5sum
(cd hit5 && bash probar.sh)              # minero sin rango (compila solo)
(cd hit7 && bash probar.sh)              # minero con rango: GPU, CPU C++ y Python, 27 casos cada uno
(cd cierre && python3 bateria.py && python3 graficar.py)   # comparativa CPU vs GPU -> resultados.csv y comparativa.png
```

Las mediciones del Hit 6 (`medir.py`) y del benchmark del Hit 7 (`benchmark.sh`) tardan varios minutos y agregan o reescriben sus CSV; se corren aparte.

En Colab (T4) el repo se clona en `/content/BlockChain-Distribuida`. Las celdas son las mismas, con `!cd BlockChain-Distribuida/pilar1/<carpeta> && ...` y `-arch=sm_75` donde se compila a mano.
