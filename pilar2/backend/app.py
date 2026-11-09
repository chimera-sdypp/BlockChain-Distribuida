"""Backend: la API por la que entran las transferencias y se consulta su estado (BC-20)."""

import json
import logging
import threading
import time
import uuid

import redis as redislib
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from prometheus_client import Counter

from comun import Servicio, config, contrato

log = logging.getLogger("backend")

# Por transacción, también en los lotes: aceptada, invalida o error (no se pudo encolar).
TX_RECIBIDAS = Counter("blockchain_tx_recibidas", "Transacciones recibidas por el backend", ["resultado"])


class NoDisponible(Exception):
    """RabbitMQ o Redis no respondieron: el cliente tiene que reintentar."""


async def leer_json(request):
    """El cuerpo como JSON, o None si no se puede parsear."""
    try:
        return json.loads(await request.body())
    except ValueError:
        return None


def invalido(errores):
    return JSONResponse({"errores": errores}, status_code=400)


def no_disponible(error):
    return JSONResponse({"error": str(error)}, status_code=503)


def nueva_transaccion(datos):
    return {"id": uuid.uuid4().hex, "origen": datos["origen"], "destino": datos["destino"],
            "monto": datos["monto"], "timestamp": int(time.time())}


def agregar_rutas(app, redis, publicador, cola=contrato.COLAS[0]):
    def registrar_y_encolar(tx):
        """Primero Redis y después la cola: así el NCT nunca ve una tx que todavía no está en Redis.

        Si la cola falla, se borra el registro para que no quede «recibida» para siempre.
        """
        clave = contrato.TX.format(tx["id"])
        try:
            redis.hset(clave, mapping={"origen": tx["origen"], "destino": tx["destino"],
                                       "monto": tx["monto"], "timestamp": tx["timestamp"],
                                       "estado": "recibida"})
        except redislib.RedisError as error:
            log.error("Redis no respondió al guardar %s: %r", clave, error)
            raise NoDisponible("Redis no disponible") from error
        try:
            publicador.publicar(cola, tx)
        except Exception as error:
            log.error("No se pudo publicar %s en %s: %r", tx["id"], cola, error)
            _borrar(clave)
            raise NoDisponible("RabbitMQ no disponible") from error

    def _borrar(*claves):
        try:
            redis.delete(*claves)
        except redislib.RedisError:
            log.exception("No se pudieron borrar %s", claves)

    @app.post("/api/transacciones", status_code=202)
    async def recibir(request: Request):
        datos = await leer_json(request)
        if datos is None:
            TX_RECIBIDAS.labels("invalida").inc()
            return invalido(["el cuerpo no es JSON válido"])
        errores = contrato.validar_transaccion(datos)
        if errores:
            TX_RECIBIDAS.labels("invalida").inc()
            return invalido(errores)
        tx = nueva_transaccion(datos)
        try:
            # Redis y pika bloquean: fuera del event loop, para no frenar los otros pedidos.
            await run_in_threadpool(registrar_y_encolar, tx)
        except NoDisponible as error:
            TX_RECIBIDAS.labels("error").inc()
            return no_disponible(error)
        TX_RECIBIDAS.labels("aceptada").inc()
        return {"id": tx["id"]}


def main():
    cola = config.texto("COLA_TRANSACCIONES", contrato.COLAS[0])
    servicio = Servicio("backend", colas=[cola])
    # Nadie consume en esta conexión: se usa sólo para publicar desde los hilos HTTP.
    agregar_rutas(servicio.app, servicio.redis, servicio.rabbit, cola=cola)
    threading.Thread(target=servicio.rabbit.conectar, name="rabbit-conectar", daemon=True).start()
    servicio.servir_http(en_hilo=False)
