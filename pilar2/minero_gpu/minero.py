"""Minero GPU (BC-27): el mismo cliente del gateway que el minero CPU, buscando con CUDA.

Corre fuera del cluster (en el k3s de una PC con NVIDIA, BC-36, o en Colab): sólo necesita la URL
del gateway, su clave (la cuenta la tiene que aprobar el organizador) y una GPU NVIDIA.
"""

import logging
import signal
import sys

from comun import config, registro
from minero_cpu.minero import ClaveRechazada, Gateway, Minero, alias_del_entorno, clave_del_entorno

from .busqueda import BuscadorGPU

log = logging.getLogger("minero_gpu")


def main():
    registro.configurar("minero_gpu", config.texto("LOG_NIVEL", "INFO").upper(), config.texto("LOGS_DIR", "logs"))
    alias = alias_del_entorno()
    gateway = Gateway(config.texto("GATEWAY_URL", "http://localhost:8080"), clave_del_entorno(alias))
    buscador = BuscadorGPU(config.texto("MINAR", "/usr/local/bin/minar"))
    minero = Minero(gateway, alias, espera=config.entero("ESPERA_SIN_TRABAJO", 2),
                    latido=config.entero("KEEPALIVE_SEGUNDOS", 5), buscador=buscador, tipo="gpu",
                    placa=buscador.placa)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("Minero %s (cuenta %s) en %s, gateway %s", alias, gateway.cuenta, buscador.placa, gateway.url)
    try:
        minero.presentarse()
        minero.trabajar()
    except ClaveRechazada as error:
        sys.exit(str(error))
    finally:
        minero.detener()
