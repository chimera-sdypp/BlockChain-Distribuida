"""Frontend: la página del sistema y los avisos en vivo por SSE (BC-28).

La página le pide los datos a la API del backend (`/api/*`, mismo dominio por el Ingress).
Este servicio sólo sirve el HTML y reenvía al navegador los avisos del exchange `eventos`:
cada pod tiene su propia cola atada, así que cada navegador recibe todos, esté en el pod que esté.
"""

import asyncio
import json
import logging
import threading
from pathlib import Path

from fastapi.responses import HTMLResponse, StreamingResponse
from prometheus_client import Counter, Gauge

from comun import Servicio, config, contrato

log = logging.getLogger("frontend")

PAGINA = Path(__file__).parent / "estaticos" / "index.html"
KEEPALIVE = 15  # segundos: así el Ingress no corta las conexiones quietas
AVISOS = (contrato.BLOQUE_CONFIRMADO, contrato.TRANSACCION_ESTADO)

CLIENTES = Gauge("blockchain_sse_clientes", "Navegadores conectados por SSE a este pod")
REENVIADOS = Counter("blockchain_eventos_reenviados", "Avisos de `eventos` reenviados por SSE", ["evento"])


class Difusor:
    """Reparte cada aviso a todos los navegadores conectados al pod.

    Los avisos llegan desde el hilo de RabbitMQ y cada conexión SSE vive en el event loop:
    por eso se entregan con call_soon_threadsafe a una cola asyncio por conexión.
    """

    def __init__(self):
        self._conexiones = {}  # asyncio.Queue → su loop
        self._lock = threading.Lock()
        CLIENTES.set_function(self.clientes)

    def conectar(self):
        cola = asyncio.Queue()
        with self._lock:
            self._conexiones[cola] = asyncio.get_running_loop()
        return cola

    def desconectar(self, cola):
        with self._lock:
            self._conexiones.pop(cola, None)

    def clientes(self):
        with self._lock:
            return len(self._conexiones)

    def difundir(self, evento, datos):
        with self._lock:
            conexiones = list(self._conexiones.items())
        for cola, loop in conexiones:
            try:
                loop.call_soon_threadsafe(cola.put_nowait, (evento, datos))
            except RuntimeError:  # el loop ya se cerró
                self.desconectar(cola)
        REENVIADOS.labels(evento).inc()


def formato_sse(evento, datos):
    return f"event: {evento}\ndata: {json.dumps(datos)}\n\n"


def agregar_rutas(app, difusor, keepalive=KEEPALIVE):
    @app.get("/", response_class=HTMLResponse)
    def pagina():
        return PAGINA.read_text(encoding="utf-8")

    @app.get("/eventos")
    async def eventos():
        async def flujo():
            cola = difusor.conectar()
            try:
                yield ": conectado\n\n"
                while True:
                    try:
                        evento, datos = await asyncio.wait_for(cola.get(), timeout=keepalive)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield formato_sse(evento, datos)
            finally:
                difusor.desconectar(cola)

        # X-Accel-Buffering: que ningún proxy junte los avisos antes de mandarlos.
        return StreamingResponse(flujo(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def escuchar(rabbit, difusor, avisos=AVISOS, exchange=contrato.EXCHANGE_EVENTOS):
    """Hilo que se suscribe a `eventos` y le pasa cada aviso al difusor."""
    hilo = threading.Thread(target=rabbit.suscribir, name="eventos",
                            args=(exchange, avisos, difusor.difundir), daemon=True)
    hilo.start()
    return hilo


def main():
    servicio = Servicio("frontend", usa_redis=False)
    difusor = Difusor()
    agregar_rutas(servicio.app, difusor, keepalive=config.entero("SSE_KEEPALIVE", KEEPALIVE))
    escuchar(servicio.rabbit, difusor)
    servicio.servir_http(en_hilo=False)
