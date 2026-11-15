import json
from pathlib import Path

import pytest

from comun import contrato
from comun.tests.claves import CUENTA, PRIVADA, firmada

VECTOR = json.loads((Path(__file__).parent.parent / "vector_prueba.json").read_text())
ALICE, BOB, MINERO, ORGANIZADOR = CUENTA["alice"], CUENTA["bob"], CUENTA["minero"], CUENTA["organizador"]


def tx_de_alice(**cambios):
    return {**contrato.transaccion(PRIVADA["alice"], BOB, 10, 1, 1793836830), **cambios}


# ── Vector de prueba ──

def test_vector_de_prueba():
    genesis = contrato.bloque_genesis(VECTOR["genesis"]["saldos"], VECTOR["genesis"]["timestamp"])
    assert genesis["block_hash"] == VECTOR["genesis"]["block_hash"] == VECTOR["tarea"]["previous_hash"]
    tarea, minero = VECTOR["tarea"], VECTOR["minero"]
    bloque = contrato.transacciones_del_bloque(tarea, minero)
    assert bloque[0] == VECTOR["coinbase"] and bloque[1:] == tarea["transacciones"]
    assert contrato.raiz_transacciones(bloque) == VECTOR["raiz"]
    texto = contrato.texto_para(tarea, minero)
    assert texto == VECTOR["texto"]
    hash_ = contrato.hash_bloque(texto, VECTOR["nonce"])
    assert hash_ == VECTOR["block_hash"] and contrato.cumple(hash_, tarea["prefijo"])


def test_las_transacciones_del_vector_estan_bien_firmadas():
    for tx in VECTOR["tarea"]["transacciones"]:
        assert contrato.validar_transaccion_firmada(tx) == []


def test_las_claves_de_prueba_son_las_del_vector():
    assert {n: CUENTA[n] for n in VECTOR["cuentas"]} == VECTOR["cuentas"]


# ── Cuentas y firmas ──

@pytest.mark.parametrize("valor, valida", [
    (ALICE, True), ("0" * 64, True), (ALICE.upper(), False), (ALICE[:-1], False), (ALICE + "0", False),
    ("emision", False), ("alice", False), (None, False), (7, False),
])
def test_cuenta_valida(valor, valida):
    assert contrato.cuenta_valida(valor) is valida


def test_una_clave_nueva_firma_y_verifica():
    privada, publica = contrato.nueva_clave()
    assert contrato.cuenta_valida(publica) and contrato.clave_publica(privada) == publica
    firma = contrato.firmar(privada, "hola")
    assert contrato.verificar_firma(publica, "hola", firma)
    assert not contrato.verificar_firma(publica, "chau", firma)
    assert not contrato.verificar_firma(ALICE, "hola", firma)


@pytest.mark.parametrize("cuenta, firma", [("no-es-clave", "0" * 128), (ALICE, "0" * 127), (ALICE, None), (ALICE, "z" * 128)])
def test_verificar_firma_mal_formada_da_false_sin_tirar(cuenta, firma):
    assert contrato.verificar_firma(cuenta, "hola", firma) is False


# ── Transacción firmada ──

def test_la_misma_transaccion_reenviada_da_el_mismo_id():
    assert tx_de_alice()["id"] == tx_de_alice()["id"]
    assert contrato.transaccion(PRIVADA["alice"], BOB, 10, 2, 1793836830)["id"] != tx_de_alice()["id"]


@pytest.mark.parametrize("cambio", [{"monto": 11}, {"destino": CUENTA["carol"]}, {"nonce": 2}, {"timestamp": 1}])
def test_cambiar_un_campo_rompe_la_firma(cambio):
    assert any("firma" in e for e in contrato.validar_transaccion_firmada(tx_de_alice(**cambio)))


def test_firmada_por_otro_no_vale():
    ajena = contrato.transaccion(PRIVADA["bob"], CUENTA["carol"], 10, 1, 1)
    assert contrato.validar_transaccion_firmada({**ajena, "origen": ALICE})


def test_el_id_tiene_que_ser_el_hash():
    assert contrato.validar_transaccion_firmada(tx_de_alice(id="otro")) == ["id: no es el hash de la transacción"]
    sin_id = tx_de_alice()
    del sin_id["id"]
    assert contrato.validar_transaccion_firmada(sin_id) == []


@pytest.mark.parametrize("cambio, campo", [
    ({"origen": "alice"}, "origen"), ({"destino": "emision"}, "destino"), ({"monto": 0}, "monto"),
    ({"monto": 1.5}, "monto"), ({"monto": True}, "monto"), ({"monto": contrato.MONTO_MAXIMO + 1}, "monto"),
    ({"nonce": 0}, "nonce"), ({"nonce": "1"}, "nonce"), ({"timestamp": -1}, "timestamp"),
    ({"firma": "ab"}, "firma"), ({"destino": ALICE}, "distintos"),
])
def test_transaccion_firmada_invalida(cambio, campo):
    errores = contrato.validar_transaccion_firmada(tx_de_alice(**cambio))
    assert any(campo in e for e in errores), errores


