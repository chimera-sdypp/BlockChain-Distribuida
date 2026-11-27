"""TrP (pool de transacciones, P5): un servicio con 2 funciones.

- Parte cada tarea del NCT en rangos de nonce (BC-21, NCT.2): `tareas` → `rangos`.
- Recibe lo que devuelven los mineros y le pasa al NCT el ganador (BC-22): `resultados` → `soluciones`.
"""

import json
import logging
import re
import threading
import time
from dataclasses import dataclass

from prometheus_client import Counter, Gauge

from comun import Rabbit, Servicio, config, contrato

from .particion import CRITERIOS, decidir_prefijo, partir, potencia_viva
from .resultados import SOLUCIONES, recibir

log = logging.getLogger("trp")

TAREAS, RANGOS, RESULTADOS = "tareas", "rangos", "resultados"
HASH = re.compile(r"^[0-9a-f]{32}$")

RANGOS_PUBLICADOS = Counter("blockchain_rangos_publicados", "Rangos de nonce publicados en la cola rangos")
PREFIJO = Gauge("blockchain_prefijo", "Largo del prefijo elegido para la última tarea")
PREFIJO_MOTIVO = Counter("blockchain_prefijo_elecciones", "Decisiones de prefijo del TrP por motivo", ["motivo"])
TIEMPO_ESPERADO = Gauge("blockchain_tiempo_bloque_esperado_segundos",
                        "Tiempo esperado por bloque configurado en el TrP")


@dataclass(frozen=True)
class Ajustes:
    espacio: int            # se buscan nonces en [0, espacio - 1]
    porcentaje: int         # tamaño de cada rango, en % del espacio (1 a 50)
    prefijo_base: int       # caracteres del prefijo del sistema: el máximo
    segundos_objetivo: int  # cuánto se quiere que tarde un bloque (SEGUNDOS_POR_BLOQUE)
    ajustar: bool           # False: siempre el prefijo base (para la prueba de prefijos, BC-39)
    vivo_segundos: int      # un minero cuenta si avisó hace menos que esto
    ventana_ajuste: int = 5     # cada cuántos bloques minados se reajusta por tiempo (VENTANA_AJUSTE)
    ajuste_maximo: int = 1      # máximo de caracteres hex por ajuste (AJUSTE_MAXIMO)
    prefijo_inicial: int = 0    # 0: arranca según la potencia; > 0: arranca bajo para convergencia
    criterio: str = "suma"  # cómo se junta la potencia de los vivos; otro que "suma", sólo para BC-59
    emergencia: int = contrato.EMERGENCIA_SEGUNDOS  # un carácter menos por cada tantos segundos sin bloque (0 = no)


def cargar_ajustes():
    ajustes = Ajustes(
        espacio=config.entero("ESPACIO_NONCES", 2**32),
        porcentaje=config.entero("FRAGMENTO_PORCENTAJE", 10),
        prefijo_base=config.entero("PREFIJO_BASE", 6),
        segundos_objetivo=config.entero("SEGUNDOS_POR_BLOQUE", 60),
        ajustar=config.entero("AJUSTAR_PREFIJO", 1) == 1,
        vivo_segundos=config.entero("VIVO_SEGUNDOS", 15),
        ventana_ajuste=config.entero("VENTANA_AJUSTE", 5),
        ajuste_maximo=config.entero("AJUSTE_MAXIMO", 1),
        prefijo_inicial=config.entero("PREFIJO_INICIAL", 0),
        criterio=config.texto("CRITERIO_POTENCIA", "suma"),
        emergencia=config.entero("EMERGENCIA_SEGUNDOS", contrato.EMERGENCIA_SEGUNDOS),
    )
    if not 1 <= ajustes.porcentaje <= 50:
        raise ValueError("FRAGMENTO_PORCENTAJE va de 1 a 50")
    if not 1 <= ajustes.prefijo_base <= 32:
        raise ValueError("PREFIJO_BASE va de 1 a 32")
    if ajustes.espacio < 2 or ajustes.espacio > 2**64:
        raise ValueError("ESPACIO_NONCES va de 2 a 2^64")
    if ajustes.segundos_objetivo < 1:
        raise ValueError("SEGUNDOS_POR_BLOQUE tiene que ser >= 1")
    if ajustes.ventana_ajuste < 1:
        raise ValueError("VENTANA_AJUSTE tiene que ser >= 1")
    if ajustes.ajuste_maximo < 0:
        raise ValueError("AJUSTE_MAXIMO no puede ser negativo")
    if not 0 <= ajustes.prefijo_inicial <= 32:
        raise ValueError("PREFIJO_INICIAL va de 0 a 32")
    minimo = espacio_minimo(ajustes.prefijo_base)
    if ajustes.espacio < minimo:
        # BC-73: con menos, muchas tareas no tienen solución y se rearman: el ajuste por tiempo mide mal.
        raise ValueError(f"ESPACIO_NONCES ({ajustes.espacio}) tiene que ser al menos 16^PREFIJO_BASE × 4 = {minimo} "
                         f"para que casi todas las tareas tengan solución (con PREFIJO_BASE={ajustes.prefijo_base})")
    if ajustes.emergencia < 0:
        raise ValueError("EMERGENCIA_SEGUNDOS no puede ser negativo (0 la apaga)")
    if ajustes.criterio not in CRITERIOS:
        raise ValueError(f"CRITERIO_POTENCIA va {', '.join(CRITERIOS)}, no {ajustes.criterio!r}")
    return ajustes


def espacio_minimo(prefijo_base):
    """El espacio de nonces mínimo para un prefijo: 4 veces los intentos esperados (BC-73). Con eso, una
    tarea no tiene solución con probabilidad e^-4 ≈ 2 %, y casi nunca se rearma."""
    return 16 ** prefijo_base * 4


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


