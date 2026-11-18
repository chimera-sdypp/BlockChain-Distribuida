"""BC-54: el historial de cada minero (tiempo conectado, alta, hashrate), quién entra y quién sale, GET /mineros
y las métricas por minero."""

import logging
import time

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from comun import web
from comun.tests.claves import CUENTA
from gateway import app as gateway

MINERO = CUENTA["minero"]
HISTORIAL = f"historial:{MINERO}"


def test_el_tiempo_conectado_suma_los_avisos_seguidos(armar, redis):
    g, _ = armar()
    for ahora in (1000, 1005, 1010, 1014):
        g.avisar(MINERO, "minero", ahora, hps=2_000)
    historial = redis.hgetall(HISTORIAL)
    assert historial["segundos_conectado"] == "14"
    assert (historial["ultimo_aviso"], historial["hashes_por_segundo"], historial["conectado"]) == ("1014", "2000", "1")
    assert redis.sismember("mineros:conectados", MINERO)


def test_un_hueco_mas_largo_que_vivo_no_cuenta_como_conectado(armar, redis):
    g, _ = armar()
    for ahora in (1000, 1005, 1100, 1105):   # de 1005 a 1100 no avisó: estuvo desconectado
        g.avisar(MINERO, "minero", ahora)
    assert redis.hget(HISTORIAL, "segundos_conectado") == "10"


def test_un_aviso_viejo_de_otro_pod_no_vuelve_atras_el_ultimo_aviso(armar, redis):
    g, _ = armar()
    g.avisar(MINERO, "minero", 1005)
    g.avisar(MINERO, "minero", 1004)
    assert redis.hget(HISTORIAL, "ultimo_aviso") == "1005"


def test_la_conexion_queda_en_el_log_una_vez(armar, caplog):
    g, _ = armar()
    with caplog.at_level(logging.INFO, logger="gateway"):
        for ahora in (1000, 1005, 1010):
            g.avisar(MINERO, "minero", ahora)
    assert sum("Minero conectado: minero" in r.getMessage() for r in caplog.records) == 1


def test_el_alta_sale_de_cuentas_al_presentarse(cliente, redis):
    respuesta = cliente.post("/hello", {"minero": MINERO, "tipo": "gpu", "placa": "RTX 4060"})
    assert respuesta.status_code == 200
    assert redis.hget(HISTORIAL, "alta") == "1793836800"   # la de aprobar() en conftest
    assert redis.hget(HISTORIAL, "conectado") == "1"


def test_el_keepalive_anota_el_hashrate_y_el_aviso(cliente, redis):
    redis.hset(f"nodo:{MINERO}", mapping={"tipo": "gpu", "alias": "minero", "bench": 1})
    antes = int(time.time())
    respuesta = cliente.post("/keepalive", {"minero": MINERO, "hashes_por_segundo": 1_500_000_000, "rango": None})
    assert respuesta.status_code == 200
    historial = redis.hgetall(HISTORIAL)
    assert historial["hashes_por_segundo"] == "1500000000" and int(historial["ultimo_aviso"]) >= antes


def test_el_que_deja_de_avisar_queda_desconectado_y_en_el_log(armar, redis, caplog):
    g, _ = armar()
    redis.hset(f"nodo:{MINERO}", "alias", "gpu-tomas")
    for ahora in (1000, 1005):
        g.avisar(MINERO, "gpu-tomas", ahora)
    assert g.revisar_desconectados(ahora=1015) == 0                 # 10 s sin avisar: sigue vivo
    with caplog.at_level(logging.WARNING, logger="gateway"):
        assert g.revisar_desconectados(ahora=1030) == 1
    assert redis.hget(HISTORIAL, "conectado") == "0" and not redis.sismember("mineros:conectados", MINERO)
    assert any("Minero desconectado: gpu-tomas" in r.getMessage() for r in caplog.records)
    assert g.revisar_desconectados(ahora=1040) == 0                 # una sola vez
    g.avisar(MINERO, "gpu-tomas", 1100)                             # vuelve: se reconecta, el hueco no suma
    assert redis.hget(HISTORIAL, "conectado") == "1" and redis.hget(HISTORIAL, "segundos_conectado") == "5"


def test_con_dos_pods_lo_da_por_desconectado_uno_solo(armar, redis):
    g1, _ = armar()
    g2, _ = armar()
    g1.avisar(MINERO, "minero", 1000)
    assert g1.revisar_desconectados(ahora=1100) + g2.revisar_desconectados(ahora=1100) == 1


