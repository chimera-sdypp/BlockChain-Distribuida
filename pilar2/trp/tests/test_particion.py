import pytest

from trp.particion import CRITERIOS, largo_prefijo, partir, potencia_viva


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