_PISOS = {}  # génesis → su piso de prefijo (no cambia mientras la cadena sea la misma)


def piso_de_la_cadena(redis):
    """El piso de prefijo que fijó el génesis (v4, BC-97): ninguna tarea baja de ahí, ni sin mineros ni en la bajada
    de emergencia, porque los nodos rechazarían el bloque. Sin génesis, 1."""
    genesis = redis.lindex(contrato.CADENA, 0) if hasattr(redis, "lindex") else None
    if not genesis:
        return 1
    if genesis not in _PISOS:
        guardadas = redis.hget(contrato.BLOQUE.format(genesis), "transactions")
        if guardadas is None:
            return 1
        _PISOS[genesis] = contrato.prefijo_minimo_de({"transactions": json.loads(guardadas)})
    return _PISOS[genesis]


def elegir_prefijo_con_motivo(redis, ajustes, ahora, armada=None):
    """(prefijo, motivo, potencia en hashes/s): BC-60 y BC-73 (ver `particion.decidir_prefijo`), con la potencia
    viva juntada según el criterio (BC-59). Sin ajuste, la potencia es None porque no se mira.

    `armada`: cuándo se armó por primera vez la caja de la tarea (la manda el NCT), para la bajada de emergencia.
    Si hubo un recálculo por potencia (o es el primero de la cadena), queda en `prefijo:referencia`.
    v4: nunca menos que el piso de la cadena (motivo «piso»): si no hay potencia para el piso, la tarea espera.
    """
    piso = piso_de_la_cadena(redis)
    if not ajustes.ajustar:
        if ajustes.prefijo_base < piso:
            return "0" * piso, "piso", None
        return "0" * ajustes.prefijo_base, "fijo", None
    nodos = [redis.hgetall(clave) for clave in redis.scan_iter(match=contrato.NODOS, count=contrato.NODOS_POR_SCAN)]
    potencia = potencia_viva(nodos, ahora, ajustes.vivo_segundos, ajustes.criterio)
    hashes = redis.lrange(contrato.CADENA, -(ajustes.ventana_ajuste + 1), -1) if hasattr(redis, "lrange") else []
    bloques = [b for b in (redis.hgetall(contrato.BLOQUE.format(h)) for h in hashes) if b]
    referencia = redis.hgetall(contrato.PREFIJO_REFERENCIA)
    largo, motivo, nueva = decidir_prefijo(potencia, bloques, ajustes, referencia, ahora, armada)
    if largo < piso:
        largo, motivo = piso, "piso"
        if nueva is not None:
            nueva = {**nueva, "prefijo": piso}
    if nueva is not None:
        redis.hset(contrato.PREFIJO_REFERENCIA, mapping=nueva)
        if motivo in ("inicial", "potencia"):
            log.info("Prefijo por potencia (%s): %d caracteres con %s", motivo, largo, legible(potencia))
    return "0" * largo, motivo, potencia


def elegir_prefijo(redis, ajustes, ahora, armada=None):
    """(prefijo, potencia en hashes/s); sin ajuste, la potencia es None porque no se mira."""
    prefijo, _, potencia = elegir_prefijo_con_motivo(redis, ajustes, ahora, armada)
    return prefijo, potencia


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
    armada = tarea.get("armada") if isinstance(tarea.get("armada"), (int, float)) else None
    prefijo, motivo, potencia = elegir_prefijo_con_motivo(redis, ajustes, time.time() if ahora is None else ahora,
                                                          armada)
    rangos = partir(ajustes.espacio, ajustes.porcentaje)
    redis.hset(contrato.TAREA.format(tarea["tarea"]), mapping={"prefijo": prefijo, "rangos": len(rangos)})
    for numero, (desde, hasta) in enumerate(rangos):
        publicar(RANGOS, {"tarea": tarea["tarea"], "rango": numero, "desde": desde, "hasta": hasta,
                          "prefijo": prefijo})
    RANGOS_PUBLICADOS.inc(len(rangos))
    PREFIJO.set(len(prefijo))
    PREFIJO_MOTIVO.labels(motivo).inc()
    TIEMPO_ESPERADO.set(ajustes.segundos_objetivo)
    # Las pruebas de pilar3 leen esta línea (fragmentos.py, potencia.py, dificultad.py): agregar sólo al final.
    detalle = "" if potencia is None else f", criterio {ajustes.criterio}, potencia {legible(potencia)}"
    log.info("Tarea %s: %d transacciones, prefijo %s, %d rangos del %d %%%s, motivo %s",
             tarea["tarea"], len(tarea["transacciones"]), prefijo, len(rangos), ajustes.porcentaje, detalle, motivo)


def main():
    ajustes = cargar_ajustes()
    TIEMPO_ESPERADO.set(ajustes.segundos_objetivo)
    servicio = Servicio("trp", colas=[TAREAS, RANGOS])
    servicio.log.info("Ajustes: %s", ajustes)
    servicio.servir_http()
    # Una conexión por cola consumida, cada una en su hilo; cada consumidor publica desde su propio
    # manejador (ver comun/rabbit.py).
    de_resultados = Rabbit(servicio.config, [RESULTADOS, SOLUCIONES])
    threading.Thread(target=de_resultados.consumir, name="resultados", daemon=True,
                     args=(RESULTADOS, lambda r: recibir(r, servicio.redis, de_resultados.publicar))).start()
    servicio.rabbit.consumir(TAREAS, lambda tarea: particionar(tarea, servicio.redis, servicio.rabbit.publicar, ajustes))
