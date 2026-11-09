"""TrP (pool de transacciones, P5): parte cada tarea del NCT en rangos de nonce (BC-21, NCT.2).

Recibir los resultados de los mineros (BC-22) va en este mismo servicio.
"""

import logging
import re
import time
from dataclasses import dataclass

from prometheus_client import Counter, Gauge

from comun import Servicio, config, contrato

from .particion import largo_prefijo, partir, potencia_viva

log = logging.getLogger("trp")

TAREAS, RANGOS = "tareas", "rangos"
NODOS = "nodo:*"  # los escribe el gateway; los mineros son pocos, alcanza con SCAN
HASH = re.compile(r"^[0-9a-f]{32}$")

RANGOS_PUBLICADOS = Counter("blockchain_rangos_publicados", "Rangos de nonce publicados en la cola rangos")
PREFIJO = Gauge("blockchain_prefijo", "Largo del prefijo elegido para la última tarea")


@dataclass(frozen=True)
class Ajustes:
    espacio: int            # se buscan nonces en [0, espacio - 1]
    porcentaje: int         # tamaño de cada rango, en % del espacio (1 a 50)
    prefijo_base: int       # caracteres del prefijo del sistema: el máximo
    segundos_objetivo: int  # cuánto se quiere que tarde un bloque con los mineros vivos
    ajustar: bool           # False: siempre el prefijo base (para la prueba de prefijos, BC-39)
    vivo_segundos: int      # un minero cuenta si avisó hace menos que esto


def cargar_ajustes():
    ajustes = Ajustes(
        espacio=config.entero("ESPACIO_NONCES", 2**32),
        porcentaje=config.entero("FRAGMENTO_PORCENTAJE", 10),
        prefijo_base=config.entero("PREFIJO_BASE", 6),
        segundos_objetivo=config.entero("SEGUNDOS_POR_BLOQUE", 30),
        ajustar=config.entero("AJUSTAR_PREFIJO", 1) == 1,
        vivo_segundos=config.entero("VIVO_SEGUNDOS", 15),
    )
    if not 1 <= ajustes.porcentaje <= 50:
        raise ValueError("FRAGMENTO_PORCENTAJE va de 1 a 50")
    if not 1 <= ajustes.prefijo_base <= 32:
        raise ValueError("PREFIJO_BASE va de 1 a 32")
    if ajustes.espacio < 2 or ajustes.espacio > 2**64:
        raise ValueError("ESPACIO_NONCES va de 2 a 2^64")
    return ajustes


def errores_tarea(tarea):
    """Lo mínimo para poder armar el texto: si falta algo, la tarea no se puede minar."""
    if not isinstance(tarea, dict):
        return ["no es un objeto"]
    errores = []
    if not isinstance(tarea.get("tarea"), str) or not tarea["tarea"]:
        errores.append("tarea")
    if not isinstance(tarea.get("previous_hash"), str) or not HASH.match(tarea["previous_hash"]):
        errores.append("previous_hash")
    if not isinstance(tarea.get("timestamp"), int):
        errores.append("timestamp")
    if not isinstance(tarea.get("transacciones"), list) or not all(isinstance(tx, dict) for tx in tarea["transacciones"]):
        errores.append("transacciones")
    return errores


def elegir_prefijo(redis, ajustes, ahora):
    if not ajustes.ajustar:
        return "0" * ajustes.prefijo_base
    nodos = [redis.hgetall(clave) for clave in redis.scan_iter(match=NODOS, count=100)]
    potencia = potencia_viva(nodos, ahora, ajustes.vivo_segundos)
    return "0" * largo_prefijo(potencia, ajustes.segundos_objetivo, ajustes.prefijo_base)


def particionar(tarea, redis, publicar, ajustes, ahora=None):
    """NCT.2: elige el prefijo, registra la tarea y publica un mensaje por rango.

    El ack de la tarea lo hace comun/rabbit.py cuando esto termina, es decir, después de publicar
    todos los rangos. Si el pod muere a mitad, la tarea vuelve y se repite entera: tarea:<id> queda
    igual y los rangos repetidos no molestan, porque el TrP los anota en un conjunto (BC-22).
    """
    errores = errores_tarea(tarea)
    if errores:
        log.error("Tarea inválida, se descarta (%s): %r", ", ".join(errores), tarea)
        return
    prefijo = elegir_prefijo(redis, ajustes, time.time() if ahora is None else ahora)
    texto = contrato.texto_bloque(tarea["previous_hash"], tarea["timestamp"], tarea["transacciones"])
    rangos = partir(ajustes.espacio, ajustes.porcentaje)
    redis.hset(contrato.TAREA.format(tarea["tarea"]),
               mapping={"bloque": texto, "prefijo": prefijo, "rangos": len(rangos)})
    for numero, (desde, hasta) in enumerate(rangos):
        publicar(RANGOS, {"tarea": tarea["tarea"], "rango": numero, "bloque": texto,
                          "desde": desde, "hasta": hasta, "prefijo": prefijo})
    RANGOS_PUBLICADOS.inc(len(rangos))
    PREFIJO.set(len(prefijo))
    log.info("Tarea %s: %d transacciones, prefijo %s, %d rangos del %d %%",
             tarea["tarea"], len(tarea["transacciones"]), prefijo, len(rangos), ajustes.porcentaje)


def main():
    ajustes = cargar_ajustes()
    servicio = Servicio("trp", colas=[TAREAS, RANGOS])
    servicio.log.info("Ajustes: %s", ajustes)
    servicio.servir_http()
    # El consumidor publica los rangos desde su propio manejador (ver comun/rabbit.py).
    servicio.rabbit.consumir(TAREAS, lambda tarea: particionar(tarea, servicio.redis, servicio.rabbit.publicar, ajustes))
