"""Ticket 02 y BC-57: pedidos firmados, mineros aprobados, /hello, /bench y las métricas de mineros."""

import json
import time

import pytest
from prometheus_client import REGISTRY

from comun import contrato
from comun.tests.claves import CUENTA
from gateway.tests.conftest import aprobar


def hola(cuenta=None):
    return {"minero": cuenta or CUENTA["minero"], "tipo": "gpu", "placa": "RTX 4060"}


def test_sin_firma_da_401(cliente):
    respuesta = cliente.cliente.post("/hello", json=hola())
    assert respuesta.status_code == 401 and "X-Minero" in respuesta.json()["error"]


def test_con_la_firma_de_otra_clave_da_401(cliente, otro):
    cuerpo = json.dumps(hola()).encode()
    firma = contrato.firmar_pedido(otro.privada, "POST", "/hello", cuerpo)
    firma["X-Minero"] = CUENTA["minero"]  # dice ser «minero», pero firma con la clave de «minero2»
    assert cliente.cliente.post("/hello", content=cuerpo, headers=firma).status_code == 401


def test_un_cuerpo_cambiado_despues_de_firmar_da_401(cliente):
    cuerpo = json.dumps(hola()).encode()
    firma = contrato.firmar_pedido(cliente.privada, "POST", "/hello", cuerpo)
    otro = json.dumps({**hola(), "tipo": "cpu"}).encode()
    assert cliente.cliente.post("/hello", content=otro, headers=firma).status_code == 401


def test_un_pedido_grabado_y_mandado_40_segundos_despues_da_401(cliente):
    respuesta = cliente.post("/hello", hola(), tiempo=int(time.time()) - 40)
    assert respuesta.status_code == 401 and "X-Tiempo" in respuesta.json()["error"]


def test_un_minero_sin_aprobar_da_403(armar, redis):
    _, cliente = armar("intruso")
    respuesta = cliente.post("/hello", hola(CUENTA["intruso"]))
    assert respuesta.status_code == 403 and "aprobar" in respuesta.json()["error"]
    aprobar(redis, "intruso", tipo="usuario")  # un usuario no es un minero
    assert cliente.post("/hello", hola(CUENTA["intruso"])).status_code == 403


def test_un_minero_sin_aprobar_queda_pedido_con_su_alias(armar, redis):
    """BC-58: la firma ya demuestra que tiene la privada, así que el organizador lo ve en el panel."""
    _, cliente = armar("intruso")
    assert cliente.post("/hello", {**hola(CUENTA["intruso"]), "alias": "gpu-intruso"}).status_code == 403
    pedido = json.loads(redis.hget("altas:pendientes", CUENTA["intruso"]))
    assert pedido["tipo"] == "minero" and pedido["nombre"] == "gpu-intruso"
    assert cliente.get("/tasks/next").status_code == 403  # reintentar no lo duplica ni le cambia el alias
    assert redis.hlen("altas:pendientes") == 1
    assert json.loads(redis.hget("altas:pendientes", CUENTA["intruso"]))["nombre"] == "gpu-intruso"


def test_el_hello_le_pone_el_alias_a_un_pedido_que_anoto_un_keepalive(armar, redis):
    """BC-71: con la cadena reiniciada, el primer pedido del minero puede ser un keep-alive, que no trae alias."""
    _, cliente = armar("intruso")
    aviso = {"minero": CUENTA["intruso"], "hashes_por_segundo": 1, "rango": None}
    assert cliente.post("/keepalive", aviso).status_code == 403
    assert json.loads(redis.hget("altas:pendientes", CUENTA["intruso"]))["nombre"] == CUENTA["intruso"][:12]
    pedida = json.loads(redis.hget("altas:pendientes", CUENTA["intruso"]))["pedida"]

    assert cliente.post("/hello", {**hola(CUENTA["intruso"]), "alias": "minero-gpu-0"}).status_code == 403

    pedido = json.loads(redis.hget("altas:pendientes", CUENTA["intruso"]))
    assert (pedido["nombre"], pedido["tipo"], pedido["pedida"]) == ("minero-gpu-0", "minero", pedida)


def test_un_minero_sin_alias_queda_pedido_con_el_principio_de_la_cuenta(armar, redis):
    _, cliente = armar("intruso")
    cliente.post("/hello", {**hola(CUENTA["intruso"]), "alias": "no vale"})
    assert json.loads(redis.hget("altas:pendientes", CUENTA["intruso"]))["nombre"] == CUENTA["intruso"][:12]


