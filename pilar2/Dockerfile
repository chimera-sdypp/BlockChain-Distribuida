# Imagen de los servicios Python del Pilar 2; SERVICIO es la carpeta del servicio.
#   docker build --build-arg SERVICIO=ejemplo -t ejemplo pilar2
FROM python:3.14-slim AS dependencias
ARG SERVICIO
COPY comun/requirements.txt /tmp/comun.txt
COPY ${SERVICIO}/requirements.txt /tmp/servicio.txt
RUN python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir -r /tmp/comun.txt -r /tmp/servicio.txt

FROM python:3.14-slim
ARG SERVICIO
ENV SERVICIO=${SERVICIO} PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=dependencias /venv /venv
COPY comun/ comun/
COPY ${SERVICIO}/ ${SERVICIO}/
RUN mkdir logs && chown app logs
USER app
EXPOSE 8080
CMD ["sh", "-c", "exec python -m \"$SERVICIO\""]
