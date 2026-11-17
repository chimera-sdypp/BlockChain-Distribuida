import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

import pytest

from comun import Rabbit, config, contrato, redisdb
from trp.app import Ajustes, RANGOS, TAREAS, cargar_ajustes, particionar

VECTOR = json.loads((Path(__file__).parents[2] / "comun" / "vector_prueba.json").read_text())
AHORA = 1_800_000_000


def tarea(id_="t1"):
    t = VECTOR["tarea"]
    return {"tarea": id_, "previous_hash": t["previous_hash"], "altura": t["altura"],
            "transacciones": t["transacciones"], "timestamp": t["timestamp"]}


def ajustes(**cambios):
    base = dict(espacio=2**32, porcentaje=10, prefijo_base=6, segundos_objetivo=30, ajustar=True, vivo_segundos=15,
                criterio="suma")
    return Ajustes(**{**base, **cambios})


class RedisFalso:
    def __init__(self, nodos=None):
        self.hashes = {f"nodo:{id_}": nodo for id_, nodo in (nodos or {}).items()}

    def scan_iter(self, match, count=None):
        prefijo = match.rstrip("*")
        return [clave for clave in self.hashes if clave.startswith(prefijo)]

    def hgetall(self, clave):
        return dict(self.hashes.get(clave, {}))

    def hset(self, clave, mapping):
        self.hashes.setdefault(clave, {}).update({k: str(v) for k, v in mapping.items()})


def correr(t, redis, **cambios):
    publicados = []
    particionar(t, redis, lambda cola, mensaje: publicados.append((cola, mensaje)), ajustes(**cambios), ahora=AHORA)
    return publicados


def test_publica_los_rangos_y_registra_la_tarea():
    redis = RedisFalso({"cpu-1": {"tipo": "cpu", "bench": "8600000", "ultimo_aviso": str(AHORA - 2)}})
    publicados = correr(tarea(), redis)
    assert len(publicados) == 10 and all(cola == RANGOS for cola, _ in publicados)
    primero, ultimo = publicados[0][1], publicados[-1][1]
    # Sin texto: el de cada minero, con su coinbase, lo arma el gateway al prestar el rango.
    assert primero == {"tarea": "t1", "rango": 0, "desde": 0, "hasta": 429_496_729, "prefijo": "000000"}
    assert (ultimo["rango"], ultimo["hasta"]) == (9, 2**32 - 1)
    assert redis.hashes["tarea:t1"] == {"prefijo": "000000", "rangos": "10"}


def test_con_lo_que_anota_el_texto_de_cada_minero_es_el_del_contrato():
    # tarea:<id> del NCT más el prefijo del TrP: el minero le concatena el nonce y da el block_hash del vector.
    redis = RedisFalso()
    t = tarea()
    redis.hashes["tarea:t1"] = {"previous_hash": t["previous_hash"], "timestamp": str(t["timestamp"]),
                                "altura": str(t["altura"]), "transacciones": json.dumps(t["transacciones"])}
    correr(t, redis, ajustar=False, prefijo_base=4)
    texto = contrato.texto_para(contrato.tarea_guardada(redis.hashes["tarea:t1"]), VECTOR["minero"])
    assert texto == VECTOR["texto"]
    assert contrato.hash_bloque(texto, VECTOR["nonce"]) == VECTOR["block_hash"]


def test_sin_gpu_baja_el_prefijo_y_con_gpu_usa_el_base():
    lento = {"cpu-1": {"tipo": "cpu", "bench": "500000", "ultimo_aviso": str(AHORA)}}
    gpu = {"gpu-1": {"tipo": "gpu", "bench": "2300000000", "ultimo_aviso": str(AHORA)}}
    assert correr(tarea(), RedisFalso(lento))[0][1]["prefijo"] == "00000"
    assert correr(tarea(), RedisFalso(gpu))[0][1]["prefijo"] == "000000"
    assert correr(tarea(), RedisFalso())[0][1]["prefijo"] == "0"  # nadie vivo


def test_sin_ajuste_siempre_el_prefijo_base():
    assert correr(tarea(), RedisFalso(), ajustar=False, prefijo_base=8)[0][1]["prefijo"] == "0" * 8


