"""Logs a consola, a memoria y a un archivo rotativo en disco."""

import logging
import os
import sys
from collections import deque
from logging.handlers import RotatingFileHandler

# Fluent Bit separa este formato en fecha, nivel y mensaje.
FORMATO = "%(asctime)s | %(levelname)s | %(message)s"
FECHA = "%Y-%m-%d %H:%M:%S"


class Memoria(logging.Handler):
    """Guarda las últimas `capacidad` líneas."""

    def __init__(self, capacidad=500):
        super().__init__()
        self.lineas = deque(maxlen=capacidad)

    def emit(self, registro):
        self.lineas.append(self.format(registro))


def configurar(servicio, nivel="INFO", directorio="logs"):
    os.makedirs(directorio, exist_ok=True)
    formato = logging.Formatter(FORMATO, FECHA)
    manejadores = [
        logging.StreamHandler(sys.stderr),
        Memoria(),
        RotatingFileHandler(os.path.join(directorio, f"{servicio}.log"),
                            maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ]
    raiz = logging.getLogger()
    for viejo in raiz.handlers[:]:
        raiz.removeHandler(viejo)
        viejo.close()
    for manejador in manejadores:
        manejador.setFormatter(formato)
        raiz.addHandler(manejador)
    raiz.setLevel(nivel)
    logging.getLogger("pika").setLevel(logging.WARNING)
    return logging.getLogger(servicio)


def memoria():
    """El buffer en memoria de los logs (None si no se llamó a configurar)."""
    return next((m for m in logging.getLogger().handlers if isinstance(m, Memoria)), None)
