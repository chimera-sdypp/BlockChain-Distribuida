"""La API del backend con Redis y RabbitMQ falsos: formato, firma, reenvíos, fallas, lote y métricas."""

import json

import pytest
import redis
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from backend import app as backend
from comun import contrato, web
from comun.tests.claves import CUENTA, FECHA, PRIVADA, Billetera, carga, firmada

COLA = "transacciones-prueba"


APROBADAS = ("alice", "bob", "carol")


class RedisFalso:
    def __init__(self, falla=False, aprobadas=APROBADAS, permisos=()):
        """Las cuentas de `aprobadas` están en `cuentas` y las claves de `permisos`, en `permisos` (BC-94)."""
        self.hashes, self.falla = {}, falla
        if aprobadas:
            self.hashes[contrato.CUENTAS] = {CUENTA[n]: json.dumps({"tipo": "usuario", "nombre": n}) for n in aprobadas}
        if permisos:
            self.hashes[contrato.PERMISOS] = {CUENTA[n]: "prefijo" for n in permisos}

    def transacciones(self):
        """Las claves tx:<id> que hay: lo que el backend escribió."""
        return {clave for clave in self.hashes if clave.startswith("tx:")}

    def hmget(self, clave, campos):
        if self.falla:
            raise redis.ConnectionError("Redis caído")
        return [self.hashes.get(clave, {}).get(campo) for campo in campos]

    def hset(self, clave, mapping):
        if self.falla:
            raise redis.ConnectionError("Redis caído")
        self.hashes.setdefault(clave, {}).update({k: str(v) for k, v in mapping.items()})

    def hgetall(self, clave):
        if self.falla:
            raise redis.ConnectionError("Redis caído")
        return dict(self.hashes.get(clave, {}))

    def hget(self, clave, campo):
        return self.hashes.get(clave, {}).get(campo)

    def delete(self, *claves):
        for clave in claves:
            self.hashes.pop(clave, None)

    def pipeline(self, transaction=True):
        return TuberiaFalsa(self)

    def register_script(self, script):
        """Sólo LUA_CREAR: las altas (BC-58) se prueban contra Redis de verdad, en test_altas.py."""
        if script == backend.LUA_CREAR:
            return self._crear
        return lambda **_: pytest.fail("este Redis falso sólo sabe LUA_CREAR")

    def _crear(self, keys, args):
        """LUA_CREAR: si alguna clave ya existe devuelve sus índices (desde 1); si no, las crea todas."""
        if self.falla:
            raise redis.ConnectionError("Redis caído")
        repetidas = [i + 1 for i, clave in enumerate(keys) if clave in self.hashes]
        if repetidas:
            return repetidas
        n, campos = int(args[0]), args[1:]
        for i, clave in enumerate(keys):
            pares = campos[i * 2 * n:(i + 1) * 2 * n]
            self.hashes[clave] = dict(zip(pares[::2], pares[1::2]))
        return []


class TuberiaFalsa:
    """Junta los HSET y los aplica todos en execute(), como un pipeline de Redis."""

    def __init__(self, redis_falso):
        self.redis, self.pendientes = redis_falso, []

    def hset(self, clave, mapping):
        self.pendientes.append(lambda: self.redis.hset(clave, mapping=mapping) or 1)

    def hmget(self, clave, campos):
        self.pendientes.append(lambda: self.redis.hmget(clave, campos))

    def execute(self):
        return [pendiente() for pendiente in self.pendientes]


class PublicadorFalso:
    def __init__(self, falla_desde=None):
        self.publicados, self.falla_desde = [], falla_desde

    def publicar(self, cola, mensaje):
        if self.falla_desde is not None and len(self.publicados) + 1 >= self.falla_desde:
            raise ConnectionError("RabbitMQ caído")
        self.publicados.append((cola, mensaje))


def cliente(redis=None, publicador=None, lote_maximo=1000, organizador=CUENTA["organizador"]):
    app = web.crear_app("backend", {})
    backend.agregar_rutas(app, redis or RedisFalso(), publicador or PublicadorFalso(), cola=COLA,
                          lote_maximo=lote_maximo, organizador=organizador)
    return TestClient(app)


