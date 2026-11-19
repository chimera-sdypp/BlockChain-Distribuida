import json
import os
import subprocess
import uuid

import pytest

from comun import config, contrato, redisdb
from escalador.app import (CLAVES, SIMULADAS, ClavesVMs, Simulado, Tofu, ciclo, hay_trabajo, leer_nodos,
                           segundos_sin_bloque)
from escalador.decision import Ajustes, Decisor, sacar_foto

AHORA = 1_800_000_000


def ajustes():
    return Ajustes(prefijo_base=6, segundos_objetivo=30, maximo=3, hps_vm=1_000_000, espera_subir=30,
                   espera_bajar=120, vivo_segundos=15, prefijo_vm="minero-cpu-")


class RedisFalso:
    def __init__(self, nodos=None, pendientes=0, cajas=0):
        self.hashes = {f"nodo:{id_}": nodo for id_, nodo in (nodos or {}).items()}
        self.listas = {"tx:pendientes": pendientes, "cajas": cajas}
        self._cola = []

    def scan_iter(self, match, count=None):
        return [clave for clave in self.hashes if clave.startswith(match.rstrip("*"))]

    def pipeline(self, transaction=True):
        return self

    def hgetall(self, clave):
        self._cola.append(dict(self.hashes.get(clave, {})))

    def llen(self, clave):
        self._cola.append(self.listas[clave])

    def lindex(self, clave, indice):
        self._cola.append(None)  # sin cajas armadas: nada trabado

    def hmget(self, clave, *campos):
        self._cola.append([None] * len(campos))

    def execute(self):
        resultado, self._cola = self._cola, []
        return resultado


class AplicadorFalso:
    def __init__(self, falla=False):
        self.pedidos, self.falla = [], falla

    def aplicar(self, cantidad):
        self.pedidos.append(cantidad)
        if self.falla:
            raise RuntimeError("tofu falló")


def test_trabajo_es_pendientes_o_cajas():
    assert not hay_trabajo(RedisFalso())
    assert hay_trabajo(RedisFalso(pendientes=1))
    assert hay_trabajo(RedisFalso(cajas=1))


def test_leer_nodos_por_id():
    redis = RedisFalso({"gpu-1": {"tipo": "gpu"}, "minero-cpu-0": {"tipo": "cpu"}})
    assert leer_nodos(redis) == {"gpu-1": {"tipo": "gpu"}, "minero-cpu-0": {"tipo": "cpu"}}


def test_ciclo_aplica_solo_cuando_cambia_la_cantidad():
    redis, aplicador = RedisFalso(cajas=1), AplicadorFalso()
    decisor = Decisor(ajustes(), actual=0)
    assert [ciclo(redis, decisor, aplicador, AHORA + s) for s in (0, 10, 30, 40)] == [0, 0, 1, 1]
    assert aplicador.pedidos == [1]


def test_si_el_apply_falla_se_reintenta_en_el_proximo_ciclo():
    redis, aplicador = RedisFalso(cajas=1), AplicadorFalso(falla=True)
    decisor = Decisor(ajustes(), actual=0)
    ciclo(redis, decisor, aplicador, AHORA)
    with pytest.raises(RuntimeError):
        ciclo(redis, decisor, aplicador, AHORA + 30)
    assert decisor.actual == 0
    aplicador.falla = False
    assert ciclo(redis, decisor, aplicador, AHORA + 40) == 1
    assert aplicador.pedidos == [1, 1]


# --- OpenTofu, sin correrlo ------------------------------------------------------------------
class Ejecutar:
    def __init__(self, salidas=None):
        self.comandos, self.salidas, self.entornos = [], salidas or {}, []

    def __call__(self, comando, **opciones):
        assert opciones["check"] and opciones["timeout"]
        self.comandos.append(comando[2:])
        self.entornos.append(opciones.get("env"))
        salida = self.salidas.get(comando[2])
        if isinstance(salida, Exception):
            raise salida
        return subprocess.CompletedProcess(comando, 0, stdout=salida or "")


def test_tofu_inicia_lee_y_aplica_la_cantidad():
    ejecutar = Ejecutar({"output": "2\n", "apply": "Apply complete! Resources: 1 added, 0 changed, 0 destroyed."})
    tofu = Tofu("/tofu", ejecutar=ejecutar)
    assert tofu.actual() == 2
    tofu.aplicar(3)
    assert ejecutar.comandos == [["init", "-input=false"], ["output", "-no-color", "-raw", "cantidad"],
                                 ["apply", "-auto-approve", "-input=false", "-var", "cantidad=3"]]