def test_una_firma_mala_no_deja_pedido(cliente, otro, redis):
    cuerpo = json.dumps(hola()).encode()
    firma = {**contrato.firmar_pedido(otro.privada, "POST", "/hello", cuerpo), "X-Minero": CUENTA["intruso"]}
    assert cliente.cliente.post("/hello", content=cuerpo, headers=firma).status_code == 401
    assert not redis.exists("altas:pendientes")


def test_el_minero_del_cuerpo_tiene_que_ser_el_que_firma(cliente):
    assert cliente.post("/hello", hola(CUENTA["minero2"])).status_code == 400


def test_health_y_metrics_no_piden_firma(cliente):
    assert cliente.cliente.get("/health").status_code == 200
    assert cliente.cliente.get("/metrics").status_code == 200


def test_un_minero_nuevo_recibe_la_tarea_modelo(cliente, redis):
    respuesta = cliente.post("/hello", hola())

    assert respuesta.status_code == 200
    assert respuesta.json() == contrato.BENCH
    nodo = redis.hgetall(f"nodo:{CUENTA['minero']}")
    # El alias sale de `cuentas`, que escribió el organizador, no de lo que diga el minero.
    assert (nodo["tipo"], nodo["placa"], nodo["alias"]) == ("gpu", "RTX 4060", "minero")
    assert abs(int(nodo["ultimo_aviso"]) - time.time()) < 5


def test_despues_del_bench_queda_registrado(cliente, redis):
    cliente.post("/hello", hola())

    respuesta = cliente.post("/bench", {"minero": CUENTA["minero"], "segundos": 15.2, "hashes_por_segundo": 1100000})

    assert respuesta.status_code == 204
    assert redis.hget(f"nodo:{CUENTA['minero']}", "bench") == "1100000"
    assert cliente.post("/hello", hola()).json() == {"registrado": True}


@pytest.mark.parametrize("malo", [
    {"minero": CUENTA["minero"], "tipo": "tpu", "placa": "x"},
    {"tipo": "gpu", "placa": "x"},
    ["no", "es", "un", "objeto"],
])
def test_hello_invalido_da_400(cliente, malo):
    assert cliente.post("/hello", malo).status_code == 400


def test_bench_invalido_da_400(cliente):
    assert cliente.post("/bench", {"minero": CUENTA["minero"], "hashes_por_segundo": -1}).status_code == 400


def test_cuenta_los_pedidos_rechazados(armar, redis):
    _, cliente = armar("intruso")
    antes = REGISTRY.get_sample_value("blockchain_pedidos_rechazados_total", {"motivo": "no_aprobado"}) or 0
    cliente.post("/hello", hola(CUENTA["intruso"]))
    assert REGISTRY.get_sample_value("blockchain_pedidos_rechazados_total", {"motivo": "no_aprobado"}) == antes + 1


def test_las_metricas_cuentan_los_mineros_vivos_por_tipo(armar, redis):
    armar()
    ahora = int(time.time())
    redis.hset("nodo:gpu-1", mapping={"tipo": "gpu", "bench": 2_000_000_000, "hashes_por_segundo": 1_900_000_000,
                                      "ultimo_aviso": ahora})
    redis.hset("nodo:gpu-2", mapping={"tipo": "gpu", "bench": 2_000_000_000, "ultimo_aviso": ahora - 60})  # caído
    redis.hset("nodo:cpu-1", mapping={"tipo": "cpu", "bench": 1_000_000, "ultimo_aviso": ahora})  # sin aviso todavía
    redis.hset("nodo:cpu-2", mapping={"tipo": "cpu", "bench": 2_000_000, "hashes_por_segundo": 0,
                                      "ultimo_aviso": ahora})  # todavía no minó: cuenta su bench

    assert REGISTRY.get_sample_value("blockchain_mineros", {"tipo": "gpu"}) == 1
    assert REGISTRY.get_sample_value("blockchain_mineros", {"tipo": "cpu"}) == 2
    assert REGISTRY.get_sample_value("blockchain_hashes_por_segundo", {"tipo": "gpu"}) == 1_900_000_000
    assert REGISTRY.get_sample_value("blockchain_hashes_por_segundo", {"tipo": "cpu"}) == 3_000_000
