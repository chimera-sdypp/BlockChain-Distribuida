"""Lo que arma cada servicio al arrancar: config, logs, RabbitMQ, Redis y la app HTTP."""

import signal
import sys
import threading

from . import config, redisdb, registro, web
from .rabbit import Rabbit


class Servicio:
    def __init__(self, nombre, colas=(), usa_rabbit=True, usa_redis=True):
        self.config = config.cargar(nombre)
        self.log = registro.configurar(nombre, self.config.log_nivel, self.config.logs_dir)
        self.rabbit = Rabbit(self.config, colas) if usa_rabbit else None
        self.redis = redisdb.conectar(self.config) if usa_redis else None
        chequeos = {}
        if self.rabbit:
            chequeos["rabbitmq"] = self.rabbit.conectado
        if self.redis:
            chequeos["redis"] = lambda: redisdb.vivo(self.redis)
        self.app = web.crear_app(nombre, chequeos)
        if threading.current_thread() is threading.main_thread():
            # En el contenedor el proceso es el PID 1 e ignora SIGTERM si no lo atiende:
            # sin esto, Kubernetes espera 30 s y lo mata. Lo que no tuvo ack vuelve a la cola.
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    def servir_http(self, en_hilo=True):
        self.log.info("HTTP en el puerto %d (/health y /metrics)", self.config.puerto)
        return web.servir(self.app, self.config.puerto, en_hilo)
