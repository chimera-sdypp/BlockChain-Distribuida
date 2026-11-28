import json
from pathlib import Path

import pytest

from comun import contrato
from comun.tests.claves import CUENTA, FECHA, PRIVADA, firmada, cadena_actual

VECTOR = json.loads((Path(__file__).parent.parent / "vector_prueba.json").read_text())
ALICE, BOB, MINERO, ORGANIZADOR = CUENTA["alice"], CUENTA["bob"], CUENTA["minero"], CUENTA["organizador"]


def tx_de_alice(**cambios):
    return {**contrato.transaccion(PRIVADA["alice"], BOB, 10, 1, 1793836830, cadena=cadena_actual()), **cambios}


# ── Vector de prueba ──

def test_vector_de_prueba():
    g = VECTOR["genesis"]
    genesis = contrato.bloque_genesis(g["saldos"], g["timestamp"], organizador=g["organizador"], nombres=g["nombres"],
                                      prefijo_minimo=g["prefijo_minimo"])
    assert VECTOR["cadena"] == genesis["block_hash"] and contrato.prefijo_minimo_de(genesis) == 4
    assert genesis["block_hash"] == g["block_hash"] == VECTOR["tarea"]["previous_hash"]
    assert genesis == g["bloque"]
    tarea, minero = VECTOR["tarea"], VECTOR["minero"]
    assert tarea["raiz_estado"] == VECTOR["estado_genesis"]["raiz"]
    bloque = contrato.transacciones_del_bloque(tarea, minero)
    assert bloque[0] == VECTOR["coinbase"] and bloque[1:] == tarea["transacciones"]
    assert contrato.raiz_transacciones(bloque) == VECTOR["raiz_transacciones"]
    assert contrato.raiz_bloque(bloque, tarea["raiz_estado"]) == VECTOR["raiz"]
    texto = contrato.texto_para(tarea, minero)
    assert texto == VECTOR["texto"] and len(texto) == 77
    hash_ = contrato.hash_bloque(texto, VECTOR["nonce"])
    assert hash_ == VECTOR["block_hash"] and contrato.cumple(hash_, tarea["prefijo"])
    assert contrato.texto_de_bloque(VECTOR["bloque"]) == texto


def test_las_transacciones_del_vector_estan_bien_firmadas():
    transferencias, acta = VECTOR["tarea"]["transacciones"][:2], VECTOR["tarea"]["transacciones"][2]
    for tx in transferencias:
        assert contrato.validar_transaccion_firmada(tx, VECTOR["cadena"]) == []
        assert contrato.mensaje_transaccion(tx, VECTOR["cadena"]).startswith(VECTOR["cadena"] + ":")
    assert contrato.mensaje_transaccion(transferencias[0], VECTOR["cadena"]) == VECTOR["mensaje_transferencia"]
    assert contrato.mensaje_acta(acta, VECTOR["cadena"]) == VECTOR["mensaje_alta"]
    assert contrato.tipo_de(acta) == "alta" and contrato.validar_acta(acta, CUENTA["organizador"], VECTOR["cadena"]) == []


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
    assert contrato.transaccion(PRIVADA["alice"], BOB, 10, 2, 1793836830, cadena=cadena_actual())["id"] != tx_de_alice()["id"]


@pytest.mark.parametrize("cambio", [{"monto": 11}, {"destino": CUENTA["carol"]}, {"nonce": 2}, {"timestamp": 1}])
def test_cambiar_un_campo_rompe_la_firma(cambio):
    assert any("firma" in e for e in contrato.validar_transaccion_firmada(tx_de_alice(**cambio), cadena=cadena_actual()))


def test_firmada_por_otro_no_vale():
    ajena = contrato.transaccion(PRIVADA["bob"], CUENTA["carol"], 10, 1, 1, cadena=cadena_actual())
    assert contrato.validar_transaccion_firmada({**ajena, "origen": ALICE}, cadena=cadena_actual())


def test_el_id_tiene_que_ser_el_hash():
    assert contrato.validar_transaccion_firmada(tx_de_alice(id="otro"), cadena=cadena_actual()) == ["id: no es el hash de la transacción"]
    sin_id = tx_de_alice()
    del sin_id["id"]
    assert contrato.validar_transaccion_firmada(sin_id, cadena=cadena_actual()) == []


