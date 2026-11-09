import pytest

from trp.particion import largo_prefijo, partir, potencia_viva


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
