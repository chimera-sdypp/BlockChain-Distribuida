"""BC-76: el minero tramposo hace lo que dice cada trampa (el sistema lo prueban gateway y pilar3/pruebas)."""

import pytest

from comun import contrato
from comun.tests.claves import CUENTA, PRIVADA
from minero_cpu.minero import Gateway
from minero_cpu.tests.test_minero import VECTOR, GatewayFalso, rango
from minero_cpu.trampas import MineroTramposo

PREFIJO = VECTOR["tarea"]["prefijo"]


@pytest.fixture
def armar():
    creados = []

    def _armar(trampa, rangos=(), bench=None):
        falso = GatewayFalso(rangos, bench)
        minero = MineroTramposo(Gateway(falso.url, PRIVADA["minero"]), "trampa", procesos=2, espera=0.1, latido=0.2,
                                trampa=trampa)
        creados.append((falso, minero))
        return falso, minero

    yield _armar
    for falso, minero in creados:
        minero.detener()
        falso.cerrar()


def test_una_trampa_desconocida_no_arranca():
    with pytest.raises(ValueError, match="TRAMPA"):
        MineroTramposo(Gateway("http://x", PRIVADA["minero"]), "t", procesos=1, trampa="otra")


@pytest.mark.parametrize("trampa, factor", [("bench-alto", 1000), ("bench-bajo", 0.01)])
def test_declara_otro_bench(armar, trampa, factor):
    falso, minero = armar(trampa, bench=dict(contrato.BENCH, hasta=99_999))
    minero.presentarse()
    declarado = falso.de("/bench")[0]["hashes_por_segundo"]
    assert declarado > 1 and (declarado > 10_000_000 if factor > 1 else declarado < 100_000)


def test_gpu_falsa_dice_ser_gpu_y_nunca_pide_rangos(armar):
    falso, minero = armar("gpu-falsa", rangos=[rango(0, 0, 200_000, PREFIJO)], bench=dict(contrato.BENCH, hasta=9_999))
    minero.presentarse()
    assert falso.de("/hello")[0]["tipo"] == "gpu"
    assert falso.de("/bench")[0]["hashes_por_segundo"] == 2_000_000_000
    assert minero.un_rango() is False and falso.de("/tasks/next") == []


def test_sin_solucion_contesta_sin_buscar(armar):
    falso, minero = armar("sin-solucion", rangos=[rango(0, 0, 200_000, PREFIJO)])
    assert minero.un_rango() is True
    assert falso.de("/results") == [{"tarea": "t1", "rango": 0, "minero": CUENTA["minero"], "sin_solucion": True}]
    assert minero.buscador.probados == 0


def test_guardar_encuentra_el_nonce_y_contesta_sin_solucion(armar):
    falso, minero = armar("guardar", rangos=[rango(0, 0, 200_000, PREFIJO)])
    assert minero.un_rango() is True
    [resultado] = falso.de("/results")
    assert resultado["sin_solucion"] is True and "nonce" not in resultado
    assert minero.buscador.probados > VECTOR["nonce"]  # buscó de verdad


def test_retener_manda_keepalive_sin_minar_hasta_que_se_lo_sacan(armar):
    falso, minero = armar("retener", rangos=[rango(0, 0, 200_000, PREFIJO)])
    falso.cortar = True  # el gateway se lo saca en el primer keep-alive
    assert minero.un_rango() is True
    assert falso.de("/keepalive")[0]["rango"] == {"tarea": "t1", "rango": 0}
    assert falso.de("/results") == [] and minero.buscador.probados == 0


def test_acaparar_pide_diez(armar):
    falso, minero = armar("acaparar", rangos=[rango(n, 0, 1000, PREFIJO) for n in range(10)])
    falso.cortar = True
    minero.un_rango()
    assert len(falso.de("/tasks/next")) == 10
