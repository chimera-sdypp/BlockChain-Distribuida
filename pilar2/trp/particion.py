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


def evento_potencia(potencia, referencia, hubo_bloque):
    """BC-73: si la potencia viva cambió 4 veces o más desde el último recálculo por potencia.

    Devuelve "sube", "baja" o None. El umbral es el mismo del ajuste por tiempo: |log16(P / ref)| ≥ 0,5.
    - Bajar es la dirección segura (bloques más fáciles) y para «irse» un minero tiene que dejar de
      avisar, o sea, de minar: se recalcula en el acto.
    - Subir pide al menos un bloque minado desde el último recálculo. Así un minero que parpadea (una
      Colab que pierde la red 20 s) no hace ir y venir el prefijo, y un bench inflado no lo sube dos
      veces seguidas. La excepción es venir de ninguna potencia: el primero que llega recalcula.
    """
    if referencia <= 0:
        return "sube" if potencia > 0 else None
    if potencia <= 0:
        return "baja"
    cambio = math.log(potencia / referencia, 16)
    if cambio <= -0.5:
        return "baja"
    if cambio >= 0.5 and hubo_bloque:
        return "sube"
    return None


def nivel_emergencia(ahora, desde, emergencia):
    """BC-73: cuántos caracteres bajar porque no sale un bloque: uno por cada `emergencia` segundos desde
    `desde` (cuando se armó la caja, o el último recálculo si fue después). Como la regla de la testnet de
    Bitcoin: si pasan 20 minutos sin bloque, se permite la dificultad mínima."""
    if emergencia <= 0 or desde is None or ahora <= desde:
        return 0
    return int((ahora - desde) // emergencia)


def _altura(bloque):
    try:
        return int(bloque.get("altura", 0))
    except (TypeError, ValueError):
        return 0


def decidir_prefijo(potencia, bloques, ajustes, referencia=None, ahora=0.0, armada=None):
    """Elige `(largo, motivo, referencia nueva o None)` (BC-60 y BC-73).

    - `ajustar=False`: `(prefijo_base, "fijo", None)`, para las pruebas de prefijo fijo (BC-39).
    - Sin referencia (cadena nueva o reiniciada): el techo por potencia (`"inicial"`), o `prefijo_inicial`
      si se configuró (pruebas de convergencia). Se anota como referencia.
    - Si la potencia viva cambió 4 veces o más (`evento_potencia`): el techo con la potencia nueva
      (`"potencia"`), y se anota como referencia.
    - Si no, el ajuste por tiempo (`"tiempo"`): el prefijo del último bloque minado (o el de la
      referencia, si después no se minó ninguno), reajustado cada `ventana_ajuste` bloques. El techo ya
      no lo limita: un bench que dice de menos no frena al tiempo.
    - Bajada de emergencia (`"emergencia"`): un carácter menos por cada `emergencia` segundos sin bloque
      desde que se armó la caja (o desde el último recálculo por potencia, si fue después).

    `bloques`: los últimos de `cadena` (del más viejo al más nuevo). `referencia`: lo que hay en
    `prefijo:referencia` (dict con potencia, altura, prefijo y cuando), o None. El tercer valor, si no es
    None, es lo que hay que guardar como referencia nueva.
    """
    if not ajustes.ajustar:
        return ajustes.prefijo_base, "fijo", None
    techo = largo_prefijo(potencia, ajustes.segundos_objetivo, ajustes.prefijo_base)
    punta = _altura(bloques[-1]) if bloques else 0

    def nueva(largo):
        return {"potencia": potencia, "altura": punta, "prefijo": largo, "cuando": ahora}

    try:
        ref = {"potencia": float(referencia["potencia"]), "altura": int(referencia["altura"]),
               "prefijo": int(referencia["prefijo"]), "cuando": float(referencia["cuando"])}
    except (KeyError, TypeError, ValueError):
        ref = None
    if ref is not None and ref["altura"] > punta:
        ref = None  # la referencia es de otra cadena (se reinició)
    guardar = None
    if ref is None:
        con_prefijo = [b for b in bloques if _altura(b) > 0 and b.get("prefijo")]
        if not con_prefijo:
            inicial = getattr(ajustes, "prefijo_inicial", 0)
            largo = min(inicial, techo) if inicial > 0 else techo
            return largo, "inicial", nueva(largo)
        # Una cadena que ya tiene bloques pero no referencia (se actualizó desde antes de BC-73): se sigue
        # por tiempo desde el último bloque, y la potencia de ahora queda como referencia.
        ref = {"potencia": potencia, "altura": 0, "prefijo": len(con_prefijo[-1]["prefijo"]), "cuando": ahora}
        guardar = ref
    elif evento_potencia(potencia, ref["potencia"], punta > ref["altura"]):
        return techo, "potencia", nueva(techo)

    minados = [b for b in bloques if _altura(b) > ref["altura"] and b.get("prefijo")]
    if minados:
        anterior = max(1, min(len(minados[-1]["prefijo"]), ajustes.prefijo_base))
        largo = anterior
        if punta > 0 and punta % ajustes.ventana_ajuste == 0:
            tiempos = tiempos_desde_bloques(bloques, ajustes.ventana_ajuste)
            if tiempos:
                largo = ajustar_por_tiempo(anterior, tiempos, ajustes.segundos_objetivo, ajustes.ajuste_maximo,
                                           minimo=1, maximo=ajustes.prefijo_base)
    else:
        largo = max(1, min(ref["prefijo"], ajustes.prefijo_base))

    desde = ref["cuando"] if armada is None else max(float(armada), ref["cuando"])
    bajar = nivel_emergencia(ahora, desde, getattr(ajustes, "emergencia", 0))
    if bajar > 0 and largo > 1:
        return max(1, largo - bajar), "emergencia", guardar
    return largo, "tiempo", guardar
