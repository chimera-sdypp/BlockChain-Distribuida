"""BC-75: el minero que contesta `sin_solucion` sin buscar queda suspendido."""

import json

import pytest
from prometheus_client import REGISTRY

from comun import contrato
from comun.tests.claves import CUENTA
from gateway.tests.conftest import presentado
from gateway.tests.test_rangos import publicar_rangos, rango, sacar, texto
from gateway.tests.test_vencidos import todos

MINERO, OTRO = CUENTA["minero"], CUENTA["minero2"]


@pytest.fixture(autouse=True)
def ya_presentados(redis):
    for nombre in ("minero", "minero2"):
        presentado(redis, nombre)


def sin_solucion(numero=0, tarea="t1", cuenta=MINERO):
    return {"tarea": tarea, "rango": numero, "minero": cuenta, "sin_solucion": True}


def muestra(nombre, **etiquetas):
    return REGISTRY.get_sample_value(nombre, etiquetas) or 0


# --- Suspensión ---------------------------------------------------------------------------------
def test_un_suspendido_no_recibe_rangos_y_su_prestamo_vuelve_a_la_cola(armar, redis, colas):
    g, cliente = armar()
    publicar_rangos(redis, colas, rango("t1", 0))
    cliente.get("/tasks/next")

    assert g.suspender(MINERO, "estadistica", {"prueba": True}) is True

    [devuelto] = todos(colas, "rangos")
    assert devuelto["rango"] == 0 and not redis.exists(f"prestamo_de:{MINERO}")
    respuesta = cliente.get("/tasks/next")
    assert respuesta.status_code == 403 and "suspendida" in respuesta.json()["error"]
    assert g.suspender(MINERO, "estadistica", {}) is False  # ya estaba
    datos = json.loads(redis.hget("suspendidos", MINERO))
    assert (datos["nombre"], datos["motivo"], datos["evidencia"]) == ("minero", "estadistica", {"prueba": True})


def test_las_metricas_cuentan_los_suspendidos(armar, redis):
    g, _ = armar()
    g.suspender(MINERO, "estadistica", {})
    from gateway.app import COLECTOR  # noqa: F401 (registrado al importar)
    assert REGISTRY.get_sample_value("blockchain_mineros_suspendidos") == 1


# --- Chequeo estadístico ------------------------------------------------------------------------
def test_un_sin_solucion_imposible_suspende_y_el_rango_vuelve_a_la_cola(armar, redis, colas):
    g, cliente = armar(muestreo=0)
    # 100.000 nonces con prefijo «000» (uno cada 4096): P(sin solución) = e^-24 ≈ 2,5e-11
    publicar_rangos(redis, colas, rango("t1", 0, hasta=99_999, prefijo="000"))
    cliente.get("/tasks/next")
    antes = muestra("blockchain_suspensiones_total", motivo="estadistica")

    assert cliente.post("/results", sin_solucion()).status_code == 202

    assert redis.hexists("suspendidos", MINERO)
    evidencia = json.loads(redis.hget("suspendidos", MINERO))["evidencia"]
    assert evidencia["nonces"] == 100_000 and evidencia["probabilidad"] < 1e-6
    assert sacar(colas, "resultados") is None  # al TrP no le llega
    [devuelto] = todos(colas, "rangos")
    assert (devuelto["tarea"], devuelto["rango"]) == ("t1", 0)  # para que lo busque otro
    assert muestra("blockchain_suspensiones_total", motivo="estadistica") == antes + 1


def test_un_sin_solucion_posible_no_suspende(armar, redis, colas):
    g, cliente = armar(muestreo=0)
    # 1.000 nonces con prefijo «00» (uno cada 256): P = e^-3,9 ≈ 2 %: puede pasar
    publicar_rangos(redis, colas, rango("t1", 0, hasta=999, prefijo="00"))
    cliente.get("/tasks/next")
    cliente.post("/results", sin_solucion())
    assert not redis.exists("suspendidos")
    assert sacar(colas, "resultados")["sin_solucion"] is True


def test_un_minero_honesto_no_se_suspende_en_muchos_rangos(armar, redis, colas):
    # Rangos donde no tener solución es posible: nunca se suspende, aunque devuelva muchos sin solución.
    g, cliente = armar(muestreo=0)
    for n in range(20):
        publicar_rangos(redis, colas, rango("t1", n, hasta=499, prefijo="00"))
        cliente.get("/tasks/next")
        cliente.post("/results", sin_solucion(n))
    assert not redis.exists("suspendidos")


# --- Reverificación por muestreo -----------------------------------------------------------------
PREFIJO, HASTA = "000", 49_999  # P(sin solución) = e^-12 ≈ 5e-6: no alcanza para la estadística; ~12 soluciones


def nonce_en(cuenta, desde=0, hasta=HASTA, prefijo=PREFIJO):
    t = contrato.texto_para({**json.loads(json.dumps(texto_tarea())), "prefijo": prefijo}, cuenta)
    return next(n for n in range(desde, hasta + 1) if contrato.hash_bloque(t, n).startswith(prefijo)), t


def texto_tarea():
    from gateway.tests.test_rangos import TAREA
    return TAREA


@pytest.fixture
def reverificando(armar, redis, colas):
    """El minero devuelve sin solución un rango que sí tiene, y se sortea la reverificación (muestreo 100 %)."""
    g, cliente = armar(muestreo=1, azar=lambda: 0.0)
    _, otro = armar("minero2", muestreo=1, azar=lambda: 0.0)
    publicar_rangos(redis, colas, rango("t1", 3, hasta=HASTA, prefijo=PREFIJO))
    cliente.get("/tasks/next")
    cliente.post("/results", sin_solucion(3))
    return g, cliente, otro