def test_post_firmado_encola_y_queda_recibida():
    redis, publicador = RedisFalso(), PublicadorFalso()
    tx = firmada("alice", "bob", 10)
    respuesta = cliente(redis, publicador).post("/api/transacciones", json=tx)

    assert respuesta.status_code == 202
    assert respuesta.json() == {"id": contrato.id_transaccion(tx)}
    [(cola, mensaje)] = publicador.publicados
    assert cola == COLA and mensaje == tx
    guardada = redis.hashes[f"tx:{tx['id']}"]
    assert guardada["estado"] == "recibida"
    assert (guardada["origen"], guardada["destino"], guardada["monto"], guardada["nonce"], guardada["firma"]) == (
        CUENTA["alice"], CUENTA["bob"], "10", "1", tx["firma"])


def test_una_transferencia_de_una_cuenta_sin_alta_da_403_y_no_guarda_nada():
    """BC-94: la prueba del profesor. Una clave recién generada firma una transferencia: antes daba 202 y dejaba
    tx:<id> para siempre; ahora el backend la corta antes de escribir en Redis o en la cola."""
    redis, publicador = RedisFalso(), PublicadorFalso()
    tx = firmada("nueva", "bob", 10)

    respuesta = cliente(redis, publicador).post("/api/transacciones", json=tx)

    assert respuesta.status_code == 403
    assert respuesta.json() == {"error": "cuenta no aprobada"}
    assert redis.transacciones() == set() and publicador.publicados == []


def test_el_id_lo_calcula_el_backend_aunque_no_venga():
    publicador = PublicadorFalso()
    tx = firmada("alice", "bob", 10)
    sin_id = {k: v for k, v in tx.items() if k != "id"}
    assert cliente(publicador=publicador).post("/api/transacciones", json=sin_id).json() == {"id": tx["id"]}


def test_la_misma_transaccion_reenviada_da_409_y_no_se_encola_dos_veces():
    redis, publicador = RedisFalso(), PublicadorFalso()
    c, tx = cliente(redis, publicador), firmada("alice", "bob", 10)
    assert c.post("/api/transacciones", json=tx).status_code == 202

    respuesta = c.post("/api/transacciones", json=tx)

    assert respuesta.status_code == 409
    assert respuesta.json() == {"error": backend.REENVIO, "id": tx["id"]}
    assert len(publicador.publicados) == 1


def cambiada(**cambios):
    return {**firmada("alice", "bob", 10), **cambios}


@pytest.mark.parametrize("cuerpo, motivo", [
    ({k: v for k, v in firmada("alice", "bob", 10).items() if k != "origen"}, "origen"),
    (cambiada(origen="alice"), "origen"),
    (cambiada(monto=0), "monto"),
    (cambiada(monto=1.5), "monto"),
    (cambiada(nonce=0), "nonce"),
    (cambiada(firma="ab" * 64), "firma"),
    (cambiada(monto=11), "firma"),                       # el monto se cambió después de firmar
    (cambiada(destino=CUENTA["carol"]), "firma"),
    (contrato.transaccion(PRIVADA["bob"], CUENTA["carol"], 10, 1) | {"origen": CUENTA["alice"]}, "firma"),
    (cambiada(id="0" * 32), "id"),
    (firmada("alice", "alice", 10), "distintos"),
    ([firmada("alice", "bob", 10)], "objeto"),
])
def test_formato_o_firma_invalidos_dan_400_con_el_motivo_y_no_encola(cuerpo, motivo):
    redis, publicador = RedisFalso(), PublicadorFalso()
    respuesta = cliente(redis, publicador).post("/api/transacciones", json=cuerpo)

    assert respuesta.status_code == 400
    assert any(motivo in error for error in respuesta.json()["errores"]), respuesta.json()
    assert publicador.publicados == [] and redis.transacciones() == set()


