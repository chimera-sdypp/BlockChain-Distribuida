# Imagen del minero GPU. Se construye desde la raíz del repo (usa el MD5 de pilar1/):
#   docker build -f pilar2/minero_gpu/Dockerfile -t minero-gpu .
# Corre con el runtime de NVIDIA (--gpus all, o nvidia.com/gpu: 1 en Kubernetes).

# 1) El binario CUDA. cudart va estático, así que la imagen final no necesita CUDA: la
#    biblioteca del driver la monta el runtime de NVIDIA.
FROM nvidia/cuda:12.6.3-devel-ubuntu24.04 AS cuda
WORKDIR /src
COPY pilar1/hit4/md5.cuh pilar1/hit4/
COPY pilar1/hit7/md5_cpu.h pilar1/hit7/
COPY pilar2/minero_gpu/minar.cu pilar2/minero_gpu/
# T4 (Colab), A100, serie 30, serie 40 y PTX para las que vengan (las compila el driver).
RUN nvcc -O3 -o /minar pilar2/minero_gpu/minar.cu \
      -gencode arch=compute_75,code=sm_75 -gencode arch=compute_80,code=sm_80 \
      -gencode arch=compute_86,code=sm_86 -gencode arch=compute_89,code=[sm_89,compute_89]

# 2) Las dependencias de Python, como en pilar2/Dockerfile.
FROM python:3.14-slim AS dependencias
COPY pilar2/comun/requirements.txt /tmp/comun.txt
RUN python -m venv /venv && /venv/bin/pip install --no-cache-dir -r /tmp/comun.txt

FROM python:3.14-slim
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1 MINAR=/usr/local/bin/minar \
    NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility
RUN useradd --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=dependencias /venv /venv
COPY --from=cuda /minar /usr/local/bin/minar
COPY pilar2/comun/ comun/
COPY pilar2/minero_cpu/ minero_cpu/
COPY pilar2/minero_gpu/ minero_gpu/
RUN mkdir logs && chown app logs
USER app
CMD ["python", "-m", "minero_gpu"]
