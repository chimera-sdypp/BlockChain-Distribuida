from dataclasses import dataclass

import pytest

from trp.particion import (
    CRITERIOS,
    ajustar_por_tiempo,
    decidir_prefijo,
    evento_potencia,
    largo_prefijo,
    partir,
    potencia_viva,
    tiempos_desde_bloques,
)


@pytest.mark.parametrize("porcentaje, cantidad", [(1, 100), (10, 10), (30, 4), (50, 2)])
def test_rangos_sin_huecos_ni_solapamientos(porcentaje, cantidad):
    espacio = 2**32
    rangos = partir(espacio, porcentaje)
    assert len(rangos) == cantidad
    assert rangos[0][0] == 0 and rangos[-1][1] == espacio - 1
    for (_, hasta), (desde, _) in zip(rangos, rangos[1:]):
        assert desde == hasta + 1
    assert sum(hasta - desde + 1 for desde, hasta in rangos) == espacio


def test_espacio_que_no_divide_justo():
    assert partir(101, 50) == [(0, 50), (51, 100)]
    assert partir(7, 10) == [(i, i) for i in range(7)]  # rangos de 1: no puede haber más que nonces


@pytest.mark.parametrize("porcentaje", [0, 51, 100])
def test_porcentaje_fuera_de_rango(porcentaje):
    with pytest.raises(ValueError):
        partir(1000, porcentaje)


def test_potencia_cuenta_solo_los_vivos():
    ahora = 1_000_000
    nodos = [
        {"tipo": "gpu", "bench": "2300000000", "ultimo_aviso": str(ahora - 3)},
        {"tipo": "cpu", "bench": "8000000", "ultimo_aviso": str(ahora - 14)},
        {"tipo": "gpu", "bench": "2300000000", "ultimo_aviso": str(ahora - 60)},  # muerto
        {"tipo": "cpu", "bench": "no-numero", "ultimo_aviso": str(ahora)},       # a medio escribir
        {"tipo": "cpu"},                                                           # sin aviso todavía
    ]
    assert potencia_viva(nodos, ahora, 15) == 2_308_000_000


GRUPO = [  # 1 GPU y 2 CPU, todos vivos
    {"tipo": "gpu", "bench": "1800000000", "ultimo_aviso": "100"},
    {"tipo": "cpu", "bench": "8600000", "ultimo_aviso": "100"},
    {"tipo": "cpu", "bench": "8600000", "ultimo_aviso": "100"},
    {"tipo": "gpu", "bench": "9000000000", "ultimo_aviso": "0"},  # muerto: no cuenta para ningún criterio
]


@pytest.mark.parametrize("criterio, potencia, largo", [
    ("suma", 1_817_200_000, 8),
    ("promedio", 1_817_200_000 / 3, 8),
    ("maximo", 1_800_000_000, 8),
    ("minimo", 8_600_000, 6),  # 8,6 MH/s × 30 s ≈ 2,6 × 10^8 < 16^7
])
def test_cada_criterio_con_una_gpu_y_dos_cpu(criterio, potencia, largo):
    assert potencia_viva(GRUPO, 105, 15, criterio) == pytest.approx(potencia)
    assert largo_prefijo(potencia_viva(GRUPO, 105, 15, criterio), 30, base=8) == largo


def test_por_defecto_la_suma():
    assert potencia_viva(GRUPO, 105, 15) == potencia_viva(GRUPO, 105, 15, "suma")


@pytest.mark.parametrize("criterio", CRITERIOS)
def test_sin_mineros_vivos_la_potencia_es_cero(criterio):
    assert potencia_viva([], 105, 15, criterio) == 0
    assert potencia_viva(GRUPO, 10_000, 15, criterio) == 0


def test_criterio_desconocido():
    with pytest.raises(ValueError):
        potencia_viva(GRUPO, 105, 15, "mediana")


@pytest.mark.parametrize("potencia, largo", [
    (0, 1),                 # sin mineros: el más fácil
    (8_600_000, 6),         # un minero CPU (8,6 MH/s × 30 s ≈ 2,6 × 10^8 ≈ 16^6,9)
    (500_000, 5),           # un CPU lento
    (2_300_000_000, 6),     # con GPU daría 9, pero no pasa del prefijo base
])
def test_largo_del_prefijo_segun_la_potencia(potencia, largo):
    assert largo_prefijo(potencia, 30, base=6) == largo


def test_largo_exacto_en_potencias_de_16():
    assert largo_prefijo(16**6, 1, base=8) == 6
    assert largo_prefijo(16**6 - 1, 1, base=8) == 5
    assert largo_prefijo(2_300_000_000, 30, base=12) == 9  # con GPU y sin tope bajo


