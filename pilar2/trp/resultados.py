"""TrP · recibir los resultados (BC-22): de lo que devuelven los mineros, al NCT le pasa sólo el ganador.

No es un joiner: no se junta nada. Gana el primer nonce válido de cada tarea; los siguientes se
descartan. Si todos los rangos vuelven sin solución, le avisa al NCT para que rearme el bloque.
"""

import json
import logging
import re

from prometheus_client import Counter

from comun import contrato

log = logging.getLogger("trp")

SOLUCIONES = "soluciones"
SIN_SOLUCION = "sin_solucion"  # valor de `ganador` cuando ningún rango tuvo solución
SIN_SOLUCION_RANGOS = "tarea:{}:sin_solucion"  # conjunto con los rangos que volvieron sin solución
HASH = re.compile(r"^[0-9a-f]{32}$")

RANGOS_SIN_SOLUCION = Counter("blockchain_rangos_sin_solucion", "Rangos que los mineros devolvieron sin solución")
RESULTADOS_DESCARTADOS = Counter("blockchain_resultados_descartados", "Resultados descartados", ["motivo"])


def errores_resultado(r):
    if not isinstance(r, dict):
        return ["no es un objeto"]
    errores = []
    if not isinstance(r.get("tarea"), str) or not r["tarea"]:
        errores.append("tarea")
    if not isinstance(r.get("rango"), int) or isinstance(r.get("rango"), bool) or r["rango"] < 0:
        errores.append("rango")
    if not contrato.usuario_valido(r.get("minero")):
        errores.append("minero")
    if r.get(SIN_SOLUCION) is not True:
        if not isinstance(r.get("nonce"), int) or isinstance(r.get("nonce"), bool) or r["nonce"] < 0:
            errores.append("nonce")
        if not isinstance(r.get("hash"), str) or not HASH.match(r["hash"]):
            errores.append("hash")
    return errores


def _descartar(motivo, mensaje, *args):
    RESULTADOS_DESCARTADOS.labels(motivo).inc()
    log.info(mensaje, *args)


def recibir(resultado, redis, publicar):
    """Procesa un mensaje de `resultados`. El ack lo hace comun/rabbit.py al terminar.

    `ganador` en tarea:<id> se escribe una sola vez (HSETNX), así los 2 pods no publican dos
    soluciones. Si el pod muere entre el HSETNX y el publicar, el mensaje vuelve y, como el ganador
    guardado es el mismo, se publica de nuevo: el NCT tiene que tolerar una solución repetida.
    """
    errores = errores_resultado(resultado)
    if errores:
        log.error("Resultado inválido, se descarta (%s): %r", ", ".join(errores), resultado)
        RESULTADOS_DESCARTADOS.labels("invalido").inc()
        return
    id_, clave = resultado["tarea"], contrato.TAREA.format(resultado["tarea"])
    tarea = redis.hgetall(clave)
    if "bloque" not in tarea:
        return _descartar("tarea_desconocida", "Resultado de una tarea que no partió el TrP (%s); se descarta", id_)

    if resultado.get(SIN_SOLUCION):
        redis.sadd(SIN_SOLUCION_RANGOS.format(id_), resultado["rango"])  # conjunto: un rango repetido no suma
        RANGOS_SIN_SOLUCION.inc()
        devueltos = redis.scard(SIN_SOLUCION_RANGOS.format(id_))
        if devueltos < int(tarea["rangos"]):
            return
        ganador = SIN_SOLUCION
    else:
        # El minero puede mentir: se verifica el hash con el texto y el prefijo de la tarea.
        hash_ = contrato.hash_bloque(tarea["bloque"], resultado["nonce"])
        if hash_ != resultado["hash"] or not contrato.cumple(hash_, tarea["prefijo"]):
            return _descartar("hash_incorrecto", "El nonce %d de %s para la tarea %s no da el hash; se descarta",
                              resultado["nonce"], resultado["minero"], id_)
        ganador = json.dumps({"minero": resultado["minero"], "nonce": resultado["nonce"], "hash": hash_})

    if not redis.hsetnx(clave, "ganador", ganador) and redis.hget(clave, "ganador") != ganador:
        return _descartar("tarde", "Tarea %s ya resuelta: se descarta el resultado del rango %s de %s",
                          id_, resultado["rango"], resultado["minero"])

    if ganador == SIN_SOLUCION:
        log.info("Tarea %s: los %s rangos volvieron sin solución", id_, tarea["rangos"])
        publicar(SOLUCIONES, {"tarea": id_, SIN_SOLUCION: True})
    else:
        log.info("Tarea %s: ganó %s con el nonce %d", id_, resultado["minero"], resultado["nonce"])
        publicar(SOLUCIONES, {"tarea": id_, "nonce": resultado["nonce"], "hash": resultado["hash"],
                              "prefijo": tarea["prefijo"], "minero": resultado["minero"]})
