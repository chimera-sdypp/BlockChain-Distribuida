import pytest

from escalador.decision import Ajustes, Decisor, Foto, necesarias, sacar_foto

AHORA = 1_800_000_000


def ajustes(**cambios):
    base = dict(prefijo_base=6, segundos_objetivo=30, maximo=3, hps_vm=1_000_000, espera_subir=30,
                espera_bajar=120, vivo_segundos=15, prefijo_vm="minero-cpu-")
    return Ajustes(**{**base, **cambios})


def nodo(tipo, bench, hace=1):
    return {"tipo": tipo, "bench": str(bench), "ultimo_aviso": str(AHORA - hace)}


def sin_gpu(ajena=0.0, trabajo=True, hps_vm=None):
    return Foto(gpus=0, ajena=ajena, hps_vm=hps_vm, trabajo=trabajo)


# --- La foto de nodo:* -----------------------------------------------------------------------
def test_foto_separa_gpus_vms_y_cpu_ajena():
    nodos = {"gpu-1": nodo("gpu", 2_000_000_000), "gpu-muerta": nodo("gpu", 2_000_000_000, hace=60),
             "minero-cpu-0": nodo("cpu", 800_000), "minero-cpu-1": nodo("cpu", 1_200_000),
             "pc-tomas": nodo("cpu", 5_000_000), "pc-vieja": nodo("cpu", 9_000_000, hace=15),
             "roto": {"tipo": "cpu", "bench": "", "ultimo_aviso": "x"}}
    foto = sacar_foto(nodos, AHORA, True, ajustes())
    assert foto == Foto(gpus=1, ajena=5_000_000, hps_vm=1_000_000, trabajo=True)


def test_foto_sin_vms_vivas_no_tiene_bench_de_vm():
    assert sacar_foto({"pc": nodo("cpu", 10)}, AHORA, False, ajustes()).hps_vm is None


# --- Cuántas hacen falta -----------------------------------------------------------------------
def test_con_gpu_o_sin_trabajo_no_hacen_falta():
    assert necesarias(Foto(gpus=1, ajena=0, hps_vm=None, trabajo=True), ajustes()) == 0
    assert necesarias(sin_gpu(trabajo=False), ajustes()) == 0


@pytest.mark.parametrize("prefijo, esperado", [(5, 1), (6, 1), (7, 3)])
def test_las_que_hacen_falta_para_el_prefijo_base(prefijo, esperado):
    # 16^5/30 ≈ 35 kh/s y 16^6/30 ≈ 559 kh/s: alcanza una VM de 1 Mh/s; 16^7/30 ≈ 8,9 Mh/s: el tope.
    assert necesarias(sin_gpu(), ajustes(prefijo_base=prefijo)) == esperado


def test_descuenta_la_cpu_ajena_y_usa_el_bench_de_las_vms():
    a = ajustes(prefijo_base=7, maximo=20)                  # objetivo ≈ 8,95 Mh/s
    assert necesarias(sin_gpu(), a) == 9
    assert necesarias(sin_gpu(ajena=5_000_000), a) == 4
    assert necesarias(sin_gpu(ajena=5_000_000, hps_vm=2_000_000), a) == 2
    assert necesarias(sin_gpu(ajena=10_000_000), a) == 0


# --- Sin oscilar ----------------------------------------------------------------------------
def test_espera_un_rato_sin_gpu_antes_de_crear():
    d = Decisor(ajustes(), actual=0)
    assert d.decidir(sin_gpu(), AHORA) == 0
    assert d.decidir(sin_gpu(), AHORA + 29) == 0
    assert d.decidir(sin_gpu(), AHORA + 30) == 1


def test_una_gpu_que_vuelve_un_momento_reinicia_la_espera_para_crear():
    d = Decisor(ajustes(), actual=0)
    d.decidir(sin_gpu(), AHORA)
    d.decidir(Foto(gpus=1, ajena=0, hps_vm=None, trabajo=True), AHORA + 20)
    assert d.decidir(sin_gpu(), AHORA + 40) == 0
    assert d.decidir(sin_gpu(), AHORA + 70) == 1


def test_borra_recien_cuando_el_menor_se_sostiene():
    d = Decisor(ajustes(), actual=1)
    con_gpu = Foto(gpus=1, ajena=0, hps_vm=None, trabajo=True)
    assert d.decidir(con_gpu, AHORA) == 1
    assert d.decidir(con_gpu, AHORA + 119) == 1
    assert d.decidir(con_gpu, AHORA + 120) == 0


def test_si_la_gpu_se_va_otra_vez_antes_de_la_espera_las_vms_siguen():
    d = Decisor(ajustes(), actual=1)
    d.decidir(Foto(gpus=1, ajena=0, hps_vm=None, trabajo=True), AHORA)
    assert d.decidir(sin_gpu(), AHORA + 60) == 1                 # vuelve a hacer falta: se olvida la baja
    d.decidir(Foto(gpus=1, ajena=0, hps_vm=None, trabajo=True), AHORA + 100)
    assert d.decidir(Foto(gpus=1, ajena=0, hps_vm=None, trabajo=True), AHORA + 200) == 1  # 100 s, no 120
    assert d.decidir(Foto(gpus=1, ajena=0, hps_vm=None, trabajo=True), AHORA + 220) == 0


def test_sin_trabajo_baja_a_cero_despues_de_la_espera():
    d = Decisor(ajustes(), actual=2)
    assert d.decidir(sin_gpu(trabajo=False), AHORA) == 2
    assert d.decidir(sin_gpu(trabajo=False), AHORA + 120) == 0


def test_sube_de_a_varias_si_hace_falta_mas():
    d = Decisor(ajustes(prefijo_base=7, maximo=20), actual=0)
    d.decidir(sin_gpu(ajena=5_000_000), AHORA)
    assert d.decidir(sin_gpu(ajena=5_000_000), AHORA + 30) == 4
    assert d.decidir(sin_gpu(), AHORA + 40) == 9                # se fue la otra CPU: sube enseguida
