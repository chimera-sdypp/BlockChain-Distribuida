# Imagen del escalador: la de los servicios (pilar2/Dockerfile) más el binario de OpenTofu.
#   docker build -f escalador/Dockerfile -t escalador pilar2
FROM python:3.14-slim AS dependencias
COPY comun/requirements.txt /tmp/comun.txt
RUN python -m venv /venv && /venv/bin/pip install --no-cache-dir -r /tmp/comun.txt

FROM python:3.14-slim AS tofu
ADD https://github.com/opentofu/opentofu/releases/download/v1.13.1/tofu_1.13.1_linux_amd64.tar.gz /tmp/tofu.tar.gz
RUN echo "378ada19d4bc70c43732004e8159be771b23b9a5afdf059e5f8a2b3fa2c70a69  /tmp/tofu.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/tofu.tar.gz -C /usr/local/bin tofu

FROM python:3.14-slim
ENV SERVICIO=escalador PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1 TOFU_DIR=/tofu
RUN useradd --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=dependencias /venv /venv
COPY --from=tofu /usr/local/bin/tofu /usr/local/bin/tofu
COPY comun/ comun/
COPY escalador/ escalador/
# /tofu: el módulo de las instancias (BC-35) y su .terraform; tofu init escribe ahí.
RUN mkdir logs /tofu && chown app logs /tofu
USER app
EXPOSE 8080
CMD ["python", "-m", "escalador"]
