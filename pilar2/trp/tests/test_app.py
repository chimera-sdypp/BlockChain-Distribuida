import json
import os
import threading
import time
import uuid
from pathlib import Path

import pytest

from comun import Rabbit, config, contrato, redisdb
from trp.app import Ajustes, RANGOS, TAREAS, particionar

VECTOR = json.loads((Path(__file__).parents[2] / "comun" / "vector_prueba.json").read_text())
AHORA = 1_800_000_000


def tarea(id_="t1"):
    return {"tarea": id_, "previous_hash": VECTOR["previous_hash"],
            "transacciones": VECTOR["transacciones"], "timestamp": VECTOR["timestamp"]}


def ajustes(**cambios):
    base = dict(espacio=2**32, porcentaje=10, prefijo_base=6, segundos_objetivo=30, ajustar=True, vivo_segundos=15)
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
    assert primero == {"tarea": "t1", "rango": 0, "bloque": VECTOR["texto"],
                       "desde": 0, "hasta": 429_496_729, "prefijo": "000000"}
    assert (ultimo["rango"], ultimo["hasta"]) == (9, 2**32 - 1)
    assert redis.hashes["tarea:t1"] == {"bloque": VECTOR["texto"], "prefijo": "000000", "rangos": "10"}


def test_el_texto_es_el_del_contrato():
    # El minero le concatena el nonce al texto y tiene que dar el block_hash del vector.
    mensaje = correr(tarea(), RedisFalso())[0][1]
    assert contrato.hash_bloque(mensaje["bloque"], VECTOR["nonce"]) == VECTOR["block_hash"]


def test_sin_gpu_baja_el_prefijo_y_con_gpu_usa_el_base():
    lento = {"cpu-1": {"tipo": "cpu", "bench": "500000", "ultimo_aviso": str(AHORA)}}
    gpu = {"gpu-1": {"tipo": "gpu", "bench": "2300000000", "ultimo_aviso": str(AHORA)}}
    assert correr(tarea(), RedisFalso(lento))[0][1]["prefijo"] == "00000"
    assert correr(tarea(), RedisFalso(gpu))[0][1]["prefijo"] == "000000"
    assert correr(tarea(), RedisFalso())[0][1]["prefijo"] == "0"  # nadie vivo


def test_sin_ajuste_siempre_el_prefijo_base():
    assert correr(tarea(), RedisFalso(), ajustar=False, prefijo_base=8)[0][1]["prefijo"] == "0" * 8


def test_repetir_la_tarea_deja_lo_mismo():
    redis = RedisFalso()
    primera, segunda = correr(tarea(), redis), correr(tarea(), redis)
    assert primera == segunda
    assert redis.hashes["tarea:t1"]["rangos"] == "10"


@pytest.mark.parametrize("cambio", [
    {"previous_hash": "corto"}, {"timestamp": "ayer"}, {"transacciones": None}, {"tarea": ""},
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
