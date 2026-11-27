"""Ticket 03 y BC-62: GET /tasks/next con el texto de cada minero y POST /results, con el préstamo de punta a punta."""

import json
import time

import redis as redislib
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from comun import contrato, web
from comun.tests.claves import CUENTA
from gateway import app as gateway
from gateway.tests.conftest import VECTOR, Minero

TAREA = VECTOR["tarea"]


def rango(tarea, numero, desde=0, hasta=999, prefijo="00"):
    return {"tarea": tarea, "rango": numero, "desde": desde, "hasta": hasta, "prefijo": prefijo}


def guardar_tarea(redis, tarea, estado="en_curso", prefijo="00", rangos=10):
    """tarea:<id> como la dejan el NCT (los datos del bloque) y el TrP (el prefijo y cuántos rangos)."""
    redis.hset(f"tarea:{tarea}", mapping={
        "previous_hash": TAREA["previous_hash"], "timestamp": TAREA["timestamp"], "altura": TAREA["altura"],
        "transacciones": json.dumps(TAREA["transacciones"]), "prefijo": prefijo, "estado": estado,
        "raiz_estado": TAREA["raiz_estado"], "rangos": rangos})


def publicar_rangos(redis, colas, *rangos, estado="en_curso"):
    rabbit, nombres = colas
    for r in rangos:
        guardar_tarea(redis, r["tarea"], estado, r["prefijo"])
        rabbit.publicar(nombres["rangos"], r)


def sacar(colas, cola):
    rabbit, nombres = colas
    return rabbit.tomar(nombres[cola], lambda m: m)


def texto(tarea="t1", nombre="minero", prefijo="00"):
    return contrato.texto_para({**TAREA, "prefijo": prefijo}, CUENTA[nombre])


def test_sin_trabajo_da_204(cliente):
    assert cliente.get("/tasks/next").status_code == 204


def test_entrega_el_rango_con_el_texto_del_minero_y_lo_anota_como_prestamo(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0))
    antes = time.time()

    respuesta = cliente.get("/tasks/next")

    # con la altura del bloque, para que el minero sepa de qué época son sus shares (BC-82)
    assert respuesta.status_code == 200 and respuesta.json() == {**rango("t1", 0), "bloque": texto(), "altura": 1}
    vence = redis.zscore("prestamos", "t1:0")
    assert antes + 29 <= vence <= time.time() + 31
    assert redis.hget("prestamo:t1:0", "minero") == CUENTA["minero"]
    assert json.loads(redis.hget("prestamo:t1:0", "mensaje")) == rango("t1", 0)  # sin texto: vuelve a la cola así
    assert sacar(colas, "rangos") is None  # se le hizo ack: no queda en la cola


def test_el_texto_es_el_del_vector_de_prueba(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0, prefijo=TAREA["prefijo"]))
    bloque = cliente.get("/tasks/next").json()["bloque"]
    assert bloque == VECTOR["texto"]
    assert contrato.hash_bloque(bloque, VECTOR["nonce"]) == VECTOR["block_hash"]


def test_dos_mineros_con_la_misma_tarea_reciben_textos_distintos(cliente, otro, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0), rango("t1", 1))
    uno, dos = cliente.get("/tasks/next").json(), otro.get("/tasks/next").json()
    assert uno["bloque"] == texto() and dos["bloque"] == texto(nombre="minero2") and uno["bloque"] != dos["bloque"]


def test_descarta_los_rangos_de_tareas_cerradas(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("vieja", 0), rango("vieja", 1), estado="confirmada")
    publicar_rangos(redis, colas, rango("nueva", 0))

    respuesta = cliente.get("/tasks/next")

    assert respuesta.json()["tarea"] == "nueva"
    assert not redis.exists("prestamo:vieja:0") and redis.zcard("prestamos") == 1


def test_cuenta_los_rangos_entregados(cliente, redis, colas):
    antes = REGISTRY.get_sample_value("blockchain_tareas_entregadas_total") or 0
    publicar_rangos(redis, colas, rango("t1", 0))
    cliente.get("/tasks/next")
    assert REGISTRY.get_sample_value("blockchain_tareas_entregadas_total") == antes + 1


