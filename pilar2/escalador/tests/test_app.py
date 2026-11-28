import json
import os
import subprocess
import urllib.error
import uuid

import pytest

from comun import config, contrato, redisdb
from comun.tests.claves import CUENTA, PRIVADA, cadena_actual
from escalador.app import (SIMULADAS, AtributosGCE, ClavesVMs, Simulado, Tofu, ciclo, hay_trabajo, leer_nodos,
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
    """Los HGET/HGETALL/HEXISTS/HSET que usa ClavesVMs."""

    def __init__(self):
        self.hashes = {}

    def hgetall(self, clave):
        return dict(self.hashes.get(clave, {}))

    def hget(self, clave, campo):
        return self.hashes.get(clave, {}).get(campo)

    def hexists(self, clave, campo):
        return campo in self.hashes.get(clave, {})

    def hset(self, clave, campo=None, valor=None, mapping=None):
        self.hashes.setdefault(clave, {}).update(mapping or {campo: valor})


class BackendFalso:
    """El backend que ve el escalador: valida la firma del acta (sobre el génesis) y lleva el nonce de su clave."""

    def __init__(self):
        self.recibidas, self.nonces = [], {}

    def nonce(self, cuenta):
        return self.nonces.get(cuenta, 0)

    def cadena(self):
        return cadena_actual()

    def mandar(self, tx):
        assert contrato.validar_acta(tx, "f" * 64, cadena_actual()) == [] and contrato.es_delegada(tx)
        self.recibidas.append(tx)
        self.nonces[tx["origen"]] = tx["nonce"]
        return 202, {"id": tx["id"]}


ESCALADOR = PRIVADA["escalador"]


def vm(n):
    """La clave pública que publica la VM n (la privada no sale de la VM: acá sólo se usa para derivarla)."""
    return CUENTA[f"vm{n}"]


def armar(publicadas=None, permiso=True):
    redis, backend = HashesFalsos(), BackendFalso()
    publicadas = {} if publicadas is None else publicadas
    claves = ClavesVMs(redis, backend, publicadas.get, ESCALADOR)
    if permiso:
        redis.hset("permisos", claves.cuenta, "minero-cpu-")
    return redis, backend, claves, publicadas


def en_la_cadena(redis, cuenta, nombre):
    redis.hset("cuentas", cuenta, json.dumps({"tipo": "minero", "nombre": nombre, "alta": AHORA}))


def actas(backend):
    return [(tx["id"].split("-")[0], tx["destino"], tx["datos"]) for tx in backend.recibidas]


def test_sin_escalador_clave_no_arranca():
    with pytest.raises(ValueError, match="ESCALADOR_CLAVE"):
        ClavesVMs(HashesFalsos(), BackendFalso(), {}.get, "")


def test_cada_vm_se_da_de_alta_con_la_clave_que_publico_y_ninguna_privada_pasa_por_redis():
    redis, backend, claves, publicadas = armar({"minero-cpu-0": vm(0)})
    assert claves.preparar(2, ahora=AHORA) == {"minero-cpu-0": vm(0)}  # la 1 todavía no arrancó
    assert actas(backend) == [("alta", vm(0), "minero:minero-cpu-0")]
    publicadas["minero-cpu-1"] = vm(1)
    redis.hset("altas:en_camino", vm(0), "{}")
    claves.preparar(2, ahora=AHORA + 10)
    assert actas(backend)[1:] == [("alta", vm(1), "minero:minero-cpu-1")]  # la 0, en camino: no se repite
    assert [tx["nonce"] for tx in backend.recibidas] == [1, 2]
    en_la_cadena(redis, vm(0), "minero-cpu-0")
    en_la_cadena(redis, vm(1), "minero-cpu-1")
    claves.preparar(2, ahora=AHORA + 20)
    assert len(backend.recibidas) == 2
    assert not [clave for clave in redis.hashes if clave.startswith("escalador")]
    privadas = {PRIVADA["vm0"], PRIVADA["vm1"], ESCALADOR}
    assert not any(p in json.dumps(redis.hashes) for p in privadas)


def test_una_vm_recreada_con_otra_clave_da_de_baja_la_vieja():
    redis, backend, claves, _ = armar({"minero-cpu-0": vm(9)})
    en_la_cadena(redis, vm(0), "minero-cpu-0")  # la clave del disco anterior (la VM spot se borró y se volvió a crear)
    claves.preparar(1, ahora=AHORA)
    assert actas(backend) == [("baja", vm(0), "la VM se recreó con otra clave"), ("alta", vm(9), "minero:minero-cpu-0")]


def test_una_baja_del_organizador_no_la_deshace_el_escalador(caplog):
    redis, backend, claves, _ = armar({"minero-cpu-0": vm(0)})
    redis.hset("vetadas", vm(0), "1")  # el organizador le dio de baja a esa clave
    with caplog.at_level("WARNING", logger="escalador"):
        for segundos in range(3):
            claves.preparar(1, ahora=AHORA + segundos)
    assert backend.recibidas == [] and caplog.text.count("tiene una baja del organizador") == 1  # avisa una vez


def test_sin_permiso_no_manda_nada_y_lo_avisa(caplog):
    _, backend, claves, _ = armar({"minero-cpu-0": vm(0)}, permiso=False)
    with caplog.at_level("ERROR", logger="escalador"):
        claves.preparar(1, ahora=AHORA)
    assert backend.recibidas == [] and "python -m claves permiso" in caplog.text


def test_si_el_permiso_llega_despues_la_vm_ya_creada_pide_su_alta_en_el_ciclo_siguiente(caplog):
    redis, backend, claves, _ = armar({"minero-cpu-0": vm(0)}, permiso=False)
    tofu = Tofu("/tofu", claves=claves, ejecutar=Ejecutar({"apply": "Apply complete!"}))
    with caplog.at_level("ERROR", logger="escalador"):
        tofu.aplicar(1)
        tofu.revisar(1)
        tofu.revisar(1)
    assert backend.recibidas == [] and caplog.text.count("python -m claves permiso") == 1  # avisa una vez
    redis.hset("permisos", claves.cuenta, "minero-cpu-")
    tofu.revisar(1)
    assert actas(backend) == [("alta", vm(0), "minero:minero-cpu-0")]
    tofu.revisar(0)


def test_tofu_no_lleva_claves_y_da_de_baja_las_vms_que_sobran():
    redis, backend, claves, _ = armar()
    ejecutar = Ejecutar({"apply": "Apply complete!"})
    tofu = Tofu("/tofu", claves=claves, ejecutar=ejecutar)
    for n in range(3):
        en_la_cadena(redis, vm(n), f"minero-cpu-{n}")
    en_la_cadena(redis, CUENTA["pc-tomas"], "pc-tomas")  # un minero ajeno: no es del escalador
    tofu.aplicar(1)
    assert ejecutar.comandos[-1] == ["apply", "-auto-approve", "-input=false", "-var", "cantidad=1"]
    assert ejecutar.entornos[-1] is None  # nada de TF_VAR_claves: el state no tiene privadas (BC-95)
    assert actas(backend) == [("baja", vm(1), "la VM se borró"), ("baja", vm(2), "la VM se borró")]


class Respuesta:
    def __init__(self, datos):
        self.datos = datos

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.datos).encode()


def test_la_clave_publicada_se_lee_de_los_guest_attributes_con_el_token_del_pod():
    pedidos = []

    def abrir(pedido, timeout):
        pedidos.append(pedido)
        if "metadata.google.internal" in pedido.full_url:
            assert pedido.get_header("Metadata-flavor") == "Google"
            return Respuesta({"access_token": "tok", "expires_in": 3600})
        if "minero-cpu-1" in pedido.full_url:
            raise urllib.error.HTTPError(pedido.full_url, 404, "no publicó", {}, None)
        assert pedido.get_header("Authorization") == "Bearer tok"
        return Respuesta({"variableValue": vm(0) + "\n"})

    atributos = AtributosGCE("proyecto-x", "us-central1-a", abrir=abrir)
    assert atributos("minero-cpu-0") == vm(0)
    assert atributos("minero-cpu-1") is None
    assert pedidos[1].full_url == ("https://compute.googleapis.com/compute/v1/projects/proyecto-x/zones/us-central1-a/"
                                   "instances/minero-cpu-0/getGuestAttributes?variableKey=minero/publica")
    assert sum("metadata.google.internal" in p.full_url for p in pedidos) == 1  # el token se reusa


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