@pytest.mark.parametrize("cambio, campo", [
    ({"origen": "alice"}, "origen"), ({"destino": "emision"}, "destino"), ({"monto": 0}, "monto"),
    ({"monto": 1.5}, "monto"), ({"monto": True}, "monto"), ({"monto": contrato.MONTO_MAXIMO + 1}, "monto"),
    ({"nonce": 0}, "nonce"), ({"nonce": "1"}, "nonce"), ({"timestamp": -1}, "timestamp"),
    ({"firma": "ab"}, "firma"), ({"destino": ALICE}, "distintos"),
])
def test_transaccion_firmada_invalida(cambio, campo):
    errores = contrato.validar_transaccion_firmada(tx_de_alice(**cambio), cadena=cadena_actual())
    assert any(campo in e for e in errores), errores


def test_cuerpo_que_no_es_objeto():
    assert contrato.validar_transaccion_firmada([1, 2], cadena=cadena_actual()) == ["el cuerpo tiene que ser un objeto JSON"]


# ── Emisión ──

def test_carga_firmada_por_el_organizador():
    tx = contrato.carga(PRIVADA["organizador"], 3, ALICE, 500, 1793836800, cadena=cadena_actual())
    assert tx["id"] == "carga-3" and tx["origen"] == contrato.EMISION and tx["nonce"] == 3
    assert contrato.validar_carga(tx, ORGANIZADOR, cadena=cadena_actual()) == []
    assert contrato.validar_carga(tx, ALICE, cadena=cadena_actual()) == ["firma: no es la del organizador"]
    assert contrato.validar_carga({**tx, "monto": 501}, ORGANIZADOR, cadena=cadena_actual()) == ["firma: no es la del organizador"]
    assert any("id" in e for e in contrato.validar_carga({**tx, "id": "carga-4"}, ORGANIZADOR, cadena=cadena_actual()))


def test_carga_firmada_por_un_usuario_no_vale():
    falsa = contrato.carga(PRIVADA["alice"], 1, ALICE, 10**6, 1, cadena=cadena_actual())
    assert contrato.validar_carga(falsa, ORGANIZADOR, cadena=cadena_actual())


def test_coinbase_y_participacion_tienen_id_fijo_y_sin_firma():
    cb = contrato.coinbase(7, MINERO, 40, 1)
    part = contrato.participacion(2, MINERO, 15, 1)
    assert (cb["id"], part["id"]) == (f"cb-7-{MINERO}", f"part-2-{MINERO}")
    assert cb["firma"] == part["firma"] == "" and contrato.es_emision(cb) and contrato.es_emision(part)
    assert contrato.serializar_transaccion(cb) == f"cb-7-{MINERO}:emision:{MINERO}:40:0:1::"  # datos y firma vacíos


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
             "transacciones": muchas, "raiz_estado": "f" * 32}
    assert len(contrato.texto_para(tarea, MINERO)) == 77


def test_cambiar_una_transaccion_cambia_el_hash():
    tarea = VECTOR["tarea"]
    otra = {**tarea, "transacciones": [tx_de_alice(), tarea["transacciones"][1]]}
    assert contrato.hash_bloque(contrato.texto_para(otra, VECTOR["minero"]), VECTOR["nonce"]) != VECTOR["block_hash"]


def test_el_genesis_reparte_desde_emision_con_altura_0():
    genesis = contrato.bloque_genesis({ALICE: 1}, 1)
    assert genesis["altura"] == 0 and genesis["previous_hash"] == contrato.CEROS
    assert genesis["raiz_estado"] == contrato.RAIZ_VACIA
    assert genesis["transactions"] == [contrato.reglas_genesis(1, 1),
                                       contrato._emision(f"genesis-{ALICE}", ALICE, 1, 0, 1, datos="usuario:")]


def test_el_genesis_v3_trae_al_organizador_primero_y_el_nombre_de_cada_cuenta():
    organizador = CUENTA["organizador"]
    genesis = contrato.bloque_genesis({ALICE: 5}, 1, organizador=organizador, nombres={ALICE: "alice"})
    assert [tx["id"] for tx in genesis["transactions"]] == [f"organizador-{organizador}", "reglas", f"genesis-{ALICE}"]
    assert genesis["transactions"][2]["datos"] == "usuario:alice"


# ── v4 (BC-96): las reglas del génesis y lo firmado atado a una cadena ──

def test_el_genesis_v4_trae_el_piso_de_prefijo():
    genesis = contrato.bloque_genesis({}, 1, organizador=ORGANIZADOR, prefijo_minimo=6)
    assert genesis["transactions"][1] == contrato.reglas_genesis(6, 1)
    assert genesis["transactions"][1]["datos"] == "prefijo_minimo:6" and contrato.tipo_de(genesis["transactions"][1]) == "reglas"
    assert contrato.prefijo_minimo_de(genesis) == 6
    assert contrato.prefijo_minimo_de({"transactions": []}) == 1  # sin reglas: 1
    for datos in ("prefijo_minimo:0", "prefijo_minimo:33", "prefijo_minimo:06", "piso:6", "prefijo_minimo:"):
        assert contrato.leer_reglas({"datos": datos}) is None