def test_el_prefijo_sigue_el_criterio():
    grupo = {"gpu-1": {"tipo": "gpu", "bench": "1800000000", "ultimo_aviso": str(AHORA)},
             "cpu-1": {"tipo": "cpu", "bench": "8600000", "ultimo_aviso": str(AHORA)},
             "cpu-2": {"tipo": "cpu", "bench": "8600000", "ultimo_aviso": str(AHORA)}}
    assert correr(tarea(), RedisFalso(grupo), prefijo_base=8)[0][1]["prefijo"] == "0" * 8
    assert correr(tarea(), RedisFalso(grupo), prefijo_base=8, criterio="minimo")[0][1]["prefijo"] == "0" * 6


def test_el_log_de_la_tarea_dice_criterio_y_potencia(caplog):
    gpu = {"gpu-1": {"tipo": "gpu", "bench": "1817200000", "ultimo_aviso": str(AHORA)}}
    with caplog.at_level("INFO", logger="trp"):
        correr(tarea(), RedisFalso(gpu), prefijo_base=8)
    linea = caplog.messages[-1]
    assert linea.endswith(", criterio suma, potencia 1,82 GH/s")
    # La prueba de fragmentación (BC-40) lee esta línea con su regex: tiene que seguir andando.
    partida = re.search(r"Tarea (\w+): \d+ transacciones, prefijo (0+), (\d+) rangos del (\d+) %", linea)
    assert partida.groups() == ("t1", "0" * 8, "10", "10")


def test_sin_ajuste_el_log_no_inventa_potencia(caplog):
    with caplog.at_level("INFO", logger="trp"):
        correr(tarea(), RedisFalso(), ajustar=False)
    assert caplog.messages[-1].endswith("rangos del 10 %")


def test_criterio_por_defecto_suma(monkeypatch):
    monkeypatch.delenv("CRITERIO_POTENCIA", raising=False)
    assert cargar_ajustes().criterio == "suma"
    monkeypatch.setenv("CRITERIO_POTENCIA", "minimo")
    assert cargar_ajustes().criterio == "minimo"


def test_criterio_invalido_no_arranca(monkeypatch):
    monkeypatch.setenv("CRITERIO_POTENCIA", "mediana")
    with pytest.raises(ValueError, match="CRITERIO_POTENCIA"):
        cargar_ajustes()


def test_repetir_la_tarea_deja_lo_mismo():
    redis = RedisFalso()
    primera, segunda = correr(tarea(), redis), correr(tarea(), redis)
    assert primera == segunda
    assert redis.hashes["tarea:t1"]["rangos"] == "10"


@pytest.mark.parametrize("cambio", [
    {"previous_hash": "corto"}, {"timestamp": "ayer"}, {"transacciones": None}, {"tarea": ""},
    {"altura": None}, {"altura": 0},
])
def test_tarea_invalida_se_descarta(cambio):
    redis = RedisFalso()
    assert correr({**tarea(), **cambio}, redis) == []
    assert "tarea:t1" not in redis.hashes


# --- Con RabbitMQ y Redis de verdad (INTEGRACION=1) -------------------------------------------
@pytest.mark.skipif(os.environ.get("INTEGRACION") != "1", reason="necesita RabbitMQ y Redis (INTEGRACION=1)")
def test_una_tarea_publicada_a_mano_genera_sus_rangos():
    c = config.cargar("prueba")
    sufijo = uuid.uuid4().hex[:8]
    cola_tareas, cola_rangos = f"{TAREAS}-{sufijo}", f"{RANGOS}-{sufijo}"
    redis = redisdb.conectar(c)
    consumidor, lector = Rabbit(c, [cola_tareas, cola_rangos]), Rabbit(c, [cola_rangos])
    id_ = f"t-{sufijo}"

    def manejar(t):
        particionar(t, redis, lambda _, m: consumidor.publicar(cola_rangos, m), ajustes(porcentaje=25))

    threading.Thread(target=consumidor.consumir, args=(cola_tareas, manejar), daemon=True).start()
    lector.conectar()
    lector.publicar(cola_tareas, tarea(id_))
    recibidos = []
    threading.Thread(target=lector.consumir, args=(cola_rangos, recibidos.append), daemon=True).start()
    limite = time.monotonic() + 10
    while time.monotonic() < limite and len(recibidos) < 4:
        time.sleep(0.1)
    try:
        assert sorted(m["rango"] for m in recibidos) == [0, 1, 2, 3]
        assert redis.hgetall(f"tarea:{id_}")["rangos"] == "4"
    finally:
        consumidor.detener()
        lector.detener()
        redis.delete(f"tarea:{id_}")
        limpiador = Rabbit(c)
        limpiador.conectar()
        for cola in (cola_tareas, cola_rangos):
            limpiador._canal.queue_delete(cola)
        limpiador.detener()