def test_una_carga_del_organizador_entra_con_su_id():
    redis, publicador = RedisFalso(), PublicadorFalso()
    tx = carga(1, "alice", 500)

    respuesta = cliente(redis, publicador).post("/api/transacciones", json=tx)

    assert respuesta.status_code == 202 and respuesta.json() == {"id": "carga-1"}
    assert publicador.publicados == [(COLA, tx)]
    assert redis.hashes["tx:carga-1"]["origen"] == contrato.EMISION


def test_una_carga_firmada_por_otro_o_sin_organizador_da_400():
    falsa = contrato.carga(PRIVADA["alice"], 1, CUENTA["alice"], 500, 1793836800)
    assert cliente().post("/api/transacciones", json=falsa).status_code == 400
    respuesta = cliente(organizador="").post("/api/transacciones", json=carga(1, "alice", 500))
    assert respuesta.status_code == 400
    assert "ORGANIZADOR_CLAVE" in respuesta.json()["errores"][0]


def test_otra_carga_con_el_mismo_numero_da_409():
    c = cliente()
    assert c.post("/api/transacciones", json=carga(1, "alice", 500)).status_code == 202
    assert c.post("/api/transacciones", json=carga(1, "bob", 900)).status_code == 409


def test_json_mal_formado_da_400():
    respuesta = cliente().post("/api/transacciones", content=b"{no es json",
                               headers={"content-type": "application/json"})
    assert respuesta.status_code == 400
    assert respuesta.json()["errores"]


def test_si_rabbitmq_falla_da_503_y_no_queda_recibida():
    redis, publicador = RedisFalso(), PublicadorFalso(falla_desde=1)
    respuesta = cliente(redis, publicador).post("/api/transacciones", json=firmada("alice", "bob", 10))

    assert respuesta.status_code == 503
    assert redis.transacciones() == set()


def test_si_redis_falla_da_503_y_no_encola():
    redis, publicador = RedisFalso(falla=True), PublicadorFalso()
    respuesta = cliente(redis, publicador).post("/api/transacciones", json=firmada("alice", "bob", 10))

    assert respuesta.status_code == 503
    assert publicador.publicados == []


def contadores(c):
    """{resultado: valor} de blockchain_tx_recibidas_total, leído por /metrics."""
    valores = {}
    for familia in text_string_to_metric_families(c.get("/metrics").text):
        for muestra in familia.samples:
            if muestra.name == "blockchain_tx_recibidas_total":
                valores[muestra.labels["resultado"]] = muestra.value
    return valores


def test_la_metrica_cuenta_aceptadas_invalidas_repetidas_y_errores():
    publicador = PublicadorFalso()
    c, billetera = cliente(publicador=publicador), Billetera()
    antes = contadores(c)
    tx = billetera("alice", "bob", 10)
    c.post("/api/transacciones", json=tx)
    c.post("/api/transacciones", json=tx)
    c.post("/api/transacciones", json=cambiada(monto=0))
    publicador.falla_desde = 1
    c.post("/api/transacciones", json=billetera("alice", "bob", 10))
    despues = contadores(c)

    for resultado in ("aceptada", "invalida", "repetida", "error"):
        assert despues[resultado] - antes.get(resultado, 0) == 1, resultado


def test_si_redis_falla_una_consulta_da_503():
    respuesta = cliente(RedisFalso(falla=True)).get("/api/transacciones/abc")
    assert respuesta.status_code == 503


# ── Ticket 04: lote de transacciones ──

def lote(*montos, origen="alice"):
    billetera = Billetera()
    return [billetera(origen, "bob", monto) for monto in montos]


def test_lote_valido_encola_todas_en_orden():
    redis, publicador = RedisFalso(), PublicadorFalso()
    cuerpo = lote(1, 2, 3)
    respuesta = cliente(redis, publicador).post("/api/transacciones/lote", json=cuerpo)

    assert respuesta.status_code == 202
    ids = respuesta.json()["ids"]
    assert ids == [tx["id"] for tx in cuerpo]
    assert [m["id"] for _, m in publicador.publicados] == ids
    assert [m["nonce"] for _, m in publicador.publicados] == [1, 2, 3]
    assert all(redis.hashes[f"tx:{id_}"]["estado"] == "recibida" for id_ in ids)


