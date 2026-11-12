# Minero GPU (BC-27)

El minero CUDA del Pilar 1 conectado al sistema. La parte HTTP es la del minero CPU (`minero_cpu/minero.py`: `/hello`, bench, `/tasks/next`, `/results`, keep-alive y «cortá»); la búsqueda la hace `minar.cu` en la GPU.

## `minar.cu`: el Hit 7 con *midstate*

El texto de un bloque mide 77 caracteres (`contrato.texto_bloque`) y sólo cambia el nonce, que va al final. El Hit 7 hashea por nonce los 77 caracteres más hasta 20 dígitos: 2 bloques de MD5. Acá se usa el *midstate* (lo mismo que hacen los mineros de Bitcoin):

- El host pasa por MD5 los bloques completos de 64 bytes del texto, una vez por rango.
- Cada hilo arranca de ese estado y procesa sólo la cola (< 64 bytes) más su nonce: con 77 caracteres, **1 bloque de MD5 por nonce**. En la RTX 4060, con el texto de un bloque: **2,03 GH/s contra 1,50 del Hit 7**.
- Sirve para textos de cualquier largo (el Hit 7 acepta hasta 120 bytes).
- Igual que el Hit 7: un hilo por nonce, y si hay varios gana el **menor** (`atomicMin`). El host recalcula el hash del ganador, lo que además verifica a la GPU.

Queda abierto como servidor (arrancar CUDA cuesta): por stdin `<prefijo> <min> <max> <largo>\n<texto>` y por stdout una línea `OK …` / `SIN_SOLUCION …` / `ERROR …`, como el Hit 7. El minero le pasa el rango de a trozos de 2^30 nonces (~1 s) para poder cortar a mitad.

## Ejecutar

```bash
# En una máquina con GPU NVIDIA
nvcc -O3 -arch=sm_89 -o minar minero_gpu/minar.cu
MINAR=./minar GATEWAY_URL=http://<gateway>:8081 MINER_TOKEN=... python -m minero_gpu

# Con Docker (desde la raíz del repo; necesita nvidia-container-toolkit)
docker build -f pilar2/minero_gpu/Dockerfile -t minero-gpu .
docker run --gpus all -e GATEWAY_URL=... -e MINER_TOKEN=... minero-gpu
```

Mismas variables que el minero CPU, más `MINAR` (el binario; en la imagen, `/usr/local/bin/minar`). La imagen se publica en `ghcr.io/chimera-sdypp/minero-gpu` al mergear a `dev` o `main`.

## En el k3s de una PC con GPU (BC-36)

Los mineros GPU del sistema en GKE corren en el k3s de la PC de un integrante (RTX 4060), no en el K3s de la cátedra: ver [`k8s/minero-gpu/`](../k8s/minero-gpu/) (preparar el k3s, desplegar, verificar y entrada y salida de GPU para BC-41).

## Pruebas

- `tests/test_minero_gpu.py` corre en el CI con `tests/minar_falso.py` (el mismo protocolo con hashlib). Con `MINAR=<binario>` corre además contra la GPU: el menor nonce coincide con Python para textos de 0 a 6000 bytes (todos los bordes del relleno de MD5: 55, 56, 63, 64…).
- 09/11, RTX 4060 contra el gateway del k3s: bench de 1,8 GH/s (con la GPU compartida con otra aplicación), resolvió un rango con prefijo `000000` y el NCT aceptó el bloque.