def test_un_genesis_nuevo_cambia_el_id_de_la_cadena():
    uno = contrato.bloque_genesis({ALICE: 1}, 1, organizador=ORGANIZADOR)
    otro = contrato.bloque_genesis({ALICE: 1}, 2, organizador=ORGANIZADOR)  # se reinició un segundo después
    assert uno["block_hash"] != otro["block_hash"]


def test_lo_firmado_para_otra_cadena_no_vale_en_esta():
    """BC-99: al reiniciar la cadena los nonces vuelven a 1; lo firmado antes no se puede volver a mandar."""
    vieja, nueva = "a" * 32, "b" * 32
    tx = contrato.transaccion(PRIVADA["alice"], BOB, 10, 1, FECHA, cadena=vieja)
    assert contrato.validar_transaccion_firmada(tx, vieja) == []
    assert contrato.validar_transaccion_firmada(tx, nueva)[0].startswith("firma:")
    assert contrato.transaccion(PRIVADA["alice"], BOB, 10, 1, FECHA, cadena=nueva)["id"] != tx["id"]
    carga = contrato.carga(PRIVADA["organizador"], 1, ALICE, 10**9, FECHA, cadena=vieja)
    assert contrato.validar_carga(carga, ORGANIZADOR, vieja) == []
    assert contrato.validar_carga(carga, ORGANIZADOR, nueva) == ["firma: no es la del organizador"]
    alta = contrato.alta(PRIVADA["organizador"], 2, ALICE, "usuario", "alice", FECHA, cadena=vieja)
    assert contrato.validar_acta(alta, ORGANIZADOR, nueva) == ["firma: no es la del organizador"]
    pedido = contrato.firmar_pedido(PRIVADA["minero"], "POST", "/hello", b"{}", tiempo=FECHA, cadena=vieja)
    assert contrato.verificar_pedido(pedido, "POST", "/hello", b"{}", ahora=FECHA, cadena=vieja)[1] is None
    assert contrato.verificar_pedido(pedido, "POST", "/hello", b"{}", ahora=FECHA, cadena=nueva)[1].startswith("X-Firma")
    alta_pedida = contrato.pedido_alta(PRIVADA["zoe"], "usuario", "zoe", cadena=vieja)
    assert contrato.validar_pedido_alta(alta_pedida, nueva)[0].startswith("firma:")


def test_sin_genesis_no_se_valida_nada():
    tx = contrato.transaccion(PRIVADA["alice"], BOB, 10, 1, FECHA, cadena="a" * 32)
    for cadena in (None, "", "no-es-un-hash"):
        assert contrato.validar_transaccion_firmada(tx, cadena) == [contrato.SIN_CADENA]
        assert contrato.verificar_pedido({}, "GET", "/", cadena=cadena) == (None, contrato.SIN_CADENA)


# ── Actas (v3, BC-79) ──

def test_un_alta_firmada_por_el_organizador_vale_y_no_sirve_como_carga():
    alta = contrato.alta(PRIVADA["organizador"], 3, ALICE, "minero", "gpu-1", FECHA, cadena=cadena_actual())
    assert alta["id"] == "alta-3" and alta["monto"] == 0 and alta["datos"] == "minero:gpu-1"
    assert contrato.tipo_de(alta) == "alta" and not contrato.es_delegada(alta)
    assert contrato.validar_acta(alta, CUENTA["organizador"], cadena=cadena_actual()) == []
    assert contrato.validar_acta(alta, CUENTA["bob"], cadena=cadena_actual()) == ["firma: no es la del organizador"]
    # La misma firma con el id de una carga no vale: el mensaje del acta empieza con su tipo.
    trucha = {**{k: v for k, v in alta.items() if k != "datos"}, "id": "carga-3", "monto": 1}
    assert contrato.validar_carga(trucha, CUENTA["organizador"], cadena=cadena_actual()) == ["firma: no es la del organizador"]


@pytest.mark.parametrize("cambio, error", [
    ({"datos": "organizador:yo"}, "datos:"), ({"datos": "usuario:"}, "datos:"), ({"monto": 5}, "monto:"),
    ({"id": "alta-9"}, "id:"), ({"destino": "alice"}, "destino:"), ({"datos": "minero:gpu-2"}, "firma:")])
