import json
import os
import threading
import uuid
from pathlib import Path

import pytest

from comun import config, contrato, redisdb
from trp.resultados import SOLUCIONES, recibir

VECTOR = json.loads((Path(__file__).parents[2] / "comun" / "vector_prueba.json").read_text())
TEXTO, PREFIJO, NONCE, HASH = VECTOR["texto"], VECTOR["prefijo"], VECTOR["nonce"], VECTOR["block_hash"]
# Otra solución del mismo bloque, para el que llega segundo.
OTRO_NONCE = next(n for n in range(NONCE + 1, 10**7) if contrato.cumple(contrato.hash_bloque(TEXTO, n), PREFIJO))
OTRO_HASH = contrato.hash_bloque(TEXTO, OTRO_NONCE)


class RedisFalso:
    def __init__(self):
        self.hashes, self.conjuntos = {}, {}

    def hgetall(self, clave):
        return dict(self.hashes.get(clave, {}))

    def hget(self, clave, campo):
        return self.hashes.get(clave, {}).get(campo)

    def hsetnx(self, clave, campo, valor):
        h = self.hashes.setdefault(clave, {})
        if campo in h:
            return 0
        h[campo] = str(valor)
        return 1

    def sadd(self, clave, valor):
        self.conjuntos.setdefault(clave, set()).add(str(valor))

    def scard(self, clave):
        return len(self.conjuntos.get(clave, set()))


@pytest.fixture
def redis():
    r = RedisFalso()
    r.hashes["tarea:t1"] = {"bloque": TEXTO, "prefijo": PREFIJO, "rangos": "3"}
    return r


@pytest.fixture
def publicados():
    return []


def enviar(redis, publicados, **resultado):
    base = {"tarea": "t1", "rango": 0, "minero": "gpu-tomas"}
    recibir({**base, **resultado}, redis, lambda cola, m: publicados.append((cola, m)))


def con_nonce(nonce=NONCE, hash_=HASH):
    return {"nonce": nonce, "hash": hash_}


def test_primer_ganador_con_su_minero(redis, publicados):
    enviar(redis, publicados, **con_nonce())
    assert publicados == [(SOLUCIONES, {"tarea": "t1", "nonce": NONCE, "hash": HASH,
                                        "prefijo": PREFIJO, "minero": "gpu-tomas"})]
    assert json.loads(redis.hget("tarea:t1", "ganador")) == {"minero": "gpu-tomas", "nonce": NONCE, "hash": HASH}


def test_los_que_llegan_despues_se_descartan(redis, publicados):
    enviar(redis, publicados, **con_nonce())
    enviar(redis, publicados, rango=2, minero="cpu-vm1", **con_nonce(OTRO_NONCE, OTRO_HASH))
    enviar(redis, publicados, rango=1, sin_solucion=True)
    assert len(publicados) == 1 and publicados[0][1]["minero"] == "gpu-tomas"


def test_si_el_mismo_mensaje_vuelve_se_publica_de_nuevo(redis, publicados):
    # El pod murió después del HSETNX y antes del ack: el mensaje vuelve y no se puede perder.
    enviar(redis, publicados, **con_nonce())
    enviar(redis, publicados, **con_nonce())
    assert len(publicados) == 2 and publicados[0] == publicados[1]


def test_todos_sin_solucion(redis, publicados):
    enviar(redis, publicados, rango=0, sin_solucion=True)
    enviar(redis, publicados, rango=1, sin_solucion=True)
    assert publicados == []
    enviar(redis, publicados, rango=2, sin_solucion=True)
    assert publicados == [(SOLUCIONES, {"tarea": "t1", "sin_solucion": True})]


def test_un_rango_repetido_no_cuenta_dos_veces(redis, publicados):
    for _ in range(3):
        enviar(redis, publicados, rango=0, sin_solucion=True)
    enviar(redis, publicados, rango=1, sin_solucion=True)
    assert publicados == []


def test_un_hash_que_no_corresponde_se_descarta(redis, publicados):
    enviar(redis, publicados, **con_nonce(NONCE + 1, HASH))           # el hash no es de ese nonce
    enviar(redis, publicados, **con_nonce(5, contrato.hash_bloque(TEXTO, 5)))  # bien calculado pero sin el prefijo
    assert publicados == [] and redis.hget("tarea:t1", "ganador") is None


def test_tarea_que_no_partio_el_trp(redis, publicados):
    enviar(redis, publicados, tarea="otra", **con_nonce())
    assert publicados == []


@pytest.mark.parametrize("cambio", [
    {"rango": -1}, {"rango": "0"}, {"minero": "vm:1"}, {"tarea": ""},
    {"nonce": "1339"}, {"hash": "corto"}, {"sin_solucion": False},
])
def test_resultado_invalido(redis, publicados, cambio):
    resultado = {"tarea": "t1", "rango": 0, "minero": "gpu-tomas", **con_nonce(), **cambio}
    if cambio.get("sin_solucion") is False:
        resultado.pop("nonce")
    recibir(resultado, redis, lambda cola, m: publicados.append(m))
    assert publicados == []


# --- 2 pods a la vez, con Redis de verdad (INTEGRACION=1) --------------------------------------
@pytest.mark.skipif(os.environ.get("INTEGRACION") != "1", reason="necesita Redis (INTEGRACION=1)")
def test_con_dos_pods_sale_una_sola_solucion():
    real = redisdb.conectar(config.cargar("prueba"))
    for intento in range(20):
        id_ = f"t-{uuid.uuid4().hex[:8]}"
        real.hset(f"tarea:{id_}", mapping={"bloque": TEXTO, "prefijo": PREFIJO, "rangos": 2})
        publicados, listos = [], threading.Barrier(2)

        def pod(minero, nonce, hash_, rango):
            listos.wait()
            recibir({"tarea": id_, "rango": rango, "minero": minero, "nonce": nonce, "hash": hash_},
                    real, lambda cola, m: publicados.append(m))

        hilos = [threading.Thread(target=pod, args=("gpu-a", NONCE, HASH, 0)),
                 threading.Thread(target=pod, args=("gpu-b", OTRO_NONCE, OTRO_HASH, 1))]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        real.delete(f"tarea:{id_}")
        assert len(publicados) == 1, (intento, publicados)
