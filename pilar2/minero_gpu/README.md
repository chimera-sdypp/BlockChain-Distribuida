# Minero GPU (BC-27)

El minero CUDA del Pilar 1 conectado al sistema. La parte HTTP es la del minero CPU (`minero_cpu/minero.py`: `/hello`, bench, `/tasks/next`, `/results`, keep-alive y «cortá»); la búsqueda la hace `minar.cu` en la GPU.

## `minar.cu`: el Hit 7 con *midstate*

El texto de un bloque mide 77 caracteres (`contrato.texto_para`: el de cada minero, con su coinbase en la raíz) y sólo cambia el nonce, que va al final. El Hit 7 hashea por nonce los 77 caracteres más hasta 20 dígitos: 2 bloques de MD5. Acá se usa el *midstate* (lo mismo que hacen los mineros de Bitcoin):

- El host pasa por MD5 los bloques completos de 64 bytes del texto, una vez por rango.
- Cada hilo arranca de ese estado y procesa sólo la cola (< 64 bytes) más su nonce: con 77 caracteres, **1 bloque de MD5 por nonce**. En la RTX 4060, con el texto de un bloque: **2,03 GH/s contra 1,50 del Hit 7**.
- Sirve para textos de cualquier largo (el Hit 7 acepta hasta 120 bytes).
- Igual que el Hit 7: un hilo por nonce, y si hay varios gana el **menor** (`atomicMin`). El host recalcula el hash del ganador, lo que además verifica a la GPU.
- **Shares (BC-63):** cada hilo que encuentra un hash con el prefijo menos un carácter lo anota con `atomicAdd` en un buffer de 64. El host se queda con los que no pasan de la solución, los vuelve a hashear y los devuelve en orden (`shares=n1,n2,…`). En la RTX 4060 no cambia la velocidad (2,19 GH/s en caliente, igual que sin shares).

Queda abierto como servidor (arrancar CUDA cuesta): por stdin `<prefijo> <min> <max> <largo>\n<texto>` y por stdout una línea `OK …` / `SIN_SOLUCION …` / `ERROR …`, como el Hit 7, con los shares al final. El minero le pasa el rango de a trozos de 2^30 nonces (~1 s) para poder cortar a mitad.

## Ejecutar

```bash
# En una máquina con GPU NVIDIA
nvcc -O3 -arch=sm_89 -o minar minero_gpu/minar.cu
MINAR=./minar GATEWAY_URL=http://<gateway>:8081 MINERO_ID=gpu-tomas python -m minero_gpu
# la primera vez genera minero.clave y muestra su cuenta: el organizador la aprueba (k8s/aprobar.sh)

# Con Docker (desde la raíz del repo; necesita nvidia-container-toolkit)
docker build -f pilar2/minero_gpu/Dockerfile -t minero-gpu .
docker run --gpus all -e GATEWAY_URL=... -e CLAVE_PRIVADA=<64 hex> -e MINERO_ID=gpu-tomas minero-gpu
```

Mismas variables que el minero CPU (`MINERO_ID` y la clave: `CLAVE_PRIVADA`, `CLAVE_SEMILLA` o `CLAVE_ARCHIVO`), más `MINAR` (el binario; en la imagen, `/usr/local/bin/minar`). La imagen se publica en `ghcr.io/chimera-sdypp/minero-gpu` al mergear a `dev` o `main`.

## En el k3s de una PC con GPU (BC-36)

Los mineros GPU del sistema en GKE corren en el k3s de la PC de un integrante (RTX 4060), no en el K3s de la cátedra: ver [`k8s/minero-gpu/`](../k8s/minero-gpu/) (preparar el k3s, desplegar, verificar y entrada y salida de GPU para BC-41).

## En Google Colab

[`colab.ipynb`](colab.ipynb) suma la GPU de Colab (T4) al pool, en paralelo con la del k3s: clona el repo, compila `minar.cu` para la GPU que tocó y corre el minero contra el gateway. Pide la clave privada del minero sin guardarla en el notebook (o genera una) y muestra la cuenta que tiene que aprobar el organizador. [Abrir en Colab](https://colab.research.google.com/github/chimera-sdypp/BlockChain-Distribuida/blob/main/pilar2/minero_gpu/colab.ipynb).

## Pruebas

- `tests/test_minero_gpu.py` corre en el CI con `tests/minar_falso.py` (el mismo protocolo con hashlib). Con `MINAR=<binario>` corre además contra la GPU: el menor nonce y los shares coinciden con Python para textos de 0 a 6000 bytes (todos los bordes del relleno de MD5: 55, 56, 63, 64…) y con el vector de prueba.
- 09/11, RTX 4060 contra el gateway del k3s: bench de 1,8 GH/s (con la GPU compartida con otra aplicación), resolvió un rango con prefijo `000000` y el NCT aceptó el bloque.
