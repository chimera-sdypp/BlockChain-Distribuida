"""BC-74: un rango a la vez por minero, la potencia medida con los shares y el plazo de cada préstamo."""

import json
import time

import pytest
from prometheus_client import REGISTRY

from comun import contrato
from comun.tests.claves import CUENTA
from gateway import app as gateway
from gateway.tests.conftest import presentado
from gateway.tests.test_rangos import publicar_rangos, rango, sacar
from gateway.tests.test_vencidos import todos

MINERO = CUENTA["minero"]


@pytest.fixture(autouse=True)
def ya_presentados(redis):
    for nombre in ("minero", "minero2"):
        presentado(redis, nombre)  # bench de 1 MH/s


def aviso(tarea="t1", numero=0, **extra):
    return {"minero": MINERO, "hashes_por_segundo": 1_000_000, "rango": {"tarea": tarea, "rango": numero}, **extra}


# --- Un rango a la vez ---------------------------------------------------------------------------
def test_diez_pedidos_seguidos_dejan_un_solo_prestamo(cliente, redis, colas):
    publicar_rangos(redis, colas, *(rango("t1", n) for n in range(10)))

    respuestas = [cliente.get("/tasks/next") for _ in range(10)]

    assert {r.json()["rango"] for r in respuestas} == {0}  # siempre el mismo
    assert redis.zcard("prestamos") == 1 and redis.get(f"prestamo_de:{MINERO}") == "t1:0"
    assert len(todos(colas, "rangos")) == 9  # los otros siguen en la cola para los demás


def test_el_que_se_reinicio_recibe_el_mismo_rango_con_su_texto(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0), rango("t1", 1))
    primero = cliente.get("/tasks/next").json()
    assert cliente.get("/tasks/next").json() == primero


def test_al_devolverlo_recibe_otro(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0), rango("t1", 1))
    cliente.get("/tasks/next")
    cliente.post("/results", {"tarea": "t1", "rango": 0, "minero": MINERO, "sin_solucion": True})
    assert not redis.exists(f"prestamo_de:{MINERO}")
    assert cliente.get("/tasks/next").json()["rango"] == 1


def test_si_su_tarea_se_cerro_recibe_uno_de_la_nueva(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("vieja", 0))
    cliente.get("/tasks/next")
    redis.hset("tarea:vieja", "estado", "confirmada")
    publicar_rangos(redis, colas, rango("nueva", 0))
    assert cliente.get("/tasks/next").json()["tarea"] == "nueva"


def test_dos_mineros_tienen_un_prestamo_cada_uno(cliente, otro, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0), rango("t1", 1))
    assert {cliente.get("/tasks/next").json()["rango"], otro.get("/tasks/next").json()["rango"]} == {0, 1}


# --- El plazo -----------------------------------------------------------------------------------
def test_sin_medicion_el_plazo_sale_del_bench_y_no_pasa_de_120_s(armar, redis):
    g, _ = armar()
    corto = rango("t1", 0, hasta=10_000_000 - 1)  # 10 M nonces a 1 MH/s: 10 s → 3 × 10 + 10 = 40 s
    largo = rango("t1", 0, hasta=10**9 - 1)        # 1000 s → 3010 s, pero sin medición como mucho 120
    assert g.plazo(MINERO, corto) == pytest.approx(40)
    assert g.plazo(MINERO, largo) == gateway.PLAZO_SIN_MEDICION


def test_con_medicion_el_plazo_sale_de_lo_medido(armar, redis):
    g, _ = armar()
    redis.hset(f"nodo:{MINERO}", mapping={"medido": 100_000, "medido_estado": "medido"})
    assert g.plazo(MINERO, rango("t1", 0, hasta=10**9 - 1)) == pytest.approx(3 * 10_000 + 10)
    redis.hset(f"nodo:{MINERO}", mapping={"medido": 0, "medido_estado": "medido"})  # mide 0: no mina
    assert g.plazo(MINERO, rango("t1", 0)) == gateway.PLAZO_MINIMO


def test_al_pasarse_el_plazo_el_keepalive_corta_y_el_rango_vuelve(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0))
    cliente.get("/tasks/next")
    redis.hset("prestamo:t1:0", "plazo", time.time() - 1)  # se le pasó
    antes = REGISTRY.get_sample_value("blockchain_prestamos_vencidos_total", {"motivo": "plazo"}) or 0

    respuesta = cliente.post("/keepalive", aviso())

    assert respuesta.json()["cortar"] is True
    [devuelto] = todos(colas, "rangos")
    assert (devuelto["tarea"], devuelto["rango"]) == ("t1", 0)
    assert not redis.exists("prestamo:t1:0") and not redis.exists(f"prestamo_de:{MINERO}")
    assert REGISTRY.get_sample_value("blockchain_prestamos_vencidos_total", {"motivo": "plazo"}) == antes + 1


def test_dentro_del_plazo_el_keepalive_no_corta(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0))
    cliente.get("/tasks/next")
    assert float(redis.hget("prestamo:t1:0", "plazo")) > time.time()
    assert cliente.post("/keepalive", aviso()).json()["cortar"] is False
    assert redis.exists("prestamo:t1:0")