# --- BC-60: ajuste de dificultad por tiempo del bloque anterior -----------------------------

@dataclass(frozen=True)
class AjustesFalsos:
    prefijo_base: int = 8
    segundos_objetivo: int = 60
    ajustar: bool = True
    ventana_ajuste: int = 5
    ajuste_maximo: int = 1
    prefijo_inicial: int = 0
    emergencia: int = 240


def bloque(altura, prefijo, timestamp, duracion=None):
    datos = {"altura": str(altura), "prefijo": "0" * prefijo if prefijo else "", "timestamp": str(timestamp)}
    if duracion is not None:
        datos["duracion"] = str(duracion)
    return datos


def test_ajustar_por_tiempo_sube_con_bloques_rapidos():
    # Con esperado=60, <=15 s (>=4x más rápido) sube +1
    assert ajustar_por_tiempo(4, [2, 3, 2, 1, 2], esperado=60) == 5
    assert ajustar_por_tiempo(4, [15, 15, 15, 15, 15], esperado=60) == 5
    # Bloques instantáneos (0 s) no dividen por cero y suben +1
    assert ajustar_por_tiempo(4, [0, 0, 0, 0, 0], esperado=60) == 5


def test_ajustar_por_tiempo_baja_con_bloques_lentos():
    # Con esperado=60, >=240 s (>=4x más lento) baja -1
    assert ajustar_por_tiempo(6, [300, 280, 260, 300, 260], esperado=60) == 5
    assert ajustar_por_tiempo(6, [240, 240, 240, 240, 240], esperado=60) == 5


def test_ajustar_por_tiempo_no_cambia_si_esta_dentro_de_4x():
    assert ajustar_por_tiempo(5, [20, 25, 30, 20, 25], esperado=60) == 5
    assert ajustar_por_tiempo(5, [60, 60, 60, 60, 60], esperado=60) == 5
    assert ajustar_por_tiempo(5, [150, 180, 170, 160, 180], esperado=60) == 5


def test_ajustar_por_tiempo_no_se_mueve_mas_de_uno():
    # Aunque haya sido 1000x más rápido o más lento, el tope por ajuste es ±1
    assert ajustar_por_tiempo(3, [0.05, 0.05, 0.05, 0.05, 0.05], esperado=60) == 4
    assert ajustar_por_tiempo(7, [60_000, 60_000, 60_000, 60_000, 60_000], esperado=60) == 6
    # Tampoco baja de 1 ni pasa de maximo
    assert ajustar_por_tiempo(1, [60_000] * 5, esperado=60) == 1
    assert ajustar_por_tiempo(6, [1] * 5, esperado=60, maximo=6) == 6


def test_ajustar_por_tiempo_sin_tiempos_mantiene_el_actual():
    assert ajustar_por_tiempo(5, [], esperado=60) == 5


def test_tiempos_desde_bloques_usa_duracion_o_timestamp():
    solo_ts = [bloque(i, 4 if i else 0, 1000 + i * 10) for i in range(6)]
    assert tiempos_desde_bloques(solo_ts, 5) == [10.0, 10.0, 10.0, 10.0, 10.0]

    con_dur = [bloque(0, 0, 0)] + [bloque(i, 4, 1000 + i * 500, duracion=3.5) for i in range(1, 6)]
    assert tiempos_desde_bloques(con_dur, 5) == [3.5, 3.5, 3.5, 3.5, 3.5]


def test_primera_vez_sin_bloques_previos_el_techo_y_queda_de_referencia():
    solo_genesis = [bloque(0, 0, 1000)]
    largo, motivo, ref = decidir_prefijo(2_300_000_000, solo_genesis, AjustesFalsos(prefijo_base=6), ahora=50.0)
    assert (largo, motivo) == (6, "inicial")
    assert ref == {"potencia": 2_300_000_000, "altura": 0, "prefijo": 6, "cuando": 50.0}
    assert decidir_prefijo(200_000, solo_genesis, AjustesFalsos(prefijo_base=6))[:2] == (5, "inicial")
    assert decidir_prefijo(0, [], AjustesFalsos(prefijo_base=6))[:2] == (1, "inicial")


def test_entre_ventanas_mantiene_el_ultimo_y_reajusta_cada_n_bloques():
    a = AjustesFalsos(prefijo_base=8, segundos_objetivo=60, ventana_ajuste=5)
    # 4 bloques rápidos (altura 4): todavía no toca reajuste -> mantiene 4
    cuatro = [bloque(0, 0, 100)] + [bloque(i, 4, 100 + i * 2) for i in range(1, 5)]
    assert decidir_prefijo(2_300_000_000, cuatro, a)[:2] == (4, "tiempo")
    # Al 5.º bloque (altura 5): promedio 2 s vs 60 s -> sube a 5
    cinco = cuatro + [bloque(5, 4, 110)]
    assert decidir_prefijo(2_300_000_000, cinco, a)[:2] == (5, "tiempo")


