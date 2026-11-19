import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

import pytest

from prometheus_client import REGISTRY

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
    def __init__(self, nodos=None, cadena=None, bloques=None):
        self.hashes = {f"nodo:{id_}": nodo for id_, nodo in (nodos or {}).items()}
        for hash_, bloque in (bloques or {}).items():
            self.hashes[f"bloque:{hash_}"] = bloque
        self.listas = {"cadena": list(cadena or [])}

    def scan_iter(self, match, count=None):
        prefijo = match.rstrip("*")
        return [clave for clave in self.hashes if clave.startswith(prefijo)]

    def hgetall(self, clave):
        return dict(self.hashes.get(clave, {}))

    def hset(self, clave, mapping):
        self.hashes.setdefault(clave, {}).update({k: str(v) for k, v in mapping.items()})

    def lrange(self, clave, inicio, fin):
        lista = self.listas.get(clave, [])
        n = len(lista)
        if inicio < 0:
            inicio = max(0, n + inicio)
        if fin < 0:
            fin = n + fin
        if fin < inicio or inicio >= n:
            return []
        return list(lista[inicio:fin + 1])


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
    assert linea.endswith(", criterio suma, potencia 1,82 GH/s, motivo inicial")  # el motivo de BC-60, al final
    # La prueba de fragmentación (BC-40) lee esta línea con su regex: tiene que seguir andando.
    partida = re.search(r"Tarea (\w+): \d+ transacciones, prefijo (0+), (\d+) rangos del (\d+) %", linea)
    assert partida.groups() == ("t1", "0" * 8, "10", "10")


def test_sin_ajuste_el_log_no_inventa_potencia(caplog):
    with caplog.at_level("INFO", logger="trp"):
        correr(tarea(), RedisFalso(), ajustar=False)
    assert caplog.messages[-1].endswith("rangos del 10 %, motivo fijo")


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


def test_cargar_ajustes_por_defecto_usa_contrato_5(monkeypatch):
    for var in ("SEGUNDOS_POR_BLOQUE", "VENTANA_AJUSTE", "AJUSTE_MAXIMO", "PREFIJO_INICIAL"):
        monkeypatch.delenv(var, raising=False)
    a = cargar_ajustes()
    assert (a.segundos_objetivo, a.ventana_ajuste, a.ajuste_maximo, a.prefijo_inicial) == (60, 5, 1, 0)


def test_ajuste_por_tiempo_desde_cadena_y_metricas():
    gpu = {"gpu-1": {"tipo": "gpu", "bench": "2300000000", "ultimo_aviso": str(AHORA)}}
    cadena = ["g", "b1", "b2", "b3", "b4", "b5"]
    bloques = {
        "g": {"altura": "0", "timestamp": "1000"},
        **{f"b{i}": {"altura": str(i), "prefijo": "0000", "timestamp": str(1000 + i * 2)} for i in range(1, 6)},
    }
    redis = RedisFalso(gpu, cadena=cadena, bloques=bloques)
    antes_tiempo = REGISTRY.get_sample_value("blockchain_prefijo_elecciones_total", {"motivo": "tiempo"}) or 0
    antes_potencia = REGISTRY.get_sample_value("blockchain_prefijo_elecciones_total", {"motivo": "potencia"}) or 0

    # Altura 5 (múltiplo de 5) y bloques cada 2 s frente a 60 s -> sube de 4 a 5
    publicados = correr(tarea("t5"), redis, prefijo_base=8, segundos_objetivo=60, ventana_ajuste=5)
    assert publicados[0][1]["prefijo"] == "00000"
    assert REGISTRY.get_sample_value("blockchain_prefijo") == 5
    assert REGISTRY.get_sample_value("blockchain_tiempo_bloque_esperado_segundos") == 60
    assert REGISTRY.get_sample_value("blockchain_prefijo_elecciones_total", {"motivo": "tiempo"}) == antes_tiempo + 1

    # Si desaparece la GPU antes de la siguiente ventana (altura 5 todavía), la potencia cae más de 4 veces:
    # se recalcula con el techo enseguida (BC-73)
    redis.hashes["nodo:gpu-1"]["ultimo_aviso"] = str(AHORA - 60)
    redis.hashes["nodo:cpu-lento"] = {"tipo": "cpu", "bench": "200000", "ultimo_aviso": str(AHORA)}
    # 200 kH/s * 60 s = 1,2e7 -> techo 5; supongamos que los bloques eran de prefijo 6
    redis.hashes["bloque:b5"]["prefijo"] = "000000"
    publicados_sin_gpu = correr(tarea("t5-rearmada"), redis, prefijo_base=8, segundos_objetivo=60, ventana_ajuste=5)
    assert publicados_sin_gpu[0][1]["prefijo"] == "00000"
    assert REGISTRY.get_sample_value("blockchain_prefijo_elecciones_total", {"motivo": "potencia"}) == antes_potencia + 1
    assert redis.hashes["prefijo:referencia"]["potencia"] == "200000.0"