def test_el_que_retiene_un_rango_lo_pierde_al_pedir_otro_pasado_el_plazo(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0))
    cliente.get("/tasks/next")
    redis.hset("prestamo:t1:0", "plazo", time.time() - 1)
    respuesta = cliente.get("/tasks/next")  # el mismo rango volvió a la cola y se lo vuelve a llevar, con plazo nuevo
    assert respuesta.json()["rango"] == 0 and float(redis.hget("prestamo:t1:0", "plazo")) > time.time()


# --- La medición -------------------------------------------------------------------------------
def prestamo(desde, shares):
    return {"desde": str(desde), "shares": str(shares), "minero": MINERO}


def test_un_minero_honesto_mide_lo_que_hace(armar, redis):
    g, _ = armar()
    # Prefijo 6: un share cada 16^5 ≈ 1 M hashes. A 1 MH/s, en 10 s son ~10 shares: no llega al tope.
    ahora = 1_000_000.0
    for _ in range(3):
        g.medir(MINERO, prestamo(ahora - 10, 10), rango("t1", 0, prefijo="000000"), ahora=ahora)
    nodo = redis.hgetall(f"nodo:{MINERO}")
    assert nodo["medido_estado"] == "medido"  # 3 × 10 s × 1 MH/s / 16^5 ≈ 28 shares esperados ≥ 20
    assert int(nodo["medido"]) == pytest.approx(10 * 16**5 / 10, rel=0.01)  # 1,05 MH/s
    assert contrato.potencia_efectiva(nodo) == 1_000_000  # min(bench, medido): el bench, que es menos


def test_un_bench_inflado_queda_en_lo_que_mide(armar, redis):
    g, _ = armar()
    redis.hset(f"nodo:{MINERO}", "bench", 1_000_000_000)  # dice 1 GH/s
    ahora = 1_000_000.0
    for _ in range(3):  # pero manda los shares de 1 MH/s
        g.medir(MINERO, prestamo(ahora - 10, 10), rango("t1", 0, prefijo="000000"), ahora=ahora)
    nodo = redis.hgetall(f"nodo:{MINERO}")
    assert nodo["medido_estado"] == "medido"
    assert contrato.potencia_efectiva(nodo) == pytest.approx(16**5, rel=0.01)  # no 1e9


def test_una_gpu_falsa_que_no_manda_shares_mide_cero(armar, redis):
    g, _ = armar()
    redis.hset(f"nodo:{MINERO}", mapping={"tipo": "gpu", "bench": 2_000_000_000})
    g.medir(MINERO, prestamo(990, 0), rango("t1", 0, prefijo="000000"), ahora=1000)  # 10 s: ~19000 esperados
    nodo = redis.hgetall(f"nodo:{MINERO}")
    assert (nodo["medido_estado"], nodo["medido"]) == ("medido", "0")
    assert contrato.potencia_efectiva(nodo) == 0


def test_un_minero_rapido_que_llega_al_tope_es_una_cota(armar, redis):
    g, _ = armar()
    redis.hset(f"nodo:{MINERO}", "bench", 2_000_000_000)
    g.medir(MINERO, prestamo(999.6, 64), rango("t1", 0, prefijo="000000"), ahora=1000)
    nodo = redis.hgetall(f"nodo:{MINERO}")
    assert nodo["medido_estado"] == "cota"
    assert contrato.potencia_efectiva(nodo) == 2_000_000_000  # sólo se sabe que es al menos eso: queda el bench


def test_con_pocos_shares_esperados_no_se_decide(armar, redis):
    g, _ = armar()
    # Prefijo 9: un share cada 16^8 ≈ 4300 M hashes; a 1 MH/s en 60 s se espera 0,01: no se puede saber.
    g.medir(MINERO, prestamo(940, 0), rango("t1", 0, prefijo="0" * 9), ahora=1000)
    nodo = redis.hgetall(f"nodo:{MINERO}")
    assert nodo["medido_estado"] == "sin_datos" and contrato.potencia_efectiva(nodo) == 1_000_000


def test_la_medicion_guarda_los_ultimos_20(armar, redis):
    g, _ = armar()
    for i in range(25):
        g.medir(MINERO, prestamo(990, 5), rango("t1", i, prefijo="000000"), ahora=1000)
    assert redis.llen(f"medicion:{MINERO}") == contrato.MEDICION_PRESTAMOS


def test_al_devolver_un_rango_se_mide(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0))
    cliente.get("/tasks/next")
    cliente.post("/results", {"tarea": "t1", "rango": 0, "minero": MINERO, "sin_solucion": True})
    [registro] = [json.loads(r) for r in redis.lrange(f"medicion:{MINERO}", 0, -1)]
    assert registro["shares"] == 0 and registro["segundos"] >= 0.05
    assert redis.hget(f"nodo:{MINERO}", "medido_estado") in ("medido", "sin_datos")


def test_la_lista_y_las_metricas_muestran_lo_medido(armar, redis):
    g, cliente = armar()
    redis.hset(f"nodo:{MINERO}", mapping={"medido": 123_456, "medido_estado": "medido", "ultimo_aviso": int(time.time())})
    [m] = [m for m in cliente.cliente.get("/mineros").json() if m["id"] == MINERO]
    assert (m["medido"], m["medido_estado"], m["potencia"]) == (123_456, "medido", 123_456)
    assert REGISTRY.get_sample_value("blockchain_minero_medido", {"minero": "minero", "tipo": "cpu"}) == 123_456
    assert REGISTRY.get_sample_value("blockchain_minero_potencia", {"minero": "minero", "tipo": "cpu"}) == 123_456