def test_si_redis_no_responde_da_503_y_el_rango_queda_en_la_cola(redis, colas):
    rabbit, nombres = colas
    publicar_rangos(redis, colas, rango("t1", 0))
    caido = redislib.Redis(port=1, socket_connect_timeout=0.2, decode_responses=True)
    g = gateway.Gateway(caido, rabbit, cola_rangos=nombres["rangos"], cola_resultados=nombres["resultados"])
    app = web.crear_app("gateway", {})
    gateway.agregar_rutas(app, g)

    respuesta = Minero(TestClient(app), "minero").get("/tasks/next")

    assert respuesta.status_code == 503
    assert sacar(colas, "rangos") == rango("t1", 0)


def resultado_de(tarea="t1", numero=3, nombre="minero", **extra):
    return {"tarea": tarea, "rango": numero, "minero": CUENTA[nombre], "nonce": 42, "hash": "00ab" + "0" * 28, **extra}


def test_un_resultado_llega_a_resultados_y_libera_el_prestamo(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 3))
    cliente.get("/tasks/next")

    respuesta = cliente.post("/results", resultado_de())

    assert respuesta.status_code == 202 and respuesta.json() == {"shares": 0}
    assert sacar(colas, "resultados") == resultado_de()
    assert not redis.exists("prestamo:t1:3") and redis.zscore("prestamos", "t1:3") is None


def test_un_rango_sin_solucion_tambien_llega(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0))
    cliente.get("/tasks/next")
    resultado = {"tarea": "t1", "rango": 0, "minero": CUENTA["minero"], "sin_solucion": True}
    assert cliente.post("/results", resultado).status_code == 202
    assert sacar(colas, "resultados") == resultado


# --- Rangos ajenos (BC-92) -------------------------------------------------------------------------
def sin_solucion(numero, tarea="t1", nombre="minero"):
    return {"tarea": tarea, "rango": numero, "minero": CUENTA[nombre], "sin_solucion": True}


def test_un_sin_solucion_de_un_rango_ajeno_da_409_y_no_llega_al_trp(cliente, otro, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0), rango("t1", 1))
    cliente.get("/tasks/next")  # minero tiene el 0
    otro.get("/tasks/next")     # minero2 tiene el 1
    antes = REGISTRY.get_sample_value("blockchain_resultados_ajenos_total") or 0

    # El ataque del profesor: pedir un rango para saber el id de la tarea y devolver «sin solución» de los demás.
    codigos = [cliente.post("/results", sin_solucion(n)).status_code for n in range(1, 10)]

    assert codigos == [409] * 9
    assert sacar(colas, "resultados") is None
    assert REGISTRY.get_sample_value("blockchain_resultados_ajenos_total") == antes + 9
    assert redis.hget(f"historial:{CUENTA['minero']}", "resultados_ajenos") == "9"
    assert redis.hget("prestamo:t1:1", "minero") == CUENTA["minero2"]  # el préstamo del otro sigue igual
    assert redis.exists("prestamo:t1:0")  # y el propio también: lo puede devolver bien
    assert cliente.post("/results", sin_solucion(0)).status_code == 202
    assert sacar(colas, "resultados") == sin_solucion(0)
    assert not redis.exists("suspendidos") or not redis.hexists("suspendidos", CUENTA["minero"])


def test_un_sin_solucion_de_un_prestamo_vencido_da_409(cliente, redis, colas):
    # A un minero honesto le pasa si su préstamo venció: no llega, pero tampoco se lo suspende.
    publicar_rangos(redis, colas, rango("t1", 0))
    cliente.get("/tasks/next")
    redis.delete("prestamo:t1:0")
    assert cliente.post("/results", sin_solucion(0)).status_code == 409
    assert sacar(colas, "resultados") is None
    assert not redis.hexists("suspendidos", CUENTA["minero"])


def test_una_solucion_de_un_rango_ajeno_pasa_porque_el_trp_verifica_el_hash(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 3))
    assert cliente.post("/results", resultado_de()).status_code == 202
    assert sacar(colas, "resultados") == resultado_de()