def test_lote_con_una_invalida_se_rechaza_entero_con_los_errores_por_indice():
    redis, publicador = RedisFalso(), PublicadorFalso()
    cuerpo = lote(1, 2, 3)
    cuerpo[1] = {**cuerpo[1], "monto": 99}              # firma que ya no corresponde
    cuerpo.append(firmada("alice", "alice", 1, nonce=4))
    respuesta = cliente(redis, publicador).post("/api/transacciones/lote", json=cuerpo)

    assert respuesta.status_code == 400
    errores = respuesta.json()["errores"]
    assert set(errores) == {"1", "3"}
    assert any("firma" in e for e in errores["1"]) and any("distintos" in e for e in errores["3"])
    assert publicador.publicados == [] and redis.transacciones() == set()


def test_lote_con_una_repetida_da_409_y_no_entra_ninguna():
    redis, publicador = RedisFalso(), PublicadorFalso()
    c = cliente(redis, publicador)
    cuerpo = lote(1, 2, 3)
    assert c.post("/api/transacciones", json=cuerpo[1]).status_code == 202

    respuesta = c.post("/api/transacciones/lote", json=cuerpo)

    assert respuesta.status_code == 409
    assert respuesta.json() == {"errores": {"1": [backend.REENVIO]}}
    assert len(publicador.publicados) == 1 and redis.transacciones() == {f"tx:{cuerpo[1]['id']}"}


def test_lote_con_la_misma_transaccion_dos_veces_da_409():
    publicador = PublicadorFalso()
    tx = firmada("alice", "bob", 1)
    respuesta = cliente(publicador=publicador).post("/api/transacciones/lote", json=[tx, tx])
    assert respuesta.status_code == 409 and respuesta.json() == {"errores": {"1": [backend.REENVIO]}}
    assert publicador.publicados == []


@pytest.mark.parametrize("cuerpo", [[], {"origen": "alice"}, "texto"])
def test_lote_vacio_o_que_no_es_lista_da_400(cuerpo):
    assert cliente().post("/api/transacciones/lote", json=cuerpo).status_code == 400


def test_lote_mas_grande_que_el_maximo_da_413():
    publicador = PublicadorFalso()
    respuesta = cliente(publicador=publicador, lote_maximo=2).post("/api/transacciones/lote", json=lote(1, 2, 3))
    assert respuesta.status_code == 413
    assert publicador.publicados == []


def test_si_la_publicacion_se_corta_a_mitad_del_lote_dice_cuales_quedaron():
    redis, publicador = RedisFalso(), PublicadorFalso(falla_desde=3)
    respuesta = cliente(redis, publicador).post("/api/transacciones/lote", json=lote(1, 2, 3, 4))

    assert respuesta.status_code == 503
    encoladas = respuesta.json()["encoladas"]
    assert encoladas == [m["id"] for _, m in publicador.publicados]
    assert len(encoladas) == 2
    assert redis.transacciones() == {f"tx:{id_}" for id_ in encoladas}


def test_el_lote_cuenta_la_metrica_por_transaccion():
    publicador = PublicadorFalso()
    c = cliente(publicador=publicador)
    antes = contadores(c)
    c.post("/api/transacciones/lote", json=lote(1, 2))                   # 2 aceptadas
    invalido = lote(1, 2, 3, origen="bob")
    invalido[1]["monto"] = 0
    c.post("/api/transacciones/lote", json=invalido)                     # 3 inválidas: el lote entero no entra
    publicador.falla_desde = 4
    c.post("/api/transacciones/lote", json=lote(1, 2, 3, origen="carol"))  # 1 aceptada y 2 con error
    despues = contadores(c)

    delta = {r: despues.get(r, 0) - antes.get(r, 0) for r in ("aceptada", "invalida", "error")}
    assert delta == {"aceptada": 3, "invalida": 3, "error": 2}