class HashesFalsos:
    """Los HSET/HGET/HGETALL/HEXISTS/HDEL/GET/SET que usan las claves de las VMs."""

    def __init__(self):
        self.hashes, self.valores = {}, {}

    def hgetall(self, clave):
        return dict(self.hashes.get(clave, {}))

    def hget(self, clave, campo):
        return self.hashes.get(clave, {}).get(campo)

    def hexists(self, clave, campo):
        return campo in self.hashes.get(clave, {})

    def hset(self, clave, campo=None, valor=None, mapping=None):
        self.hashes.setdefault(clave, {}).update(mapping or {campo: valor})

    def hdel(self, clave, campo):
        self.hashes.get(clave, {}).pop(campo, None)

    def get(self, clave):
        return self.valores.get(clave)

    def set(self, clave, valor, nx=False):
        if nx and clave in self.valores:
            return None
        self.valores[clave] = valor
        return True


class BackendFalso:
    """El backend que ve el escalador: valida la firma del acta y lleva el nonce de su clave."""

    def __init__(self):
        self.recibidas, self.nonces = [], {}

    def nonce(self, cuenta):
        return self.nonces.get(cuenta, 0)

    def mandar(self, tx):
        assert contrato.validar_acta(tx, "f" * 64) == [] and contrato.es_delegada(tx)
        self.recibidas.append(tx)
        self.nonces[tx["origen"]] = tx["nonce"]
        return 202, {"id": tx["id"]}


def con_permiso(redis, claves, prefijo="minero-cpu-"):
    redis.hset("permisos", claves.cuenta, prefijo)


def test_cada_vm_tiene_su_clave_y_se_da_de_alta_en_la_cadena_con_un_alta_delegada():
    redis, backend = HashesFalsos(), BackendFalso()
    claves = ClavesVMs(redis, backend)
    assert redis.valores["escalador:clave"] == claves.privada  # la genera la primera vez y la guarda
    con_permiso(redis, claves)
    dos = claves.preparar(2, ahora=AHORA)
    assert sorted(dos) == ["minero-cpu-0", "minero-cpu-1"] and len(set(dos.values())) == 2
    assert [(tx["destino"], tx["datos"], tx["nonce"]) for tx in backend.recibidas] == [
        (contrato.clave_publica(dos[n]), f"minero:{n}", i + 1) for i, n in enumerate(sorted(dos))]
    assert "cuentas" not in redis.hashes  # el escalador no escribe `cuentas`: lo hace el consenso
    # Ya en la cadena (o en camino): no se vuelven a pedir. Otro pod usa las mismas claves.
    for privada in dos.values():
        redis.hset("cuentas", contrato.clave_publica(privada), "{}")
    tres = ClavesVMs(redis, backend).preparar(3, ahora=AHORA + 60)
    assert tres == {**dos, "minero-cpu-2": redis.hashes[CLAVES]["minero-cpu-2"]} and len(backend.recibidas) == 3

    assert claves.retirar(1) == ["minero-cpu-1", "minero-cpu-2"]
    assert list(redis.hashes[CLAVES]) == ["minero-cpu-0"]
    bajas = [tx for tx in backend.recibidas if tx["id"].startswith("baja-")]
    assert [tx["destino"] for tx in bajas] == [contrato.clave_publica(dos["minero-cpu-1"])]  # la 2 no estaba en la cadena


def test_sin_permiso_no_manda_nada_y_lo_avisa(caplog):
    redis, backend = HashesFalsos(), BackendFalso()
    claves = ClavesVMs(redis, backend, privada="1" * 64)
    with caplog.at_level("ERROR", logger="escalador"):
        assert len(claves.preparar(1, ahora=AHORA)) == 1
    assert backend.recibidas == [] and "python -m claves permiso" in caplog.text


def test_tofu_le_pasa_las_claves_por_entorno_y_da_de_baja_las_que_sobran():
    redis, ejecutar, backend = HashesFalsos(), Ejecutar({"apply": "Apply complete!"}), BackendFalso()
    claves = ClavesVMs(redis, backend)
    con_permiso(redis, claves)
    tofu = Tofu("/tofu", claves=claves, ejecutar=ejecutar)
    tofu.aplicar(2)
    pasadas = json.loads(ejecutar.entornos[-1]["TF_VAR_claves"])
    assert pasadas == redis.hashes[CLAVES] and len(pasadas) == 2
    assert all("minero-cpu" not in parte for parte in ejecutar.comandos[-1])  # nada de claves en los argumentos
    tofu.aplicar(0)
    assert json.loads(ejecutar.entornos[-1]["TF_VAR_claves"]) == {} and redis.hashes[CLAVES] == {}


