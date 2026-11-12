"""Logs a consola, a memoria y a un archivo rotativo en disco."""

import logging
import os
import sys
from collections import deque
from logging.handlers import RotatingFileHandler

from prometheus_client import Counter

# Fluent Bit separa este formato en fecha, nivel y mensaje.
FORMATO = "%(asctime)s | %(levelname)s | %(message)s"
FECHA = "%Y-%m-%d %H:%M:%S"

# Uno por proceso: configurar() puede llamarse varias veces (los tests lo hacen).
ERRORES = Counter("blockchain_errores", "Logs de nivel ERROR o superior", ["servicio", "tipo"])


class Memoria(logging.Handler):
    """Guarda las últimas `capacidad` líneas."""

    def __init__(self, capacidad=500):
        super().__init__()
        self.lineas = deque(maxlen=capacidad)

    def emit(self, registro):
        self.lineas.append(self.format(registro))


class ContadorDeErrores(logging.Handler):
    """Suma cada log ERROR o superior a blockchain_errores_total{servicio, tipo}.

    `tipo` es la clase de la excepción adjunta, o "log" si no trae ninguna.
    """

    def __init__(self, servicio):
        super().__init__(logging.ERROR)
        self.servicio = servicio

    def emit(self, registro):
        excepcion = registro.exc_info[0] if registro.exc_info else None
        ERRORES.labels(self.servicio, excepcion.__name__ if excepcion else "log").inc()


def configurar(servicio, nivel="INFO", directorio="logs"):
    os.makedirs(directorio, exist_ok=True)
    formato = logging.Formatter(FORMATO, FECHA)
    manejadores = [
        logging.StreamHandler(sys.stderr),
        Memoria(),
        RotatingFileHandler(os.path.join(directorio, f"{servicio}.log"),
                            maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
        ContadorDeErrores(servicio),
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