def test_una_confirmada_es_un_puntero_y_se_completa_con_el_bloque():
    """BC-70: tx:<id> confirmada guarda sólo {estado, bloque}; la respuesta es la misma de siempre."""
    redis, tx = RedisFalso(), firmada("alice", "bob", 10)
    redis.hashes["bloque:abc"] = {"transactions": json.dumps([{"id": "cb-1-x", "origen": "emision"}, tx])}
    redis.hashes[f"tx:{tx['id']}"] = {"estado": "confirmada", "bloque": "abc"}

    respuesta = cliente(redis).get(f"/api/transacciones/{tx['id']}")

    assert respuesta.status_code == 200
    assert respuesta.json() == {**tx, "estado": "confirmada", "bloque": "abc"}


def test_una_pendiente_sigue_completa_en_tx():
    redis, tx = RedisFalso(), firmada("alice", "bob", 10)
    redis.hashes[f"tx:{tx['id']}"] = {**{k: str(v) for k, v in tx.items() if k != "id"}, "estado": "pendiente"}

    assert cliente(redis).get(f"/api/transacciones/{tx['id']}").json() == {**tx, "estado": "pendiente"}


# ── BC-94: nada en Redis de quien no tiene alta ──

def test_lote_con_un_origen_sin_alta_da_403_con_su_indice_y_no_guarda_ninguna():
    redis, publicador = RedisFalso(), PublicadorFalso()
    cuerpo = lote(1, 2) + lote(5, origen="nueva") + lote(3, origen="carol")

    respuesta = cliente(redis, publicador).post("/api/transacciones/lote", json=cuerpo)

    assert respuesta.status_code == 403
    assert respuesta.json() == {"errores": {"2": ["cuenta no aprobada"]}}
    assert redis.transacciones() == set() and publicador.publicados == []


def alta_delegada(clave):
    """Un alta de minero firmada por `clave` con permiso delegado (como las del escalador, BC-79)."""
    return contrato.alta(PRIVADA[clave], 1, CUENTA["vm-1"], "minero", "minero-cpu-1", FECHA, delegada=True)


def test_un_acta_delegada_de_una_clave_sin_permiso_da_403_y_no_guarda_nada():
    redis, publicador = RedisFalso(), PublicadorFalso()

    respuesta = cliente(redis, publicador).post("/api/transacciones", json=alta_delegada("escalador"))

    assert respuesta.status_code == 403
    assert respuesta.json() == {"error": "clave sin permiso"}
    assert redis.transacciones() == set() and publicador.publicados == []


def test_un_acta_delegada_de_una_clave_con_permiso_entra_aunque_no_tenga_cuenta():
    redis, publicador = RedisFalso(permisos=("escalador",)), PublicadorFalso()
    acta = alta_delegada("escalador")

    respuesta = cliente(redis, publicador).post("/api/transacciones", json=acta)

    assert respuesta.status_code == 202 and respuesta.json() == {"id": acta["id"]}
    assert len(publicador.publicados) == 1


def test_un_acta_del_organizador_entra_sin_mirar_cuentas_ni_permisos():
    redis, publicador = RedisFalso(aprobadas=()), PublicadorFalso()
    acta = contrato.alta(PRIVADA["organizador"], 1, CUENTA["nueva"], "usuario", "nueva", FECHA)

    assert cliente(redis, publicador).post("/api/transacciones", json=acta).status_code == 202


def test_en_el_lote_la_firma_mala_se_informa_antes_que_la_cuenta_sin_alta():
    cuerpo = lote(1) + lote(2, origen="nueva")
    cuerpo[0] = {**cuerpo[0], "monto": 99}

    respuesta = cliente().post("/api/transacciones/lote", json=cuerpo)

    assert respuesta.status_code == 400 and set(respuesta.json()["errores"]) == {"0"}


def test_la_metrica_cuenta_las_que_no_tienen_alta_por_transaccion():
    c = cliente()
    antes = contadores(c)
    c.post("/api/transacciones", json=firmada("nueva", "bob", 1))
    c.post("/api/transacciones/lote", json=lote(1, 2) + lote(3, origen="nueva"))

    assert contadores(c)["no_aprobada"] - antes.get("no_aprobada", 0) == 4