def test_las_vms_propias_se_reconocen_por_el_alias_de_cuentas():
    vm = {"tipo": "cpu", "bench": "800000", "ultimo_aviso": str(AHORA), "alias": "minero-cpu-0"}
    ajena = {"tipo": "cpu", "bench": "5000000", "ultimo_aviso": str(AHORA), "alias": "pc-tomas"}
    foto = sacar_foto({"a" * 64: vm, "b" * 64: ajena}, AHORA, True, ajustes())
    assert (foto.hps_vm, foto.ajena) == (800_000, 5_000_000)


def test_tofu_sin_state_es_cero():
    tofu = Tofu("/tofu", ejecutar=Ejecutar({"output": subprocess.CalledProcessError(1, "tofu")}))
    assert tofu.actual() == 0


def test_tofu_con_state_vacio_es_cero():
    aviso = "Warning: No outputs found\n\nThe state file either has no outputs defined, or all the defined outputs\n"
    tofu = Tofu("/tofu", ejecutar=Ejecutar({"output": aviso}))
    assert tofu.actual() == 0


# --- Con Redis de verdad ---------------------------------------------------------------------
integracion = pytest.mark.skipif(os.environ.get("INTEGRACION") != "1", reason="INTEGRACION=1 para correrlos")


@integracion
def test_simulado_guarda_la_cantidad_en_redis_y_sobrevive_un_reinicio():
    redis = redisdb.conectar(config.cargar("prueba-escalador"))
    redis.delete(SIMULADAS)
    try:
        assert Simulado(redis).actual() == 0
        Simulado(redis).aplicar(2)
        assert Simulado(redis).actual() == 2  # otro pod del escalador arranca desde ahí
    finally:
        redis.delete(SIMULADAS)


@integracion
def test_ciclo_contra_redis():
    redis = redisdb.conectar(config.cargar("prueba-escalador"))
    gpu = f"gpu-{uuid.uuid4().hex[:6]}"
    redis.delete(SIMULADAS, "tx:pendientes", "cajas")
    try:
        redis.rpush("cajas", '["a"]')
        aplicador = Simulado(redis)
        decisor = Decisor(ajustes(), aplicador.actual())
        ciclo(redis, decisor, aplicador, AHORA)
        assert ciclo(redis, decisor, aplicador, AHORA + 30) == 1 and aplicador.actual() == 1
        redis.hset(f"nodo:{gpu}", mapping={"tipo": "gpu", "bench": 2_000_000_000, "ultimo_aviso": AHORA + 40})
        ciclo(redis, decisor, aplicador, AHORA + 40)
        assert ciclo(redis, decisor, aplicador, AHORA + 50) == 1   # todavía no: espera_bajar
        redis.hset(f"nodo:{gpu}", "ultimo_aviso", AHORA + 160)
        assert ciclo(redis, decisor, aplicador, AHORA + 160) == 0 and aplicador.actual() == 0
    finally:
        redis.delete(SIMULADAS, "cajas", f"nodo:{gpu}")


@pytest.mark.skipif(os.environ.get("INTEGRACION") != "1", reason="necesita Redis (INTEGRACION=1)")
def test_segundos_sin_bloque_desde_que_se_armo_la_primera_caja():
    redis = redisdb.conectar(config.cargar("prueba-escalador"))
    try:
        redis.delete(contrato.CAJAS, contrato.CAJA_ARMADA)
        assert segundos_sin_bloque(redis, AHORA) == 0  # sin cajas
        redis.rpush(contrato.CAJAS, '["a"]', '["b"]')
        redis.hset(contrato.CAJA_ARMADA, mapping={"caja": '["a"]', "desde": AHORA - 400})
        assert segundos_sin_bloque(redis, AHORA) == 400
        redis.lpop(contrato.CAJAS)  # salió el bloque de la «a»: la «b» todavía no se armó
        assert segundos_sin_bloque(redis, AHORA) == 0
    finally:
        redis.delete(contrato.CAJAS, contrato.CAJA_ARMADA)