def test_una_cadena_con_bloques_y_sin_referencia_sigue_por_tiempo():
    # Se actualizó desde antes de BC-73: no vuelve al techo, sigue desde el último bloque y anota la referencia.
    a = AjustesFalsos(prefijo_base=8)
    cuatro = [bloque(0, 0, 100)] + [bloque(i, 4, 100 + i * 2) for i in range(1, 5)]
    largo, motivo, ref = decidir_prefijo(2_300_000_000, cuatro, a, ahora=7.0)
    assert (largo, motivo) == (4, "tiempo")
    assert ref == {"potencia": 2_300_000_000, "altura": 0, "prefijo": 4, "cuando": 7.0}


def referencia(potencia, altura, prefijo, cuando=0.0):
    return {"potencia": str(potencia), "altura": str(altura), "prefijo": str(prefijo), "cuando": str(cuando)}


def test_un_bench_bajo_ya_no_frena_al_ajuste_por_tiempo():
    # El caso de la RTX medida en frío (689 MH/s): el techo da 8, pero los bloques salen en 3 s.
    a = AjustesFalsos(prefijo_base=12, segundos_objetivo=60, ventana_ajuste=5)
    cadena = [bloque(0, 0, 100)] + [bloque(i, 8, 100 + i * 3, duracion=3) for i in range(1, 6)]
    largo, motivo, _ = decidir_prefijo(689_000_000, cadena, a, referencia(689_000_000, 0, 8))
    assert (largo, motivo) == (9, "tiempo")  # antes de BC-73 quedaba clavado en 8 por el techo


def test_la_potencia_que_cae_4_veces_recalcula_en_el_acto():
    a = AjustesFalsos(prefijo_base=12, segundos_objetivo=60)
    cadena = [bloque(0, 0, 100)] + [bloque(i, 9, 100 + i * 34, duracion=34) for i in range(1, 4)]
    # Se fue la RTX (2 GH/s) y quedan 2 VMs CPU (4 MH/s): ÷500 -> techo 6 sin esperar bloques
    largo, motivo, ref = decidir_prefijo(4_000_000, cadena, a, referencia(2_000_000_000, 0, 9), ahora=500.0)
    assert (largo, motivo) == (6, "potencia")
    assert ref == {"potencia": 4_000_000, "altura": 3, "prefijo": 6, "cuando": 500.0}


def test_la_potencia_que_sube_4_veces_recalcula_si_hubo_un_bloque():
    a = AjustesFalsos(prefijo_base=12, segundos_objetivo=60)
    cadena = [bloque(0, 0, 100), bloque(1, 6, 117, duracion=17)]
    # Entra la RTX y sólo había 2 VMs: ×500, y desde la referencia (altura 0) se minó un bloque -> techo 9
    assert decidir_prefijo(2_000_000_000, cadena, a, referencia(4_000_000, 0, 6))[:2] == (9, "potencia")
    # Sin bloques desde el último recálculo no sube (un minero que parpadea no hace ir y venir el prefijo)
    assert decidir_prefijo(2_000_000_000, cadena, a, referencia(4_000_000, 1, 6))[:2] == (6, "tiempo")


def test_desde_ninguna_potencia_el_primero_que_llega_recalcula():
    a = AjustesFalsos(prefijo_base=12, segundos_objetivo=60)
    cadena = [bloque(0, 0, 100), bloque(1, 1, 101, duracion=1)]
    assert decidir_prefijo(2_000_000_000, cadena, a, referencia(0, 1, 1))[:2] == (9, "potencia")


def test_un_cambio_menor_a_4_veces_no_recalcula():
    a = AjustesFalsos(prefijo_base=12, segundos_objetivo=60)
    cadena = [bloque(0, 0, 100)] + [bloque(i, 9, 100 + i * 34, duracion=34) for i in range(1, 3)]
    # Se suma una T4 a la RTX: 2 -> 3,6 GH/s (×1,8). Sigue por tiempo, con el prefijo del último bloque.
    assert decidir_prefijo(3_600_000_000, cadena, a, referencia(2_000_000_000, 0, 9))[:2] == (9, "tiempo")
    # Y si baja ×2 tampoco
    assert decidir_prefijo(1_000_000_000, cadena, a, referencia(2_000_000_000, 0, 9))[:2] == (9, "tiempo")