def test_un_alta_mal_formada_o_cambiada_no_vale(cambio, error):
    alta = {**contrato.alta(PRIVADA["organizador"], 3, ALICE, "minero", "gpu-1", FECHA, cadena=cadena_actual()), **cambio}
    assert contrato.validar_acta(alta, CUENTA["organizador"], cadena=cadena_actual())[0].startswith(error)


def test_baja_y_permiso():
    baja = contrato.baja(PRIVADA["organizador"], 4, ALICE, "se fue", FECHA, cadena=cadena_actual())
    permiso = contrato.permiso(PRIVADA["organizador"], 5, CUENTA["bob"], "minero-cpu-", FECHA, cadena=cadena_actual())
    assert contrato.validar_acta(baja, CUENTA["organizador"], cadena=cadena_actual()) == contrato.validar_acta(permiso, CUENTA["organizador"], cadena=cadena_actual()) == []
    assert contrato.validar_acta({**baja, "datos": "a|b"}, CUENTA["organizador"], cadena=cadena_actual())[0].startswith("datos:")
    revoca = contrato.permiso(PRIVADA["organizador"], 6, CUENTA["bob"], "", FECHA, cadena=cadena_actual())
    assert contrato.validar_acta(revoca, CUENTA["organizador"], cadena=cadena_actual()) == []


def test_un_alta_delegada_la_firma_la_clave_con_permiso_y_lleva_su_cuenta_en_el_id():
    alta = contrato.alta(PRIVADA["bob"], 1, ALICE, "minero", "minero-cpu-0", FECHA, delegada=True, cadena=cadena_actual())
    assert alta["id"] == f"alta-{CUENTA['bob']}-1" and alta["origen"] == CUENTA["bob"]
    assert contrato.tipo_de(alta) == "alta" and contrato.es_delegada(alta)
    assert contrato.validar_acta(alta, CUENTA["organizador"], cadena=cadena_actual()) == []
    assert contrato.validar_de_entrada(alta, CUENTA["organizador"], cadena=cadena_actual()) == []
    permiso = contrato.acta(PRIVADA["bob"], "permiso", 2, ALICE, "x", FECHA, delegada=True, cadena=cadena_actual())
    assert contrato.tipo_de(permiso) == "transferencia"  # un permiso delegado no existe: no es un acta


def test_una_transferencia_no_lleva_datos():
    tx = {**firmada("alice", "bob", 1), "datos": "algo"}
    assert "datos: una transferencia no lleva datos (no van firmados)" in contrato.validar_transaccion_firmada(tx, cadena=cadena_actual())


def test_los_tipos_salen_del_id():
    assert [contrato.tipo_de({"id": i, "origen": contrato.EMISION}) for i in
            ("genesis-x", "organizador-x", "cb-1-x", "carga-1", "part-1-x", "shares-1", "alta-1", "baja-1",
             "permiso-1", "otra")] == ["genesis", "organizador", "coinbase", "carga", "participacion", "shares",
                                       "alta", "baja", "permiso", "desconocida"]


# ── Shares publicados (v3, BC-82) ──

def test_la_raiz_de_shares_es_la_lista_ordenada_con_racha():
    detalle = {CUENTA["minero2"]: {"shares": 3, "racha": 2, "monto": 40}, CUENTA["minero"]: {"shares": 5, "racha": 1,
                                                                                           "monto": 60},
               CUENTA["zoe"]: {"shares": 0, "racha": 1, "monto": 0}}
    lista = contrato.lista_shares(detalle)
    assert lista == sorted([f"{CUENTA['minero']}:5:1", f"{CUENTA['minero2']}:3:2"])
    assert contrato.raiz_shares(detalle) == contrato.md5("|".join(lista))
    tx = contrato.shares_epoca(4, 100, contrato.raiz_shares(detalle), FECHA)
    assert tx["id"] == "shares-4" and tx["datos"] == f"100:{contrato.raiz_shares(detalle)}" and tx["monto"] == 0


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
    encabezados = contrato.firmar_pedido(PRIVADA["minero"], "post", "/results", CUERPO, tiempo=1000, cadena=cadena_actual())
    assert contrato.verificar_pedido(encabezados, "POST", "/results", CUERPO, ahora=1010, cadena=cadena_actual()) == (MINERO, None)
    minusculas = {k.lower(): v for k, v in encabezados.items()}  # como llegan en HTTP/2
    assert contrato.verificar_pedido(minusculas, "POST", "/results", CUERPO, ahora=1010, cadena=cadena_actual()) == (MINERO, None)