def test_duracion_guardada_ignora_huecos_ociosos_entre_bloques():
    gpu = {"gpu-1": {"tipo": "gpu", "bench": "2300000000", "ultimo_aviso": str(AHORA)}}
    cadena = ["g", "b1", "b2", "b3", "b4", "b5"]
    # Timestamps muy separados (1 hora ociosa entre bloques), pero duración real de minado 2 s cada uno
    bloques = {
        "g": {"altura": "0", "timestamp": "0"},
        **{f"b{i}": {"altura": str(i), "prefijo": "000", "timestamp": str(i * 3600), "duracion": "2.0"}
           for i in range(1, 6)},
    }
    redis = RedisFalso(gpu, cadena=cadena, bloques=bloques)
    publicados = correr(tarea("t6"), redis, prefijo_base=8, segundos_objetivo=60, ventana_ajuste=5)
    assert publicados[0][1]["prefijo"] == "0000"  # subió de 3 a 4 pese al tiempo ocioso


def test_convergencia_desde_prefijo_inicial_bajo():
    gpu = {"gpu-1": {"tipo": "gpu", "bench": "2300000000", "ultimo_aviso": str(AHORA)}}
    redis = RedisFalso(gpu, cadena=["g"], bloques={"g": {"altura": "0", "timestamp": "1000"}})
    primero = correr(tarea("t1"), redis, prefijo_base=8, segundos_objetivo=60, ventana_ajuste=5, prefijo_inicial=2)
    assert primero[0][1]["prefijo"] == "00"


def test_espacio_de_nonces_chico_para_el_prefijo_no_arranca(monkeypatch):
    # BC-73: con 2^32 y prefijo 8, la mayoría de las tareas no tiene solución.
    monkeypatch.setenv("PREFIJO_BASE", "8")
    monkeypatch.setenv("ESPACIO_NONCES", str(2**32))
    with pytest.raises(ValueError, match="ESPACIO_NONCES"):
        cargar_ajustes()
    monkeypatch.setenv("ESPACIO_NONCES", str(2**34))  # 16^8 × 4
    assert cargar_ajustes().espacio == 2**34


def test_emergencia_negativa_no_arranca(monkeypatch):
    monkeypatch.setenv("EMERGENCIA_SEGUNDOS", "-1")
    with pytest.raises(ValueError, match="EMERGENCIA_SEGUNDOS"):
        cargar_ajustes()


def test_la_tarea_que_lleva_240_s_sin_bloque_baja_un_caracter():
    # El NCT manda en la tarea cuándo se armó la caja: si se rearmó a los 240 s, el TrP baja uno.
    gpu = {"gpu-1": {"tipo": "gpu", "bench": "2300000000", "ultimo_aviso": str(AHORA)}}
    bloques = {"g": {"altura": "0", "timestamp": "1000"},
               "b1": {"altura": "1", "prefijo": "000000", "timestamp": "1010", "duracion": "10"}}
    redis = RedisFalso(gpu, cadena=["g", "b1"], bloques=bloques)
    redis.hashes["prefijo:referencia"] = {"potencia": "2300000000", "altura": "0", "prefijo": "6", "cuando": "0"}
    antes = REGISTRY.get_sample_value("blockchain_prefijo_elecciones_total", {"motivo": "emergencia"}) or 0
    rearmada = {**tarea("t-rearmada"), "armada": AHORA - 250}
    publicados = correr(rearmada, redis, emergencia=240)
    assert publicados[0][1]["prefijo"] == "00000"
    assert REGISTRY.get_sample_value("blockchain_prefijo_elecciones_total", {"motivo": "emergencia"}) == antes + 1
    # Recién armada, no
    assert correr({**tarea("t-nueva"), "armada": AHORA - 5}, redis, emergencia=240)[0][1]["prefijo"] == "000000"


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
