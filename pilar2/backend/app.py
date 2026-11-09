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


LIMITE_BLOQUES, LIMITE_MAXIMO_BLOQUES = 20, 100
CAMPOS_ENTEROS_BLOQUE = ("nonce", "timestamp", "recompensa")


def _entero(texto):
    try:
        return int(texto)
    except (TypeError, ValueError):
        return None


def _bloque(guardado, altura):
    """bloque:<hash> como lo guarda el NCT, con los tipos de verdad y su altura en la cadena."""
    bloque = dict(guardado, altura=altura, transactions=json.loads(guardado.get("transactions", "[]")))
    for campo in CAMPOS_ENTEROS_BLOQUE:
        if campo in bloque:
            bloque[campo] = int(bloque[campo])
    return bloque


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

    # Las consultas son sync: FastAPI las corre en su threadpool, así Redis no frena el event loop.

    @app.get("/api/transacciones/{id_}")
    def transaccion(id_: str):
        tx = redis.hgetall(contrato.TX.format(id_))
        if not tx:
            return JSONResponse({"error": "no existe la transacción"}, status_code=404)
        return {"id": id_, **tx, "monto": int(tx["monto"]), "timestamp": int(tx["timestamp"])}

    @app.get("/api/saldos")
    def saldos():
        return {usuario: int(saldo) for usuario, saldo in redis.hgetall(contrato.SALDOS).items()}

    @app.get("/api/saldos/{usuario}")
    def saldo(usuario: str):
        return {"usuario": usuario, "saldo": int(redis.hget(contrato.SALDOS, usuario) or 0)}

    @app.get("/api/bloques")
    def bloques(limite: str = str(LIMITE_BLOQUES), desde: str | None = None):
        """Del más nuevo al más viejo, empezando por la altura `desde` (por defecto, la punta)."""
        errores = []
        limite = _entero(limite)
        if limite is None or not 1 <= limite <= LIMITE_MAXIMO_BLOQUES:
            errores.append(f"limite: entero de 1 a {LIMITE_MAXIMO_BLOQUES}")
        if desde is not None:
            desde = _entero(desde)
            if desde is None or desde < 0:
                errores.append("desde: entero mayor o igual que 0")
        if errores:
            return invalido(errores)
        largo = redis.llen(contrato.CADENA)
        if largo == 0:
            return []
        primera = largo - 1 if desde is None else min(desde, largo - 1)
        ultima = max(primera - limite + 1, 0)
        hashes = redis.lrange(contrato.CADENA, ultima, primera)
        tuberia = redis.pipeline(transaction=False)
        for hash_ in hashes:
            tuberia.hgetall(contrato.BLOQUE.format(hash_))
        guardados = tuberia.execute()
        return [_bloque(guardado, altura) for altura, guardado in
                reversed(list(enumerate(guardados, start=ultima)))]

    @app.get("/api/bloques/{hash_}")
    def bloque(hash_: str):
        guardado = redis.hgetall(contrato.BLOQUE.format(hash_))
        if not guardado:
            return JSONResponse({"error": "no existe el bloque"}, status_code=404)
        # O(n) sobre la cadena; alcanza mientras el contrato no guarde la altura en el bloque.
        return _bloque(guardado, redis.lpos(contrato.CADENA, hash_))

    @app.exception_handler(redislib.RedisError)
    def redis_caido(request, error):
        log.error("Redis no respondió en %s: %r", request.url.path, error)
        return no_disponible("Redis no disponible")


def main():
    cola = config.texto("COLA_TRANSACCIONES", contrato.COLAS[0])
    servicio = Servicio("backend", colas=[cola])
    # Nadie consume en esta conexión: se usa sólo para publicar desde los hilos HTTP.
    agregar_rutas(servicio.app, servicio.redis, servicio.rabbit, cola=cola)
    threading.Thread(target=servicio.rabbit.conectar, name="rabbit-conectar", daemon=True).start()
    servicio.servir_http(en_hilo=False)