def test_cuerpo_que_no_es_objeto():
    assert contrato.validar_transaccion_firmada([1, 2]) == ["el cuerpo tiene que ser un objeto JSON"]


# ── Emisión ──

def test_carga_firmada_por_el_organizador():
    tx = contrato.carga(PRIVADA["organizador"], 3, ALICE, 500, 1793836800)
    assert tx["id"] == "carga-3" and tx["origen"] == contrato.EMISION and tx["nonce"] == 3
    assert contrato.validar_carga(tx, ORGANIZADOR) == []
    assert contrato.validar_carga(tx, ALICE) == ["firma: no es la del organizador"]
    assert contrato.validar_carga({**tx, "monto": 501}, ORGANIZADOR) == ["firma: no es la del organizador"]
    assert any("id" in e for e in contrato.validar_carga({**tx, "id": "carga-4"}, ORGANIZADOR))


def test_carga_firmada_por_un_usuario_no_vale():
    falsa = contrato.carga(PRIVADA["alice"], 1, ALICE, 10**6, 1)
    assert contrato.validar_carga(falsa, ORGANIZADOR)


def test_coinbase_y_participacion_tienen_id_fijo_y_sin_firma():
    cb = contrato.coinbase(7, MINERO, 40, 1)
    part = contrato.participacion(2, MINERO, 15, 1)
    assert (cb["id"], part["id"]) == (f"cb-7-{MINERO}", f"part-2-{MINERO}")
    assert cb["firma"] == part["firma"] == "" and contrato.es_emision(cb) and contrato.es_emision(part)
    assert contrato.serializar_transaccion(cb) == f"cb-7-{MINERO}:emision:{MINERO}:40:0:1:"


# ── Bloque ──

def test_el_texto_depende_del_minero():
    tarea, minero = VECTOR["tarea"], VECTOR["minero"]
    assert contrato.texto_para(tarea, ALICE) == VECTOR["texto_de_alice"] != contrato.texto_para(tarea, minero)
    # el nonce que encontró el minero no le sirve a otro
    assert not contrato.cumple(contrato.hash_bloque(contrato.texto_para(tarea, ALICE), VECTOR["nonce"]), tarea["prefijo"])


def test_la_coinbase_paga_segun_el_prefijo_de_la_tarea():
    tarea = {**VECTOR["tarea"], "prefijo": "000000"}
    assert contrato.transacciones_del_bloque(tarea, MINERO)[0]["monto"] == contrato.recompensa("000000") == 60


def test_el_texto_del_bloque_mide_siempre_77():
    muchas = [tx_de_alice()] * 1000
    tarea = {"previous_hash": contrato.CEROS, "timestamp": 1793836860, "altura": 123456, "prefijo": "0" * 8,
             "transacciones": muchas}
    assert len(contrato.texto_para(tarea, MINERO)) == 77


def test_cambiar_una_transaccion_cambia_el_hash():
    tarea = VECTOR["tarea"]
    otra = {**tarea, "transacciones": [tx_de_alice(), tarea["transacciones"][1]]}
    assert contrato.hash_bloque(contrato.texto_para(otra, VECTOR["minero"]), VECTOR["nonce"]) != VECTOR["block_hash"]


def test_el_genesis_reparte_desde_emision_con_altura_0():
    genesis = contrato.bloque_genesis({ALICE: 1}, 1)
    assert genesis["altura"] == 0 and genesis["previous_hash"] == contrato.CEROS
    assert genesis["transactions"] == [contrato._emision(f"genesis-{ALICE}", ALICE, 1, 0, 1)]


def test_recompensa_crece_con_el_prefijo():
    assert contrato.recompensa("000") == 30
    assert contrato.recompensa("00000", base=2) == 10


# ── Shares y épocas ──

def test_un_share_tiene_un_caracter_menos_de_prefijo():
    assert contrato.prefijo_share("00000") == "0000"
    assert contrato.es_share("0000f" + "1" * 27, "00000") and not contrato.es_share("000f" + "1" * 28, "00000")
    assert contrato.prefijo_share("0") is None and not contrato.es_share("0" * 32, "0")


@pytest.mark.parametrize("altura, epoca", [(0, 0), (9, 0), (10, 1), (19, 1), (20, 2)])
def test_epocas_de_10_bloques(altura, epoca):
    assert contrato.epoca(altura) == epoca


# ── Pedidos firmados de los mineros ──