def test_sin_bloques_despues_del_recalculo_usa_el_prefijo_de_la_referencia():
    # El recálculo puso 9 y la tarea se rearmó (sin solución) antes de minar: no vuelve al 6 del último bloque.
    a = AjustesFalsos(prefijo_base=12, segundos_objetivo=60)
    cadena = [bloque(0, 0, 100), bloque(1, 6, 117, duracion=17)]
    assert decidir_prefijo(2_000_000_000, cadena, a, referencia(2_000_000_000, 1, 9), ahora=10.0,
                           armada=5.0)[:2] == (9, "tiempo")


def test_bajada_de_emergencia_un_caracter_cada_240_s_sin_bloque():
    a = AjustesFalsos(prefijo_base=12, segundos_objetivo=60, emergencia=240)
    cadena = [bloque(0, 0, 100)] + [bloque(i, 9, 100 + i * 34, duracion=34) for i in range(1, 4)]
    ref = referencia(2_000_000_000, 0, 9, cuando=0.0)
    # La caja se armó en t=1000. A los 239 s todavía no; a los 240, 8; a los 480, 7; a los 720, 6.
    assert decidir_prefijo(2_000_000_000, cadena, a, ref, ahora=1239.0, armada=1000.0)[:2] == (9, "tiempo")
    assert decidir_prefijo(2_000_000_000, cadena, a, ref, ahora=1240.0, armada=1000.0)[:2] == (8, "emergencia")
    assert decidir_prefijo(2_000_000_000, cadena, a, ref, ahora=1480.0, armada=1000.0)[:2] == (7, "emergencia")
    assert decidir_prefijo(2_000_000_000, cadena, a, ref, ahora=1720.0, armada=1000.0)[:2] == (6, "emergencia")
    # Nunca menos de 1
    assert decidir_prefijo(2_000_000_000, cadena, a, ref, ahora=1000.0 + 240 * 50, armada=1000.0)[:2] == (1, "emergencia")


def test_la_emergencia_se_reinicia_con_cada_caja_nueva_y_con_el_recalculo():
    a = AjustesFalsos(prefijo_base=12, segundos_objetivo=60, emergencia=240)
    cadena = [bloque(0, 0, 100)] + [bloque(i, 8, 100 + i * 34, duracion=300) for i in range(1, 4)]
    # Salió el bloque 3 (la caja vieja) y la caja nueva se armó en t=2000: sin emergencia.
    assert decidir_prefijo(2_000_000_000, cadena, a, referencia(2_000_000_000, 0, 9), ahora=2010.0,
                           armada=2000.0)[:2] == (8, "tiempo")
    # Un recálculo por potencia en t=2100 corre el reloj: a t=2300 (300 s de caja) todavía no baja
    assert decidir_prefijo(2_000_000_000, cadena, a, referencia(2_000_000_000, 3, 8, cuando=2100.0), ahora=2300.0,
                           armada=2000.0)[:2] == (8, "tiempo")


def test_sin_emergencia_configurada_no_baja():
    a = AjustesFalsos(prefijo_base=12, emergencia=0)
    cadena = [bloque(0, 0, 100), bloque(1, 9, 134, duracion=34)]
    assert decidir_prefijo(2_000_000_000, cadena, a, referencia(2_000_000_000, 0, 9), ahora=99_999.0,
                           armada=0.0)[:2] == (9, "tiempo")


def test_una_referencia_de_otra_cadena_se_ignora():
    # La cadena se reinició: la referencia dice altura 40 y la punta es el génesis -> arranque.
    a = AjustesFalsos(prefijo_base=6)
    assert decidir_prefijo(2_000_000_000, [bloque(0, 0, 100)], a, referencia(2_000_000_000, 40, 3))[:2] == (6, "inicial")


def test_evento_potencia():
    assert evento_potencia(4_000_000, 2_000_000_000, hubo_bloque=False) == "baja"
    assert evento_potencia(0, 2_000_000_000, hubo_bloque=False) == "baja"
    assert evento_potencia(2_000_000_000, 4_000_000, hubo_bloque=True) == "sube"
    assert evento_potencia(2_000_000_000, 4_000_000, hubo_bloque=False) is None
    assert evento_potencia(1_000_000, 0, hubo_bloque=False) == "sube"
    assert evento_potencia(0, 0, hubo_bloque=True) is None
    # Justo ×4 y ÷4 (log16 = ±0,5) cuentan; ×3,9 no
    assert evento_potencia(4_000_000, 1_000_000, hubo_bloque=True) == "sube"
    assert evento_potencia(250_000, 1_000_000, hubo_bloque=True) == "baja"
    assert evento_potencia(3_900_000, 1_000_000, hubo_bloque=True) is None
