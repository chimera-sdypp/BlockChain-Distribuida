"""Gateway de los mineros (BC-43): la única puerta de los mineros al sistema.

Los mineros están fuera del cluster y RabbitMQ y Redis no se publican: el gateway habla HTTP con
ellos y por adentro con RabbitMQ y Redis. Sin estado en memoria: los préstamos de los rangos están
en Redis, así que cualquier pod atiende a cualquier minero.
"""

import hmac
import json
import logging
import threading
import time

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
import redis as redislib
from prometheus_client import REGISTRY, Counter
from prometheus_client.core import GaugeMetricFamily

from comun import Rabbit, Servicio, config, contrato

log = logging.getLogger("gateway")

ENTREGADAS = Counter("blockchain_tareas_entregadas", "Rangos entregados a los mineros")

TIPOS = ("gpu", "cpu")
MINERO_VIVO = 15  # segundos: el keep-alive es cada 5 s; tres perdidos y se lo da por caído


PRESTAMO = "prestamo:{}"  # prestamo:<tarea>:<rango> → {mensaje, minero}


def es_entero(valor, minimo=0):
    return isinstance(valor, int) and not isinstance(valor, bool) and valor >= minimo


def resultado_valido(datos):
    if not isinstance(datos, dict) or not isinstance(datos.get("tarea"), str) or not datos["tarea"] \
            or not es_entero(datos.get("rango")) or not contrato.usuario_valido(datos.get("minero")):
        return False
    if datos.get("sin_solucion") is True:
        return True
    return es_entero(datos.get("nonce")) and isinstance(datos.get("hash"), str)


def es_numero(valor, minimo=0):
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and valor >= minimo


class Gateway:
    def __init__(self, redis, rabbit, token, cola_rangos="rangos", cola_resultados="resultados",
                 lease=30, minero_vivo=MINERO_VIVO):
        self.redis, self.rabbit, self.token = redis, rabbit, token
        self.cola_rangos, self.cola_resultados = cola_rangos, cola_resultados
        self.lease, self.minero_vivo = lease, minero_vivo
        COLECTOR.gateway = self

    def autorizado(self, encabezado):
        esperado = f"Bearer {self.token}"
        return bool(self.token) and hmac.compare_digest((encabezado or "").encode(), esperado.encode())

    # ── Registro ──

    def hello(self, datos):
        """Devuelve (código, cuerpo). Un minero sin bench recibe la tarea modelo."""
        if not isinstance(datos, dict) or not contrato.usuario_valido(datos.get("minero")) \
                or datos.get("tipo") not in TIPOS or not isinstance(datos.get("placa"), str):
            return 400, {"error": "hace falta {minero (formato de usuario), tipo: gpu | cpu, placa}"}
        nodo = contrato.NODO.format(datos["minero"])
        self.redis.hset(nodo, mapping={"tipo": datos["tipo"], "placa": datos["placa"][:100],
                                       "ultimo_aviso": int(time.time())})
        if self.redis.hexists(nodo, "bench"):
            return 200, {"registrado": True}
        log.info("Minero nuevo %s (%s, %s): le toca la tarea modelo", datos["minero"], datos["tipo"], datos["placa"])
        return 200, contrato.BENCH

    def bench(self, datos):
        if not isinstance(datos, dict) or not contrato.usuario_valido(datos.get("minero")) \
                or not es_numero(datos.get("hashes_por_segundo"), minimo=1):
            return 400, {"error": "hace falta {minero, segundos, hashes_por_segundo > 0}"}
        self.redis.hset(contrato.NODO.format(datos["minero"]), "bench", int(datos["hashes_por_segundo"]))
        log.info("Bench de %s: %d hashes/s", datos["minero"], datos["hashes_por_segundo"])
        return 204, None

    # ── Rangos: entregar y devolver ──

    def siguiente(self, minero):
        """GET /tasks/next: el próximo rango de una tarea en curso, prestado a `minero`.

        Primero se anota el préstamo en Redis y recién después se hace el ack: un rango nunca
        queda sin dueño ni en la cola. Los de tareas cerradas se descartan (ack sin entregar).
        """
        if not contrato.usuario_valido(minero):
            return 400, {"error": "falta ?minero=<id> (formato de usuario)"}
        try:
            while True:
                rango = self.rabbit.tomar(self.cola_rangos, lambda mensaje: self._prestar(mensaje, minero))
                if rango is None:
                    return 204, None
                if rango is not False:
                    ENTREGADAS.inc()
                    return 200, rango
        except redislib.RedisError as error:
            log.error("Redis no respondió al prestar un rango (vuelve a la cola): %r", error)
            return 503, {"error": "Redis no disponible"}

    def _prestar(self, mensaje, minero):
        """Dentro de `tomar`: si devuelve, se hace el ack; si lanza (Redis caído), el rango vuelve."""
        if not self.tarea_en_curso(mensaje["tarea"]):
            log.info("Descarto el rango %s de la tarea %s, que ya se cerró", mensaje["rango"], mensaje["tarea"])
            return False
        miembro = f"{mensaje['tarea']}:{mensaje['rango']}"
        tuberia = self.redis.pipeline()
        tuberia.hset(PRESTAMO.format(miembro), mapping={"mensaje": json.dumps(mensaje), "minero": minero})
        tuberia.zadd(contrato.PRESTAMOS, {miembro: time.time() + self.lease})
        tuberia.execute()
        log.info("Rango %s prestado a %s", miembro, minero)
        return mensaje

    def tarea_en_curso(self, tarea):
        return self.redis.hget(contrato.TAREA.format(tarea), "estado") == "en_curso"

    def resultado(self, datos):
        """POST /results: lo publica en `resultados` y recién después libera el préstamo."""
        if not resultado_valido(datos):
            return 400, {"error": "hace falta {tarea, rango, minero} y {nonce, hash} o {sin_solucion: true}"}
        try:
            self.rabbit.publicar(self.cola_resultados, datos)
        except Exception as error:
            log.error("No se pudo publicar el resultado de %s:%s: %r", datos["tarea"], datos["rango"], error)
            return 503, {"error": "RabbitMQ no disponible"}
        self._liberar(f"{datos['tarea']}:{datos['rango']}")
        return 202, None

    def _liberar(self, miembro):
        try:
            tuberia = self.redis.pipeline()
            tuberia.zrem(contrato.PRESTAMOS, miembro)
            tuberia.delete(PRESTAMO.format(miembro))
            tuberia.execute()
        except redislib.RedisError:
            log.exception("No se pudo liberar el préstamo %s (vencerá y el TrP tolera el repetido)", miembro)

    # ── Métricas ──

    def mineros_vivos(self):
        """{tipo: (cantidad, hashes/s)} de los nodo:* con un aviso de hace menos de minero_vivo."""
        claves = list(self.redis.scan_iter(match=contrato.NODOS, count=100))
        tuberia = self.redis.pipeline(transaction=False)
        for clave in claves:
            tuberia.hmget(clave, "tipo", "ultimo_aviso", "hashes_por_segundo", "bench")
        ahora, totales = time.time(), {tipo: [0, 0.0] for tipo in TIPOS}
        for tipo, aviso, hps, bench in tuberia.execute():
            if tipo in totales and aviso and ahora - float(aviso) < self.minero_vivo:
                totales[tipo][0] += 1
                totales[tipo][1] += float(hps or bench or 0)
        return totales