CUERPO = b'{"tarea": "t1", "rango": 3, "nonce": 54068}'


def test_pedido_bien_firmado():
    encabezados = contrato.firmar_pedido(PRIVADA["minero"], "post", "/results", CUERPO, tiempo=1000)
    assert contrato.verificar_pedido(encabezados, "POST", "/results", CUERPO, ahora=1010) == (MINERO, None)
    minusculas = {k.lower(): v for k, v in encabezados.items()}  # como llegan en HTTP/2
    assert contrato.verificar_pedido(minusculas, "POST", "/results", CUERPO, ahora=1010) == (MINERO, None)


@pytest.mark.parametrize("metodo, ruta, cuerpo, ahora, motivo", [
    ("POST", "/results", CUERPO, 1031, "corrido"),            # pedido viejo: no se puede repetir
    ("POST", "/results", CUERPO, 969, "corrido"),
    ("POST", "/results", CUERPO.replace(b"3", b"4"), 1000, "X-Firma"),  # otro cuerpo
    ("POST", "/keepalive", CUERPO, 1000, "X-Firma"),          # otra ruta
    ("GET", "/results", CUERPO, 1000, "X-Firma"),
])
def test_pedido_alterado_o_viejo(metodo, ruta, cuerpo, ahora, motivo):
    encabezados = contrato.firmar_pedido(PRIVADA["minero"], "POST", "/results", CUERPO, tiempo=1000)
    minero, error = contrato.verificar_pedido(encabezados, metodo, ruta, cuerpo, ahora=ahora)
    assert minero is None and motivo in error


@pytest.mark.parametrize("cambio, motivo", [
    ({"X-Minero": ALICE}, "X-Firma"), ({"X-Minero": "gpu-tomas"}, "X-Minero"), ({"X-Tiempo": "²"}, "X-Tiempo"),
    ({"X-Firma": None}, "X-Firma"),
])
def test_pedido_con_encabezados_malos(cambio, motivo):
    encabezados = {**contrato.firmar_pedido(PRIVADA["minero"], "GET", "/tasks/next", tiempo=1000), **cambio}
    minero, error = contrato.verificar_pedido(encabezados, "GET", "/tasks/next", ahora=1000)
    assert minero is None and motivo in error


# ── Nombres, reparto por shares y lo que se lee de Redis ──

@pytest.mark.parametrize("nombre, valido", [
    ("gpu-tomas", True), ("cpu_vm_3", True), ("minero-gpu-0", True), ("alice.b", True),
    ("", False), ("vm:1", False), ("x" * 33, False), (7, False),
])
def test_nombre_de_una_cuenta_o_un_minero(nombre, valido):
    assert contrato.nombre_valido(nombre) is valido


def test_el_reparto_es_proporcional_a_los_shares():
    assert contrato.repartir(100, {"a": 30, "b": 10}, {}) == {"a": 75, "b": 25}
    assert contrato.repartir(100, {}, {}) == {} and contrato.repartir(100, {"a": 0}, {}) == {}


def test_la_racha_suma_10_por_ciento_por_epoca_hasta_50():
    assert [contrato.peso_participacion(10, r) for r in (1, 2, 3, 6, 7, 20)] == [100, 110, 120, 150, 150, 150]
    # con la misma cantidad de shares, el que lleva 6 épocas seguidas cobra 1,5 veces más
    assert contrato.repartir(250, {"a": 10, "b": 10}, {"a": 6, "b": 1}) == {"a": 150, "b": 100}


def test_los_montos_son_enteros_y_el_resto_se_pierde():
    montos = contrato.repartir(100, {"a": 1, "b": 1, "c": 1}, {})
    assert montos == {"a": 33, "b": 33, "c": 33}


def test_primer_bloque_de_cada_epoca():
    assert [a for a in range(0, 35) if contrato.es_primer_bloque_de_epoca(a)] == [10, 20, 30]


def test_transaccion_y_tarea_como_las_devuelve_redis():
    tx = firmada("alice", "bob", 7, nonce=3)
    texto = {k: str(v) for k, v in tx.items() if k != "id"}
    assert contrato.transaccion_guardada(tx["id"], {**texto, "estado": "pendiente"}) == tx
    tarea = VECTOR["tarea"]
    guardada = {"previous_hash": tarea["previous_hash"], "timestamp": str(tarea["timestamp"]),
                "altura": str(tarea["altura"]), "prefijo": tarea["prefijo"],
                "transacciones": json.dumps(tarea["transacciones"]), "estado": "en_curso"}
    assert contrato.tarea_guardada(guardada) == tarea
    assert contrato.texto_para(contrato.tarea_guardada(guardada), VECTOR["minero"]) == VECTOR["texto"]
