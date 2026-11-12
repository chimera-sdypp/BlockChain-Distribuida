"""Minero GPU (BC-27): el mismo cliente del gateway que el minero CPU, buscando con CUDA.

Corre fuera del cluster (en el k3s de una PC con NVIDIA, BC-36): sólo necesita la URL del gateway,
el MINER_TOKEN y una GPU NVIDIA.
"""

import logging
import signal
import socket
import sys

from comun import config, contrato, registro
from minero_cpu.minero import Gateway, Minero, TokenInvalido

from .busqueda import BuscadorGPU

log = logging.getLogger("minero_gpu")


def main():
    registro.configurar("minero_gpu", config.texto("LOG_NIVEL", "INFO").upper(), config.texto("LOGS_DIR", "logs"))
    minero_id = config.texto("MINERO_ID", socket.gethostname())
    if not contrato.usuario_valido(minero_id):
        sys.exit(f"MINERO_ID {minero_id!r} no sirve: de 1 a 32 letras, números, «_» o «-»")
    token = config.texto("MINER_TOKEN")
    if not token:
        sys.exit("Falta MINER_TOKEN")
    gateway = Gateway(config.texto("GATEWAY_URL", "http://localhost:8080"), token)
    buscador = BuscadorGPU(config.texto("MINAR", "/usr/local/bin/minar"))
    minero = Minero(gateway, minero_id, espera=config.entero("ESPERA_SIN_TRABAJO", 2),
                    latido=config.entero("KEEPALIVE_SEGUNDOS", 5), buscador=buscador, tipo="gpu",
                    placa=buscador.placa)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("Minero %s en %s, gateway %s", minero_id, buscador.placa, gateway.url)
    try:
        minero.presentarse()
        minero.trabajar()
    except TokenInvalido as error:
        sys.exit(str(error))
    finally:
        minero.detener()