def test_una_reverificacion_ajena_da_409(cliente, redis, colas):
    guardar_tarea(redis, "t1")
    numero = contrato.REVERIFICACION + 3
    assert cliente.post("/results", resultado_de(numero=numero)).status_code == 409
    assert sacar(colas, "resultados") is None


def test_un_rango_que_la_tarea_no_tiene_da_400(cliente, redis, colas):
    guardar_tarea(redis, "t1", rangos=10)
    for numero in (10, 1000, 1009, contrato.REVERIFICACION + 10):
        assert cliente.post("/results", sin_solucion(numero)).status_code == 400, numero
        assert cliente.post("/results", resultado_de(numero=numero)).status_code == 400, numero
    assert sacar(colas, "resultados") is None


def test_no_se_puede_mandar_un_resultado_a_nombre_de_otro(cliente, colas):
    assert cliente.post("/results", resultado_de(nombre="minero2")).status_code == 400
    assert sacar(colas, "resultados") is None


def test_un_resultado_mal_formado_da_400(cliente, colas):
    for malo in ({"tarea": "t1", "rango": 0, "minero": CUENTA["minero"]},
                 {"tarea": "t1", "rango": 0, "minero": "c:1", "nonce": 1, "hash": "x"},
                 {"tarea": "t1", "rango": "0", "minero": CUENTA["minero"], "sin_solucion": True},
                 resultado_de(shares="todos"), resultado_de(shares=list(range(65)))):
        assert cliente.post("/results", malo).status_code == 400, malo
    assert sacar(colas, "resultados") is None


# --- Rangos por minero (BC-59) ---------------------------------------------------------------------
def test_cada_prestamo_suma_en_el_historial_del_minero(cliente, otro, redis, colas):
    publicar_rangos(redis, colas, rango("t1", 0), rango("t1", 1), rango("t1", 2))
    primero = cliente.get("/tasks/next").json()
    # Uno a la vez (BC-74): devuelve el primero y recién ahí recibe otro.
    cliente.post("/results", {"tarea": "t1", "rango": primero["rango"], "minero": CUENTA["minero"], "sin_solucion": True})
    cliente.get("/tasks/next")
    otro.get("/tasks/next")
    assert redis.hget(f"historial:{CUENTA['minero']}", "rangos") == "2"
    assert redis.hget(f"historial:{CUENTA['minero2']}", "rangos") == "1"


def test_los_rangos_descartados_y_el_bench_no_suman(cliente, redis, colas):
    publicar_rangos(redis, colas, rango("vieja", 0), estado="confirmada")
    assert cliente.get("/tasks/next").status_code == 204
    cliente.post("/hello", {"minero": CUENTA["minero"], "tipo": "cpu", "placa": "x"})  # le toca la tarea modelo
    cliente.post("/bench", {"minero": CUENTA["minero"], "segundos": 15, "hashes_por_segundo": 1_100_000})
    assert redis.hget(f"historial:{CUENTA['minero']}", "rangos") is None


def test_las_metricas_cuentan_los_rangos_por_minero_con_su_alias(armar, redis):
    armar()
    redis.hset("nodo:" + "a" * 64, mapping={"tipo": "gpu", "alias": "gpu-tomas"})
    redis.hset("historial:" + "a" * 64, "rangos", 7)
    redis.hset("nodo:" + "b" * 64, mapping={"tipo": "cpu"})  # sin alias: el principio de la cuenta
    redis.hset("historial:" + "b" * 64, "rangos", 3)
    redis.hset("nodo:" + "c" * 64, mapping={"tipo": "cpu", "alias": "nuevo"})  # todavía sin rangos

    muestra = lambda minero, tipo: REGISTRY.get_sample_value(  # noqa: E731
        "blockchain_minero_rangos_total", {"minero": minero, "tipo": tipo})
    assert muestra("gpu-tomas", "gpu") == 7
    assert muestra("b" * 8, "cpu") == 3
    assert muestra("nuevo", "cpu") == 0
