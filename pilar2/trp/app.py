"""TrP (pool de transacciones, P5): un servicio con 2 funciones.

- Parte cada tarea del NCT en rangos de nonce (BC-21, NCT.2): `tareas` → `rangos`.
- Recibe lo que devuelven los mineros y le pasa al NCT el ganador (BC-22): `resultados` → `soluciones`.
"""

import logging
import re
import threading
import time
from dataclasses import dataclass

from prometheus_client import Counter, Gauge

from comun import Rabbit, Servicio, config, contrato

from .particion import CRITERIOS, largo_prefijo, partir, potencia_viva
from .resultados import SOLUCIONES, recibir

log = logging.getLogger("trp")

TAREAS, RANGOS, RESULTADOS = "tareas", "rangos", "resultados"
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
    criterio: str = "suma"  # cómo se junta la potencia de los vivos; otro que "suma", sólo para BC-59


def cargar_ajustes():
    ajustes = Ajustes(
        espacio=config.entero("ESPACIO_NONCES", 2**32),
        porcentaje=config.entero("FRAGMENTO_PORCENTAJE", 10),
        prefijo_base=config.entero("PREFIJO_BASE", 6),
        segundos_objetivo=config.entero("SEGUNDOS_POR_BLOQUE", 30),
        ajustar=config.entero("AJUSTAR_PREFIJO", 1) == 1,
        vivo_segundos=config.entero("VIVO_SEGUNDOS", 15),
        criterio=config.texto("CRITERIO_POTENCIA", "suma"),
    )
    if not 1 <= ajustes.porcentaje <= 50:
        raise ValueError("FRAGMENTO_PORCENTAJE va de 1 a 50")
    if not 1 <= ajustes.prefijo_base <= 32:
        raise ValueError("PREFIJO_BASE va de 1 a 32")
    if ajustes.espacio < 2 or ajustes.espacio > 2**64:
        raise ValueError("ESPACIO_NONCES va de 2 a 2^64")
    if ajustes.criterio not in CRITERIOS:
        raise ValueError(f"CRITERIO_POTENCIA va {', '.join(CRITERIOS)}, no {ajustes.criterio!r}")
    return ajustes


def errores_tarea(tarea):
    """Lo mínimo para que el gateway pueda armar el texto de cada minero: si falta algo, no se puede minar."""
    if not isinstance(tarea, dict):
        return ["no es un objeto"]
    errores = []
    if not isinstance(tarea.get("tarea"), str) or not tarea["tarea"]:
        errores.append("tarea")
    if not isinstance(tarea.get("previous_hash"), str) or not HASH.match(tarea["previous_hash"]):
        errores.append("previous_hash")
    if not isinstance(tarea.get("timestamp"), int):
        errores.append("timestamp")
    if not isinstance(tarea.get("altura"), int) or isinstance(tarea.get("altura"), bool) or tarea["altura"] < 1:
        errores.append("altura")
    if not isinstance(tarea.get("transacciones"), list) or not all(isinstance(tx, dict) for tx in tarea["transacciones"]):
        errores.append("transacciones")
    return errores


def elegir_prefijo(redis, ajustes, ahora):
    """(prefijo, potencia en hashes/s); sin ajuste, la potencia es None porque no se mira."""
    if not ajustes.ajustar:
        return "0" * ajustes.prefijo_base, None
    nodos = [redis.hgetall(clave) for clave in redis.scan_iter(match=contrato.NODOS, count=100)]
    potencia = potencia_viva(nodos, ahora, ajustes.vivo_segundos, ajustes.criterio)
    return "0" * largo_prefijo(potencia, ajustes.segundos_objetivo, ajustes.prefijo_base), potencia


def legible(hashes_por_segundo):
    """1817200000 → "1,82 GH/s", como se escriben los números en los README."""
    for divisor, unidad in ((1e9, "GH/s"), (1e6, "MH/s"), (1e3, "kH/s")):
        if hashes_por_segundo >= divisor:
            return f"{hashes_por_segundo / divisor:.2f} {unidad}".replace(".", ",")
    return f"{hashes_por_segundo:.0f} H/s"


def particionar(tarea, redis, publicar, ajustes, ahora=None):
    """NCT.2: elige el prefijo, registra la tarea y publica un mensaje por rango.

    Los rangos no llevan el texto: cada minero mina el suyo, con su coinbase adentro, y se lo arma
    el gateway al prestarle el rango (BC-62). Acá sólo se anota el prefijo en tarea:<id>, junto a lo
    que escribió el NCT.

    El ack de la tarea lo hace comun/rabbit.py cuando esto termina, es decir, después de publicar
    todos los rangos. Si el pod muere a mitad, la tarea vuelve y se repite entera: tarea:<id> queda
    igual y los rangos repetidos no molestan, porque el TrP los anota en un conjunto (BC-22).
    """
    errores = errores_tarea(tarea)
    if errores:
        log.error("Tarea inválida, se descarta (%s): %r", ", ".join(errores), tarea)
        return
    prefijo, potencia = elegir_prefijo(redis, ajustes, time.time() if ahora is None else ahora)
    rangos = partir(ajustes.espacio, ajustes.porcentaje)
    redis.hset(contrato.TAREA.format(tarea["tarea"]), mapping={"prefijo": prefijo, "rangos": len(rangos)})
    for numero, (desde, hasta) in enumerate(rangos):
        publicar(RANGOS, {"tarea": tarea["tarea"], "rango": numero, "desde": desde, "hasta": hasta,
                          "prefijo": prefijo})
    RANGOS_PUBLICADOS.inc(len(rangos))
    PREFIJO.set(len(prefijo))
    # Las pruebas de pilar3 leen esta línea (fragmentos.py, potencia.py): agregar sólo al final.
    detalle = "" if potencia is None else f", criterio {ajustes.criterio}, potencia {legible(potencia)}"
    log.info("Tarea %s: %d transacciones, prefijo %s, %d rangos del %d %%%s",
             tarea["tarea"], len(tarea["transacciones"]), prefijo, len(rangos), ajustes.porcentaje, detalle)


def main():
    ajustes = cargar_ajustes()
    servicio = Servicio("trp", colas=[TAREAS, RANGOS])
    servicio.log.info("Ajustes: %s", ajustes)
    servicio.servir_http()
    # Una conexión por cola consumida, cada una en su hilo; cada consumidor publica desde su propio
    # manejador (ver comun/rabbit.py).
    de_resultados = Rabbit(servicio.config, [RESULTADOS, SOLUCIONES])
    threading.Thread(target=de_resultados.consumir, name="resultados", daemon=True,
                     args=(RESULTADOS, lambda r: recibir(r, servicio.redis, de_resultados.publicar))).start()
    servicio.rabbit.consumir(TAREAS, lambda tarea: particionar(tarea, servicio.redis, servicio.rabbit.publicar, ajustes))
