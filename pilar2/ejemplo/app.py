"""Servicio de ejemplo: muestra cómo se usa comun/.

POST /mensajes {"texto": ...} lo publica en la cola `ejemplo`. El consumidor lo toma,
suma uno en Redis (`ejemplo:procesados`) y en la métrica `ejemplo_mensajes_total`.
"""

import logging

from fastapi import HTTPException
from prometheus_client import Counter
from pydantic import BaseModel, Field

from comun import Rabbit, Servicio

COLA = "ejemplo"
PROCESADOS = Counter("ejemplo_mensajes", "Mensajes procesados por el ejemplo")

log = logging.getLogger("ejemplo")


class Mensaje(BaseModel):
    texto: str = Field(min_length=1)


def procesar(redis, mensaje):
    total = redis.incr("ejemplo:procesados")
    PROCESADOS.inc()
    log.info("Procesado %r (van %d)", mensaje["texto"], total)
    return total


def agregar_rutas(app, publicador):
    @app.post("/mensajes", status_code=202)
    def recibir(mensaje: Mensaje):
        try:
            publicador.publicar(COLA, mensaje.model_dump())
        except Exception as error:
            log.error("No se pudo publicar en %s: %r", COLA, error)
            raise HTTPException(503, "RabbitMQ no disponible")
        return {"encolado": True}


def main():
    servicio = Servicio("ejemplo", colas=[COLA])
    # Otra conexión para publicar desde los hilos HTTP: la de servicio.rabbit es del consumidor.
    publicador = Rabbit(servicio.config, [COLA])
    agregar_rutas(servicio.app, publicador)
    servicio.servir_http()
    publicador.conectar()
    servicio.rabbit.consumir(COLA, lambda mensaje: procesar(servicio.redis, mensaje))