@pytest.mark.parametrize("metodo, ruta, cuerpo, ahora, motivo", [
    ("POST", "/results", CUERPO, 1031, "corrido"),            # pedido viejo: no se puede repetir
    ("POST", "/results", CUERPO, 969, "corrido"),
    ("POST", "/results", CUERPO.replace(b"3", b"4"), 1000, "X-Firma"),  # otro cuerpo
    ("POST", "/keepalive", CUERPO, 1000, "X-Firma"),          # otra ruta
    ("GET", "/results", CUERPO, 1000, "X-Firma"),
])
def test_pedido_alterado_o_viejo(metodo, ruta, cuerpo, ahora, motivo):
    encabezados = contrato.firmar_pedido(PRIVADA["minero"], "POST", "/results", CUERPO, tiempo=1000, cadena=cadena_actual())
    minero, error = contrato.verificar_pedido(encabezados, metodo, ruta, cuerpo, ahora=ahora, cadena=cadena_actual())
    assert minero is None and motivo in error


@pytest.mark.parametrize("cambio, motivo", [
    ({"X-Minero": ALICE}, "X-Firma"), ({"X-Minero": "gpu-tomas"}, "X-Minero"), ({"X-Tiempo": "²"}, "X-Tiempo"),
    ({"X-Firma": None}, "X-Firma"),
])
def test_pedido_con_encabezados_malos(cambio, motivo):
    encabezados = {**contrato.firmar_pedido(PRIVADA["minero"], "GET", "/tasks/next", tiempo=1000, cadena=cadena_actual()), **cambio}
    minero, error = contrato.verificar_pedido(encabezados, "GET", "/tasks/next", ahora=1000, cadena=cadena_actual())
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
                "transacciones": json.dumps(tarea["transacciones"]), "estado": "en_curso",
                "raiz_estado": tarea["raiz_estado"]}
    assert contrato.tarea_guardada(guardada) == tarea
    assert contrato.texto_para(contrato.tarea_guardada(guardada), VECTOR["minero"]) == VECTOR["texto"]


def test_el_organizador_firma_con_su_encabezado():
    encabezados = contrato.firmar_pedido(PRIVADA["organizador"], "POST", "/api/organizador/baja", CUERPO, tiempo=1000,
                                         quien=contrato.ORGANIZADOR, cadena=cadena_actual())
    assert "X-Minero" not in encabezados
    assert contrato.verificar_pedido(encabezados, "POST", "/api/organizador/baja", CUERPO, ahora=1000,
                                     quien=contrato.ORGANIZADOR, cadena=cadena_actual()) == (CUENTA["organizador"], None)
    # Firmado como minero no sirve como organizador: falta X-Organizador.
    como_minero = contrato.firmar_pedido(PRIVADA["organizador"], "POST", "/api/organizador/baja", CUERPO, tiempo=1000, cadena=cadena_actual())
    cuenta, error = contrato.verificar_pedido(como_minero, "POST", "/api/organizador/baja", CUERPO, ahora=1000,
                                              quien=contrato.ORGANIZADOR, cadena=cadena_actual())
    assert cuenta is None and "X-Organizador" in error


# ── Pedidos de alta (BC-58) ──

def test_pedido_de_alta_bien_firmado():
    pedido = contrato.pedido_alta(PRIVADA["zoe"], "usuario", "zoe", cadena=cadena_actual())
    assert pedido["clave_publica"] == CUENTA["zoe"]
    assert contrato.validar_pedido_alta(pedido, cadena=cadena_actual()) == []
    assert contrato.validar_pedido_alta(contrato.pedido_alta(PRIVADA["zoe"], "minero", "gpu-zoe", cadena=cadena_actual()), cadena=cadena_actual()) == []


@pytest.mark.parametrize("cambio, error", [
    ({"clave_publica": CUENTA["alice"]}, "firma"),   # la firma no corresponde a la clave: no es de quien pide
    ({"nombre": "otra"}, "firma"),
    ({"tipo": "minero"}, "firma"),
    ({"tipo": "organizador"}, "tipo"),               # el organizador no se pide
    ({"nombre": "con espacio"}, "nombre"),
    ({"nombre": ""}, "nombre"),
    ({"clave_publica": "zoe"}, "clave_publica"),
    ({"firma": "ab" * 64}, "firma"),
])
def test_pedido_de_alta_invalido(cambio, error):
    pedido = {**contrato.pedido_alta(PRIVADA["zoe"], "usuario", "zoe", cadena=cadena_actual()), **cambio}
    [motivo] = contrato.validar_pedido_alta(pedido, cadena=cadena_actual())
    assert motivo.startswith(error)


def test_pedido_de_alta_que_no_es_un_objeto():
    assert contrato.validar_pedido_alta(["zoe"], cadena=cadena_actual()) == ["el cuerpo tiene que ser un objeto JSON"]