class ColectorMineros:
    """Mineros vivos y hashes/s por tipo, leídos de Redis al momento del scrape."""

    def __init__(self):
        self.gateway = None

    def collect(self):
        mineros = GaugeMetricFamily("blockchain_mineros", "Mineros vivos por tipo", labels=["tipo"])
        hashes = GaugeMetricFamily("blockchain_hashes_por_segundo", "Potencia viva por tipo", labels=["tipo"])
        if self.gateway is not None:
            try:
                for tipo, (cantidad, hps) in self.gateway.mineros_vivos().items():
                    mineros.add_metric([tipo], cantidad)
                    hashes.add_metric([tipo], hps)
            except Exception:
                log.exception("No se pudieron leer los mineros para /metrics")
        yield mineros
        yield hashes


COLECTOR = ColectorMineros()
REGISTRY.register(COLECTOR)


async def leer_json(request):
    try:
        return json.loads(await request.body())
    except ValueError:
        return None


def responder(codigo, cuerpo):
    if cuerpo is None:
        return Response(status_code=codigo)
    return JSONResponse(cuerpo, status_code=codigo)


def agregar_rutas(app, gateway):
    def con_token(request: Request):
        if not gateway.autorizado(request.headers.get("authorization")):
            raise HTTPException(401, "MINER_TOKEN inválido")

    def ruta(metodo):
        """Lee el JSON y corre el método del gateway (Redis y pika bloquean) fuera del event loop."""
        async def atender(request: Request):
            datos = await leer_json(request)
            return responder(*await run_in_threadpool(metodo, datos))
        return atender

    app.post("/hello", dependencies=[Depends(con_token)])(ruta(gateway.hello))
    app.post("/bench", dependencies=[Depends(con_token)])(ruta(gateway.bench))
    app.post("/results", dependencies=[Depends(con_token)])(ruta(gateway.resultado))

    @app.get("/tasks/next", dependencies=[Depends(con_token)])
    def tasks_next(minero: str = ""):
        return responder(*gateway.siguiente(minero))


def main():
    servicio = Servicio("gateway")
    c = servicio.config
    token = config.texto("MINER_TOKEN")
    if not token:
        raise SystemExit("Falta MINER_TOKEN")
    rabbit = Rabbit(c, [config.texto("COLA_RANGOS", "rangos"), config.texto("COLA_RESULTADOS", "resultados")])
    gateway = Gateway(servicio.redis, rabbit, token,
                      cola_rangos=config.texto("COLA_RANGOS", "rangos"),
                      cola_resultados=config.texto("COLA_RESULTADOS", "resultados"),
                      lease=config.entero("TASK_LEASE_SECONDS", 30),
                      minero_vivo=config.entero("MINERO_VIVO_SEGUNDOS", MINERO_VIVO))
    agregar_rutas(servicio.app, gateway)
    for conexion in (rabbit, servicio.rabbit):  # la de los pedidos y la de /health
        threading.Thread(target=conexion.conectar, name="rabbit-conectar", daemon=True).start()
    servicio.servir_http(en_hilo=False)