def test_mineros_con_si_estan_vivos_su_saldo_y_su_historial(armar, redis):
    g, _ = armar()
    ahora = int(time.time())
    redis.hset("nodo:gpu-1", mapping={"tipo": "gpu", "alias": "gpu-tomas", "placa": "RTX 4060", "bench": 2400000000,
                                      "hashes_por_segundo": 2350000000, "ultimo_aviso": ahora - 2})
    redis.hset("nodo:cpu-1", mapping={"tipo": "cpu", "placa": "Ryzen 7", "bench": 1100000,
                                      "hashes_por_segundo": 1000000, "ultimo_aviso": ahora - 60})
    redis.hset("historial:gpu-1", mapping={"alta": 1793836800, "bloques": 3, "recompensas": 180, "shares": 40,
                                           "rangos": 12, "segundos_conectado": 7200})
    redis.hset("saldos", mapping={"gpu-1": 180, "alice": 1000})
    redis.rpush("cadena", *[f"b{i}" for i in range(23)])            # 23 bloques: la época en curso es la 2
    redis.zadd("shares:2", {"gpu-1": 9})

    respuesta = TestClient(_app(g)).get("/mineros")                  # sin firma

    assert respuesta.status_code == 200
    mineros = {m["id"]: m for m in respuesta.json()}
    gpu, cpu = mineros["gpu-1"], mineros["cpu-1"]
    assert (gpu["alias"], gpu["tipo"], gpu["placa"], gpu["vivo"], gpu["saldo"]) == ("gpu-tomas", "gpu", "RTX 4060",
                                                                                    True, 180)
    assert (gpu["bloques"], gpu["recompensas"], gpu["shares"], gpu["rangos"]) == (3, 180, 40, 12)
    assert (gpu["segundos_conectado"], gpu["alta"], gpu["shares_epoca"], gpu["epoca"]) == (7200, 1793836800, 9, 2)
    assert (cpu["alias"], cpu["vivo"], cpu["saldo"], cpu["bloques"], cpu["alta"]) == ("cpu-1"[:8], False, 0, 0, None)


def test_mineros_sin_mineros(armar):
    g, _ = armar()
    assert TestClient(_app(g)).get("/mineros").json() == []


def test_las_metricas_tienen_el_historial_de_cada_minero(armar, redis):
    armar()
    ahora = int(time.time())
    redis.hset("nodo:gpu-1", mapping={"tipo": "gpu", "alias": "gpu-tomas", "bench": 2_000_000_000,
                                      "hashes_por_segundo": 1_900_000_000, "ultimo_aviso": ahora})
    redis.hset("nodo:cpu-1", mapping={"tipo": "cpu", "alias": "minero-cpu-0", "hashes_por_segundo": 1_000_000,
                                      "ultimo_aviso": ahora - 60})   # caído
    redis.hset("historial:gpu-1", mapping={"alta": 1793836800, "bloques": 3, "recompensas": 180, "shares": 40,
                                           "segundos_conectado": 7200})
    redis.rpush("cadena", *[f"b{i}" for i in range(12)])
    redis.zadd("shares:1", {"gpu-1": 5})

    gpu = {"minero": "gpu-tomas", "tipo": "gpu"}
    cpu = {"minero": "minero-cpu-0", "tipo": "cpu"}
    muestra = REGISTRY.get_sample_value
    assert muestra("blockchain_minero_bloques_total", gpu) == 3
    assert muestra("blockchain_minero_recompensas_total", gpu) == 180
    assert muestra("blockchain_minero_shares_total", gpu) == 40
    assert muestra("blockchain_minero_segundos_conectado_total", gpu) == 7200
    assert muestra("blockchain_minero_hashes_por_segundo", gpu) == 1_900_000_000
    assert muestra("blockchain_minero_vivo", gpu) == 1
    assert muestra("blockchain_minero_alta", gpu) == 1793836800
    assert muestra("blockchain_minero_shares_epoca", gpu) == 5
    assert muestra("blockchain_epoca", {}) == 1
    assert muestra("blockchain_minero_vivo", cpu) == 0
    assert muestra("blockchain_minero_hashes_por_segundo", cpu) == 0   # caído: no cuenta su último hashrate
    assert muestra("blockchain_minero_bloques_total", cpu) == 0


def _app(g):
    app = web.crear_app("gateway", {})
    gateway.agregar_rutas(app, g)
    return app
