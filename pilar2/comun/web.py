"""Servidor HTTP de cada servicio: /health y /metrics en el mismo puerto."""

import logging
import threading

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

log = logging.getLogger(__name__)


def crear_app(servicio, chequeos):
    """`chequeos` es {dependencia: función que devuelve True si está viva}.

    /health devuelve {servicio: estado, dependencia: estado} y 503 si alguna está caída.
    Los servicios agregan sus propias rutas a la app que devuelve.
    """
    app = FastAPI(title=servicio, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        estados = {nombre: "ok" if _vivo(chequeo) else "caido" for nombre, chequeo in chequeos.items()}
        sano = all(estado == "ok" for estado in estados.values())
        return JSONResponse({servicio: "ok" if sano else "degradado", **estados},
                            status_code=200 if sano else 503)

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def _vivo(chequeo):
    try:
        return bool(chequeo())
    except Exception:
        log.exception("Falló un chequeo de /health")
        return False


def servir(app, puerto, en_hilo=False):
    """Levanta la app en 0.0.0.0:puerto. Con en_hilo=True no bloquea (para los consumidores)."""
    servidor = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=puerto,
                                             log_config=None, access_log=False))
    if not en_hilo:
        servidor.run()
        return servidor
    threading.Thread(target=servidor.run, name="http", daemon=True).start()
    return servidor
