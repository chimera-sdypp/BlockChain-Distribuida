"""BC-63: los shares que mandan los mineros, verificados uno por uno y contados por época."""

from functools import lru_cache

import pytest
from prometheus_client import REGISTRY

from comun import contrato
from comun.tests.claves import CUENTA
from gateway.tests.test_keepalive import aviso, ya_presentados  # noqa: F401 (fixture autouse)
from gateway.tests.test_rangos import TAREA, publicar_rangos, rango, sacar, texto

PREFIJO, HASTA = "000", 99_999  # shares con «00»: uno de cada 256 hashes, unos 390 en el rango


def shares_de(nombre="minero", desde=0, hasta=HASTA, prefijo=PREFIJO):
    return list(_shares(nombre, desde, hasta, prefijo))


@lru_cache
def _shares(nombre, desde, hasta, prefijo):
    t = texto(nombre=nombre, prefijo=prefijo)
    corto = contrato.prefijo_share(prefijo)
    return tuple(n for n in range(desde, hasta + 1) if contrato.cumple(contrato.hash_bloque(t, n), corto))


def prestar(cliente, redis, colas, prefijo=PREFIJO, numero=0, desde=0, hasta=HASTA):
    publicar_rangos(redis, colas, rango("t1", numero, desde=desde, hasta=hasta, prefijo=prefijo))
    assert cliente.get("/tasks/next").status_code == 200


def resultado(nombre="minero", **extra):
    return {"tarea": "t1", "rango": 0, "minero": CUENTA[nombre], "sin_solucion": True, **extra}


def epoca(redis, nombre="minero"):
    return redis.zscore(f"shares:{contrato.epoca(TAREA['altura'])}", CUENTA[nombre])


def test_los_shares_del_resultado_se_cuentan_en_la_epoca_y_en_el_historial(cliente, redis, colas):
    prestar(cliente, redis, colas)
    validos = shares_de()[:10]

    respuesta = cliente.post("/results", resultado(shares=validos))

    assert respuesta.json() == {"shares": 10}
    assert epoca(redis) == 10
    assert redis.hget(f"historial:{CUENTA['minero']}", "shares") == "10"
    assert "shares" not in sacar(colas, "resultados")  # al TrP le llega el resultado sin los shares


def test_los_shares_del_keepalive_tambien_cuentan(cliente, redis, colas):
    prestar(cliente, redis, colas)
    respuesta = cliente.post("/keepalive", aviso(shares=shares_de()[:3]))
    assert respuesta.json() == {"cortar": False, "shares": 3} and epoca(redis) == 3


def test_un_share_que_no_cumple_o_esta_fuera_del_rango_no_cuenta(cliente, redis, colas):
    prestar(cliente, redis, colas, desde=0, hasta=50_000)
    validos = set(shares_de(hasta=50_000))
    adentro, afuera = shares_de(hasta=50_000)[:2], shares_de(desde=50_001)[:2]
    malo = next(n for n in range(50_000) if n not in validos)

    assert cliente.post("/keepalive", aviso(shares=[malo, *afuera, *adentro])).json()["shares"] == 2


def test_un_share_repetido_cuenta_una_vez(cliente, redis, colas):
    prestar(cliente, redis, colas)
    uno = shares_de()[:1]
    cliente.post("/keepalive", aviso(shares=uno))
    assert cliente.post("/keepalive", aviso(shares=uno)).json()["shares"] == 0
    assert cliente.post("/results", resultado(shares=uno * 2)).json() == {"shares": 0}
    assert epoca(redis) == 1


def test_el_nonce_de_otro_minero_no_es_un_share(cliente, redis, colas):
    prestar(cliente, redis, colas)
    propios = set(shares_de())
    ajenos = [n for n in shares_de(nombre="minero2") if n not in propios][:5]
    assert cliente.post("/results", resultado(shares=ajenos)).json() == {"shares": 0}


def test_los_shares_de_un_rango_que_no_tiene_prestado_no_cuentan(cliente, otro, redis, colas):
    prestar(cliente, redis, colas)
    respuesta = otro.post("/results", resultado(nombre="minero2", shares=shares_de(nombre="minero2")[:5]))
    assert respuesta.json() == {"shares": 0} and epoca(redis, "minero2") is None


def test_con_la_tarea_cerrada_ya_no_entran(cliente, redis, colas):
    prestar(cliente, redis, colas)
    redis.hset("tarea:t1", "estado", "confirmada")  # el NCT ya puede estar pagando la época
    respuesta = cliente.post("/keepalive", aviso(shares=shares_de()[:5]))
    assert respuesta.json() == {"cortar": True, "shares": 0} and epoca(redis) is None


def test_no_pasa_del_tope_por_prestamo(cliente, redis, colas):
    prestar(cliente, redis, colas, prefijo="00")  # shares con «0»: uno de cada 16
    todos = shares_de(prefijo="00")
    assert len(todos) > 2 * contrato.SHARES_POR_RANGO
    cliente.post("/keepalive", aviso(shares=todos[:contrato.SHARES_POR_RANGO]))
    respuesta = cliente.post("/results", resultado(shares=todos[contrato.SHARES_POR_RANGO:2 * contrato.SHARES_POR_RANGO]))
    assert respuesta.json() == {"shares": 0} and epoca(redis) == contrato.SHARES_POR_RANGO


def test_con_prefijo_de_un_caracter_no_hay_shares(cliente, redis, colas):
    prestar(cliente, redis, colas, prefijo="0")
    assert cliente.post("/results", resultado(shares=[1, 2, 3])).json() == {"shares": 0}


@pytest.mark.parametrize("resultado_", ["aceptado", "descartado"])
def test_la_metrica_cuenta_los_shares(cliente, redis, colas, resultado_):
    prestar(cliente, redis, colas)
    antes = REGISTRY.get_sample_value("blockchain_shares_total", {"resultado": resultado_}) or 0
    validos = shares_de()[:4]
    cliente.post("/results", resultado(shares=validos + [validos[0] + 1]))
    despues = REGISTRY.get_sample_value("blockchain_shares_total", {"resultado": resultado_})
    assert despues - antes == (4 if resultado_ == "aceptado" else 1)
