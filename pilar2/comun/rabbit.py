"""Conexión a RabbitMQ: reconexión, colas durables, publicar y consumir con ack manual.

Una instancia por uso. La que consume (`consumir` bloquea su hilo) publica sólo desde el
manejador. Para publicar desde otros hilos (por ejemplo un endpoint HTTP) se usa otra
instancia: es segura entre hilos y, después de `conectar`, se mantiene viva sola.
"""

import json
import logging
import threading
import time
from functools import partial

import pika
import redis
from pika.exceptions import AMQPChannelError, AMQPConnectionError

log = logging.getLogger(__name__)

ERRORES = (AMQPConnectionError, AMQPChannelError)
# Fallas de la infraestructura, no del mensaje: por ejemplo, Redis mientras Sentinel elige otro primario.
# ReadOnlyError: se escribió en el primario viejo, que ya pasó a ser réplica.
TRANSITORIOS = ERRORES + (redis.ConnectionError, redis.TimeoutError, redis.ReadOnlyError)
ESPERA_TRANSITORIO = 1  # segundos antes de devolver el mensaje, para no girar en vacío
LATIDO = 30  # heartbeat de AMQP, en segundos


class Rabbit:
    def __init__(self, config, colas=()):
        self._parametros = pika.ConnectionParameters(
            host=config.rabbit_host, port=config.rabbit_puerto, virtual_host=config.rabbit_vhost,
            credentials=pika.PlainCredentials(config.rabbit_usuario, config.rabbit_clave),
            heartbeat=LATIDO, blocked_connection_timeout=60)
        self._colas = tuple(colas)
        self._prefetch = config.rabbit_prefetch
        self._conexion = self._canal = None
        self._lock = threading.Lock()
        self._consumiendo = False
        self._detenido = False
        self._latiendo = False
        self._suscripto = False
        self._exchanges = set()  # los ya declarados en la conexión actual

    def conectado(self):
        return self._conexion is not None and self._conexion.is_open

    def conectar(self):
        """Conecta (reintenta con espera creciente, hasta 30 s) y declara las colas."""
        espera = 1
        while not self._detenido:
            with self._lock:
                if self.conectado():
                    break
                try:
                    self._abrir()
                    break
                except ERRORES as error:
                    self._cerrar()
                    motivo = repr(error)
            log.warning("RabbitMQ no responde (%s); reintento en %d s", motivo, espera)
            time.sleep(espera)
            espera = min(espera * 2, 30)
        if not self._latiendo:
            self._latiendo = True
            threading.Thread(target=self._latir, name="rabbit-latido", daemon=True).start()

    def publicar(self, cola, mensaje):
        """Publica el dict como JSON persistente y espera la confirmación del broker.

        Si la conexión se había caído la reabre y reintenta una vez (salvo en la
        instancia que consume, que se reconecta en su propio bucle).
        """
        self._publicar("", cola, mensaje, mandatory=True)

    def avisar(self, exchange, clave, mensaje):
        """Publica un aviso en un exchange topic (por ejemplo `eventos`, `bloque.confirmado`).

        Lo reciben todos los suscriptores atados a esa clave; si no hay ninguno, el aviso se
        pierde sin error (a diferencia de `publicar`, que exige que la cola exista).
        """
        self._publicar(exchange, clave, mensaje, mandatory=False)

    def suscribir(self, exchange, claves, manejador):
        """Bloquea el hilo: recibe los avisos de `exchange` con esas claves y llama a manejador(dict).

        Usa una cola exclusiva propia, que RabbitMQ borra al cerrarse la conexión: cada
        suscriptor (por ejemplo, cada pod) recibe todos los avisos. Si se corta la conexión,
        reconecta y vuelve a declarar el exchange, la cola y las ataduras. Los avisos que
        lleguen mientras está desconectado se pierden. Se corta con `detener`.
        """
        self._consumiendo = True
        try:
            while not self._detenido:
                self.conectar()
                try:
                    self._declarar_exchange(exchange)
                    cola = self._canal.queue_declare("", exclusive=True, auto_delete=True).method.queue
                    for clave in claves:
                        self._canal.queue_bind(cola, exchange, routing_key=clave)
                    self._canal.basic_consume(cola, partial(self._entregar, exchange, manejador))
                    self._suscripto = True
                    self._canal.start_consuming()
                except ERRORES as error:
                    log.warning("Se cortó la suscripción a %s (%r); reconecto", exchange, error)
                    self._cerrar()
                finally:
                    self._suscripto = False
        finally:
            self._consumiendo = False
            self._cerrar()

    def suscripto(self):
        """True cuando la cola de `suscribir` ya está atada y recibiendo."""
        return self._suscripto

    def _publicar(self, exchange, clave, mensaje, mandatory):
        cuerpo = json.dumps(mensaje).encode()
        propiedades = pika.BasicProperties(content_type="application/json", delivery_mode=2)
        with self._lock:
            intentos = 1 if self._consumiendo else 2
            for intento in range(1, intentos + 1):
                try:
                    if not self.conectado():
                        if self._consumiendo:
                            raise AMQPConnectionError("se cayó la conexión del consumidor")
                        self._abrir()
                    if exchange:
                        self._declarar_exchange(exchange)
                    self._canal.basic_publish(exchange, clave, cuerpo, propiedades, mandatory=mandatory)
                    return
                except ERRORES:
                    if not self._consumiendo:
                        self._cerrar()
                    if intento == intentos:
                        raise

    def _declarar_exchange(self, exchange):
        """Idempotente: si ya existe con la misma forma (como `eventos` en colas.json), no cambia nada."""
        if exchange not in self._exchanges:
            self._canal.exchange_declare(exchange, exchange_type="topic", durable=True)
            self._exchanges.add(exchange)

    def consumir(self, cola, manejador):
        """Bloquea el hilo: por cada mensaje llama a manejador(dict) y hace ack si termina bien.

        Si el manejador falla por el mensaje, vuelve a la cola una vez; si vuelve a fallar, se
        descarta (queda el error en el log). Si falla por RabbitMQ o Redis caídos, vuelve a la
        cola siempre. Si se corta la conexión, reconecta y sigue.
        """
        self._consumiendo = True
        try:
            while not self._detenido:
                self.conectar()
                try:
                    self._canal.basic_consume(cola, partial(self._entregar, cola, manejador))
                    self._canal.start_consuming()
                except ERRORES as error:
                    log.warning("Se cortó el consumo de %s (%r); reconecto", cola, error)
                    self._cerrar()
        finally:
            self._consumiendo = False
            self._cerrar()

    def detener(self):
        """Corta `consumir` o `suscribir` (se puede llamar desde otro hilo) y el latido."""
        self._detenido = True
        conexion, canal = self._conexion, self._canal
        if self._consumiendo and conexion is not None and conexion.is_open:
            conexion.add_callback_threadsafe(canal.stop_consuming)

    def _entregar(self, cola, manejador, canal, metodo, propiedades, cuerpo):
        try:
            manejador(json.loads(cuerpo))
        except TRANSITORIOS as error:
            log.warning("Falló un mensaje de %s por la infraestructura (%r); vuelve a la cola", cola, error)
            time.sleep(ESPERA_TRANSITORIO)
            canal.basic_nack(metodo.delivery_tag, requeue=True)
        except Exception:
            reencolar = not metodo.redelivered
            log.exception("Falló un mensaje de %s; %s", cola,
                          "vuelve a la cola" if reencolar else "se descarta")
            canal.basic_nack(metodo.delivery_tag, requeue=reencolar)
        else:
            canal.basic_ack(metodo.delivery_tag)

    def _abrir(self):
        self._conexion = pika.BlockingConnection(self._parametros)
        self._canal = self._conexion.channel()
        self._canal.confirm_delivery()
        self._canal.basic_qos(prefetch_count=self._prefetch)
        self._exchanges = set()
        for cola in self._colas:
            # quorum: la cola se replica en los nodos del cluster de RabbitMQ.
            self._canal.queue_declare(cola, durable=True, arguments={"x-queue-type": "quorum"})
        log.info("Conectado a RabbitMQ en %s:%s", self._parametros.host, self._parametros.port)

    def _cerrar(self):
        try:
            if self.conectado():
                self._conexion.close()
        except Exception:
            pass
        self._conexion = self._canal = None

    def _latir(self):
        """Sin un consumidor, nadie atiende los heartbeats: este hilo lo hace y reconecta."""
        while not self._detenido:
            time.sleep(LATIDO / 3)
            with self._lock:
                if self._consumiendo or self._detenido:
                    continue
                try:
                    if self.conectado():
                        self._conexion.process_data_events(0)
                    else:
                        self._abrir()
                except ERRORES as error:
                    log.warning("RabbitMQ no responde (%r); reintento", error)
                    self._cerrar()
