import json
import os
import sys
import threading
from pathlib import Path

import pytest

from comun import contrato
from minero_cpu.minero import Gateway, Minero
from minero_cpu.tests.test_minero import TOKEN, GatewayFalso
from minero_gpu.busqueda import BuscadorGPU, ErrorGPU

VECTOR = json.loads((Path(__file__).parents[2] / "comun" / "vector_prueba.json").read_text())
FALSO = Path(__file__).with_name("minar_falso.py")


def menor_nonce(bloque, desde, hasta, prefijo):
    return next((n for n in range(desde, hasta + 1) if contrato.cumple(contrato.hash_bloque(bloque, n), prefijo)), None)


@pytest.fixture
def falso(tmp_path):
    # Un ejecutable que corre el binario falso con este mismo Python.
    lanzador = tmp_path / "minar"
    lanzador.write_text(f"#!/bin/sh\nexec {sys.executable} {FALSO}\n")
    lanzador.chmod(0o755)
    buscador = BuscadorGPU(str(lanzador), trozo=1000)
    yield buscador
    buscador.cerrar()


def test_arranca_y_lee_la_placa(falso):
    assert falso.placa == "GPU de mentira"


def test_el_menor_nonce_aunque_cruce_trozos(falso):
    bloque, prefijo = "prueba;" * 40, "abc"   # 280 bytes: más de lo que aceptaba el Hit 7
    esperado = menor_nonce(bloque, 2500, 30_000, prefijo)
    assert esperado is not None and esperado > 3499  # cae varios trozos después del primero
    assert falso.buscar(bloque, 2500, 30_000, prefijo) == (esperado, False)
    assert falso.probados == esperado - 2500 + 1


def test_vector_de_prueba(falso):
    assert falso.buscar(VECTOR["texto"], 0, 200_000, VECTOR["prefijo"]) == (VECTOR["nonce"], False)


def test_sin_solucion_recorre_todos_los_trozos(falso):
    assert falso.buscar("x", 0, 4999, "f" * 32) == (None, False)
    assert falso.probados == 5000


def test_cortar_entre_trozos(falso):
    threading.Timer(0.2, falso.cortar).start()
    assert falso.buscar("x", 0, 10**9, "f" * 32) == (None, True)
    assert falso.buscar(VECTOR["texto"], 0, 2000, VECTOR["prefijo"]) == (VECTOR["nonce"], False)


def test_si_el_binario_muere_avisa(falso):
    with pytest.raises(ErrorGPU):
        falso.buscar("x", 0, 10, "muere")


def test_binario_que_no_arranca(tmp_path):
    roto = tmp_path / "minar"
    roto.write_text("#!/bin/sh\necho 'ERROR CUDA no CUDA-capable device is detected'\n")
    roto.chmod(0o755)
    with pytest.raises(ErrorGPU, match="no arrancó"):
        BuscadorGPU(str(roto))


def test_se_presenta_como_gpu_y_devuelve_el_nonce(falso):
    gateway = GatewayFalso([{"tarea": "t1", "rango": 0, "bloque": VECTOR["texto"], "desde": 0, "hasta": 4999,
                             "prefijo": VECTOR["prefijo"]}], bench=dict(contrato.BENCH, hasta=2999))
    minero = Minero(Gateway(gateway.url, TOKEN), "cpu-prueba", buscador=falso, tipo="gpu", placa=falso.placa)
    try:
        minero.presentarse()
        assert gateway.de("/hello")[0] == {"minero": "cpu-prueba", "tipo": "gpu", "placa": "GPU de mentira"}
        assert gateway.de("/bench")[0]["hashes_por_segundo"] > 0
        assert minero.un_rango()
        assert gateway.de("/results")[0] == {"tarea": "t1", "rango": 0, "minero": "cpu-prueba",
                                             "nonce": VECTOR["nonce"], "hash": VECTOR["block_hash"]}
    finally:
        minero.detenido.set()
        gateway.cerrar()


# --- Con el binario CUDA de verdad (MINAR=ruta/al/minar) ------------------------------------
@pytest.mark.skipif(not os.environ.get("MINAR"), reason="MINAR=<binario compilado> para correrlo en una GPU")
@pytest.mark.parametrize("largo", [0, 1, 55, 56, 63, 64, 120, 121, 1000, 6000])
def test_gpu_de_verdad_coincide_con_python(largo):
    buscador = BuscadorGPU(os.environ["MINAR"])
    try:
        bloque = ("tx;" * 3000)[:largo]
        for desde, hasta in ((0, 5000), (10**12, 10**12 + 5000)):
            assert buscador.buscar(bloque, desde, hasta, "00") == (menor_nonce(bloque, desde, hasta, "00"), False)
    finally:
        buscador.cerrar()