def test_se_pide_la_reverificacion_con_el_texto_del_primero(reverificando, redis, colas):
    g, cliente, otro = reverificando
    assert not redis.exists("suspendidos")
    assert sacar(colas, "resultados")["sin_solucion"] is True  # el TrP no espera la reverificación

    prestado = otro.get("/tasks/next").json()

    assert prestado["rango"] == 3 + contrato.REVERIFICACION
    assert prestado["bloque"] == texto(prefijo=PREFIJO)  # el texto del primero, con su coinbase
    assert "reverifica" not in prestado and "rango_original" not in prestado
    assert redis.hget(f"prestamo:t1:{3 + contrato.REVERIFICACION}", "texto_de") == MINERO


def test_si_el_otro_encuentra_un_nonce_el_primero_mintio_y_el_bloque_no_se_usa(reverificando, redis, colas):
    g, cliente, otro = reverificando
    sacar(colas, "resultados")
    otro.get("/tasks/next")
    nonce, t = nonce_en(MINERO)
    antes = muestra("blockchain_reverificaciones_total", resultado="mentira")

    otro.post("/results", {"tarea": "t1", "rango": 3 + contrato.REVERIFICACION, "minero": OTRO,
                           "nonce": nonce, "hash": contrato.hash_bloque(t, nonce)})

    suspension = json.loads(redis.hget("suspendidos", MINERO))
    assert suspension["motivo"] == "reverificacion"
    assert (suspension["evidencia"]["nonce"], suspension["evidencia"]["rango"]) == (nonce, 3)
    assert suspension["evidencia"]["verificador"] == OTRO
    assert sacar(colas, "resultados") is None  # el bloque tiene la coinbase del que mintió: no va
    assert not redis.hexists("suspendidos", OTRO)
    assert muestra("blockchain_reverificaciones_total", resultado="mentira") == antes + 1


def test_si_el_otro_tampoco_encuentra_no_se_concluye_nada(reverificando, redis, colas):
    g, cliente, otro = reverificando
    sacar(colas, "resultados")
    otro.get("/tasks/next")
    otro.post("/results", sin_solucion(3 + contrato.REVERIFICACION, cuenta=OTRO))
    assert not redis.exists("suspendidos") and sacar(colas, "resultados") is None


def test_los_shares_del_verificador_se_cuentan_con_el_texto_del_primero(reverificando, redis, colas):
    g, cliente, otro = reverificando
    otro.get("/tasks/next")
    t = contrato.texto_para({**texto_tarea(), "prefijo": PREFIJO}, MINERO)
    shares = [n for n in range(2000) if contrato.hash_bloque(t, n).startswith("00")][:3]
    respuesta = otro.post("/keepalive", {"minero": OTRO, "hashes_por_segundo": 1, "shares": shares,
                                         "rango": {"tarea": "t1", "rango": 3 + contrato.REVERIFICACION}})
    assert respuesta.json()["shares"] == 3
    assert redis.hget(f"historial:{OTRO}", "shares") == "3"  # se le acreditan al verificador


def test_el_mismo_minero_no_se_reverifica_a_si_mismo(reverificando, redis, colas):
    g, cliente, otro = reverificando
    antes = muestra("blockchain_reverificaciones_total", resultado="descartada")
    assert cliente.get("/tasks/next").status_code == 204  # la tomó y la descartó
    assert muestra("blockchain_reverificaciones_total", resultado="descartada") == antes + 1


def test_con_la_tarea_cerrada_la_reverificacion_se_cancela(reverificando, redis, colas):
    g, cliente, otro = reverificando
    redis.hset("tarea:t1", "estado", "confirmada")
    assert otro.get("/tasks/next").status_code == 204


def test_sin_sortear_no_se_reverifica(armar, redis, colas):
    g, cliente = armar(muestreo=0.05, azar=lambda: 0.5)
    publicar_rangos(redis, colas, rango("t1", 0, hasta=HASTA, prefijo=PREFIJO))
    cliente.get("/tasks/next")
    cliente.post("/results", sin_solucion())
    assert todos(colas, "rangos") == []


def test_con_probabilidad_infima_no_se_muestrea_se_suspende(armar, redis, colas):
    g, cliente = armar(muestreo=1, azar=lambda: 0.0)
    publicar_rangos(redis, colas, rango("t1", 0, hasta=99_999, prefijo="000"))
    cliente.get("/tasks/next")
    cliente.post("/results", sin_solucion())
    assert json.loads(redis.hget("suspendidos", MINERO))["motivo"] == "estadistica"
    [devuelto] = todos(colas, "rangos")
    assert "reverifica" not in devuelto  # el rango normal, no una reverificación


# --- Aviso por pocos shares ---------------------------------------------------------------------
def test_tres_prestamos_seguidos_con_muchos_menos_shares_avisan_sin_suspender(armar, redis):
    g, _ = armar()
    antes = muestra("blockchain_pocos_shares_total")
    for _ in range(3):  # 10 s a 1 MH/s con prefijo 6: ~10 shares esperados; manda 1
        g.medir(MINERO, {"desde": "990", "shares": "1"}, rango("t1", 0, prefijo="000000"), ahora=1000)
    assert muestra("blockchain_pocos_shares_total") == antes + 1
    assert not redis.exists("suspendidos")
