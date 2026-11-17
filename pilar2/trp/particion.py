"""Cómo el TrP parte una tarea (BC-21 y BC-60): rango de nonce y ajuste dinámico de dificultad."""

import math


def partir(espacio, porcentaje):
    """Parte [0, espacio - 1] en rangos del `porcentaje` % del espacio: [(desde, hasta), ...].

    Cerrados, contiguos y sin solaparse; el último puede ser más corto. 1 % → 100 rangos, 50 % → 2.
    """
    if not 1 <= porcentaje <= 50:
        raise ValueError("el porcentaje va de 1 a 50")
    tamano = -(-espacio * porcentaje // 100)  # techo, para no dejar un resto suelto al final
    return [(desde, min(desde + tamano, espacio) - 1) for desde in range(0, espacio, tamano)]


CRITERIOS = ("suma", "promedio", "minimo", "maximo")


def potencia_viva(nodos, ahora, vivo_segundos, criterio="suma"):
    """Potencia (hashes/s) de los mineros que avisaron hace menos de `vivo_segundos`, según su `bench`.

    `criterio` dice cómo se juntan los bench: "suma" es la del sistema, porque un bloque tarda 16^p
    intentos dividido la potencia de TODO el pool (minan todos). "promedio", "minimo" y "maximo"
    existen sólo para la prueba de BC-59 (`CRITERIO_POTENCIA`). Sin mineros vivos, 0 con cualquiera.
    `nodos` son los hashes `nodo:<id>` como los devuelve Redis (todo texto).
    """
    if criterio not in CRITERIOS:
        raise ValueError(f"criterio desconocido: {criterio!r} (va {', '.join(CRITERIOS)})")
    benchs = []
    for nodo in nodos:
        try:
            if ahora - float(nodo.get("ultimo_aviso", 0)) < vivo_segundos:
                benchs.append(float(nodo.get("bench", 0)))
        except ValueError:
            continue  # un nodo a medio escribir no cuenta
    if not benchs:
        return 0.0
    if criterio == "promedio":
        return sum(benchs) / len(benchs)
    if criterio == "minimo":
        return min(benchs)
    if criterio == "maximo":
        return max(benchs)
    return sum(benchs)


def largo_prefijo(potencia, segundos_objetivo, base):
    """Techo del prefijo para que el bloque salga en unos `segundos_objetivo` con esa potencia.

    Con p caracteres hacen falta 16^p intentos en promedio: p = piso(log16(potencia × T)).
    Nunca más que `base` (el prefijo del sistema) ni menos que 1. Sin mineros vivos, 1: el primero
    que llegue (por ejemplo, el minero CPU que levanta el escalador) lo resuelve enseguida.
    """
    intentos = potencia * segundos_objetivo
    largo = 1
    while largo < base and 16 ** (largo + 1) <= intentos:  # sin log: math.log(16**6, 16) da 5,999…
        largo += 1
    return largo


def ajustar_por_tiempo(prefijo_actual, tiempos, esperado, ajuste_maximo=1, minimo=1, maximo=32):
    """Ajusta el largo del prefijo según cuánto tardaron los últimos bloques (BC-60).

    Basado en el ajuste de dificultad de Bitcoin (Satoshi Nakamoto, sección 4; retarget cada
    2016 bloques con tope ×4 en Bitcoin Core): `nueva = actual × esperado / real`. Como en Chimera
    cada carácter hexadecimal multiplica por 16 los intentos esperados, el salto en caracteres es
    `redondeo(log16(esperado / real))`, acotado a `±ajuste_maximo` (con `ajuste_maximo=1`, cambia
    recién a partir de ×4 más rápido o ×4 más lento, ya que log16(4) = 0,5).
    """
    if esperado <= 0:
        raise ValueError("esperado tiene que ser mayor a 0")
    if ajuste_maximo < 0:
        raise ValueError("ajuste_maximo no puede ser negativo")
    acotado = max(minimo, min(maximo, prefijo_actual))
    if not tiempos:
        return acotado
    real = sum(tiempos) / len(tiempos)
    if real <= 0:
        delta = ajuste_maximo
    else:
        exponente = math.log(esperado / real, 16)
        # Redondeo simétrico alejándose de cero en ±0,5 (exactamente ×4 o ÷4), tolerando float.
        delta = int(exponente + 0.5 + 1e-12) if exponente >= 0 else -int(-exponente + 0.5 + 1e-12)
        delta = max(-ajuste_maximo, min(ajuste_maximo, delta))
    return max(minimo, min(maximo, acotado + delta))


def tiempos_desde_bloques(bloques, ventana):
    """Segundos por bloque de los últimos hasta `ventana` bloques (del más viejo al más nuevo).

    Si los últimos `ventana` bloques minados tienen `duracion` (guardada por el NCT al confirmar,
    sin contar tiempo ocioso sin transacciones), usa esa duración; si no, resta los `timestamp`
    consecutivos de hasta `ventana + 1` bloques.
    """
    if ventana <= 0 or not bloques:
        return []
    minados = [b for b in bloques if str(b.get("altura", "")) != "0" and b.get("prefijo")]
    recientes_minados = minados[-ventana:]
    if len(recientes_minados) >= ventana and all("duracion" in b for b in recientes_minados):
        tiempos = []
        for b in recientes_minados:
            try:
                tiempos.append(max(0.0, float(b["duracion"])))
            except ValueError:
                tiempos = []
                break
        if len(tiempos) == len(recientes_minados):
            return tiempos

    con_ts = []
    for b in bloques:
        try:
            con_ts.append(int(b["timestamp"]))
        except (KeyError, TypeError, ValueError):
            continue
    con_ts = con_ts[-(ventana + 1):]
    return [max(0.0, float(actual - previo)) for previo, actual in zip(con_ts, con_ts[1:])]


def decidir_prefijo(potencia, bloques, ajustes):
    """Elige `(largo, motivo)` combinando ajuste por tiempo cada `ventana_ajuste` y techo por potencia.

    - Con `ajustar=False`: devuelve `(prefijo_base, "fijo")`.
    - Sin bloques minados previos: arranca como siempre con el techo por potencia (`"inicial"`),
      salvo que `prefijo_inicial > 0` (para pruebas de convergencia).
    - Con bloques previos: ajusta por tiempo cada `ventana_ajuste` bloques; entre ajustes mantiene
      el último prefijo. En todo momento `largo_prefijo` por potencia viva impone el techo (`"techo"`),
      de modo que si se cae la GPU la dificultad baja de inmediato sin esperar bloques lentos.
    """
    if not ajustes.ajustar:
        return ajustes.prefijo_base, "fijo"

    techo = largo_prefijo(potencia, ajustes.segundos_objetivo, ajustes.prefijo_base)
    minados = [b for b in bloques if str(b.get("altura", "")) != "0" and b.get("prefijo")]
    if not minados:
        inicial = getattr(ajustes, "prefijo_inicial", 0)
        if inicial > 0:
            elegido = min(inicial, techo)
            return elegido, "techo" if techo < inicial else "inicial"
        return techo, "inicial"

    ultimo = minados[-1]
    prefijo_anterior = max(1, min(len(ultimo["prefijo"]), ajustes.prefijo_base))
    try:
        altura_tip = int(ultimo.get("altura", len(minados)))
    except (TypeError, ValueError):
        altura_tip = len(minados)

    por_tiempo = prefijo_anterior
    if altura_tip > 0 and altura_tip % ajustes.ventana_ajuste == 0:
        tiempos = tiempos_desde_bloques(bloques, ajustes.ventana_ajuste)
        if tiempos:
            por_tiempo = ajustar_por_tiempo(
                prefijo_anterior,
                tiempos,
                ajustes.segundos_objetivo,
                ajustes.ajuste_maximo,
                minimo=1,
                maximo=ajustes.prefijo_base,
            )

    elegido = min(por_tiempo, techo)
    return elegido, "techo" if techo < por_tiempo else "tiempo"
