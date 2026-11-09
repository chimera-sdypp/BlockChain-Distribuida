"""Cómo el TrP parte una tarea (BC-21): la dificultad según los mineros vivos y los rangos de nonce."""


def partir(espacio, porcentaje):
    """Parte [0, espacio - 1] en rangos del `porcentaje` % del espacio: [(desde, hasta), ...].

    Cerrados, contiguos y sin solaparse; el último puede ser más corto. 1 % → 100 rangos, 50 % → 2.
    """
    if not 1 <= porcentaje <= 50:
        raise ValueError("el porcentaje va de 1 a 50")
    tamano = -(-espacio * porcentaje // 100)  # techo, para no dejar un resto suelto al final
    return [(desde, min(desde + tamano, espacio) - 1) for desde in range(0, espacio, tamano)]


def potencia_viva(nodos, ahora, vivo_segundos):
    """Suma de los bench (hashes/s) de los mineros que avisaron hace menos de `vivo_segundos`.

    `nodos` son los hashes `nodo:<id>` como los devuelve Redis (todo texto).
    """
    total = 0.0
    for nodo in nodos:
        try:
            if ahora - float(nodo.get("ultimo_aviso", 0)) < vivo_segundos:
                total += float(nodo.get("bench", 0))
        except ValueError:
            continue  # un nodo a medio escribir no cuenta
    return total


def largo_prefijo(potencia, segundos_objetivo, base):
    """Caracteres de prefijo para que el bloque salga en unos `segundos_objetivo` con esa potencia.

    Con p caracteres hacen falta 16^p intentos en promedio: p = piso(log16(potencia × T)).
    Nunca más que `base` (el prefijo del sistema) ni menos que 1. Sin mineros vivos, 1: el primero
    que llegue (por ejemplo, el minero CPU que levanta el escalador) lo resuelve enseguida.
    """
    intentos = potencia * segundos_objetivo
    largo = 1
    while largo < base and 16 ** (largo + 1) <= intentos:  # sin log: math.log(16**6, 16) da 5,999…
        largo += 1
    return largo
