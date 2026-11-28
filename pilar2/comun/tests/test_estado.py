"""El estado desde la cadena (BC-80) y lo que verifica comun/estado.py: Merkle, cada regla y los shares (BC-82).

Las cadenas se arman acá, en memoria (sin Redis), minando con prefijo «0»: un bloque cuesta unos pocos MD5.
"""

import json
from pathlib import Path

import pytest

from comun import contrato, estado
from comun.tests.cadena import Cadena, remina
from comun.tests.claves import CUENTA, FECHA, PRIVADA, carga, firmada, cadena_actual

VECTOR = json.loads((Path(__file__).parent.parent / "vector_prueba.json").read_text())
ORG = CUENTA["organizador"]
ALICE, BOB, CAROL, MINERO, MINERO2 = (CUENTA[n] for n in ("alice", "bob", "carol", "minero", "minero2"))
REGLAS = estado.Reglas(organizador=ORG)


# ── Merkle ──

@pytest.mark.parametrize("cantidad", [1, 2, 3, 5, 8, 13])
def test_cada_hoja_se_prueba_con_su_rama(cantidad):
    hojas = [f"hoja-{i}" for i in range(cantidad)]
    hashes = [estado.hash_hoja(h) for h in hojas]
    raiz = estado.raiz_merkle(hashes)
    for i, hoja in enumerate(hojas):
        assert estado.raiz_de_rama(hoja, estado.rama_merkle(hashes, i)) == raiz
    assert estado.raiz_de_rama("otra", estado.rama_merkle(hashes, 0)) != raiz


def test_la_raiz_vacia_y_la_de_una_hoja():
    assert estado.raiz_merkle([]) == contrato.RAIZ_VACIA
    assert estado.raiz_merkle([estado.hash_hoja("a")]) == estado.hash_hoja("a")


def test_el_estado_del_vector_da_su_raiz_y_su_prueba():
    e = estado.Estado()
    assert estado.aplicar_bloque(e, VECTOR["genesis"]["bloque"], REGLAS) == ([], [])
    assert [h for _, h in e.hojas()] == VECTOR["estado_genesis"]["hojas"]
    assert e.raiz() == VECTOR["estado_genesis"]["raiz"] == VECTOR["tarea"]["raiz_estado"]
    prueba = VECTOR["estado_genesis"]["prueba_alice"]
    assert estado.raiz_de_rama(prueba["hoja"], prueba["rama"]) == e.raiz()
    assert estado.aplicar_bloque(e, VECTOR["bloque"], REGLAS) == ([], [])
    assert e.raiz() == VECTOR["estado_bloque"]["raiz"] and e.saldos == VECTOR["estado_bloque"]["saldos"]


def test_la_raiz_incremental_es_la_de_calcular_todo():
    c = Cadena()
    c.agregar([firmada("alice", "bob", 10), carga(1, "carol", 7)])
    c.agregar([firmada("bob", "carol", 5), contrato.alta(PRIVADA["organizador"], 2, CAROL, "usuario", "carol", FECHA, cadena=cadena_actual())])
    e = c.estado
    assert e.raiz() == estado.raiz_estado(e.saldos, e.nonces, e.tipos(), e.permisos)


def test_el_snapshot_vuelve_con_la_misma_raiz_y_se_nota_si_lo_tocan():
    c = Cadena()
    c.agregar([firmada("alice", "bob", 10)])
    datos = c.estado.a_dict()
    assert estado.Estado.desde_dict(json.loads(json.dumps(datos))).raiz() == c.estado.raiz()
    datos["saldos"][ALICE] += 1
    with pytest.raises(ValueError):
        estado.Estado.desde_dict(datos)


# ── Lo que vale ──

def test_una_cadena_con_todo_se_reconstruye():
    c = Cadena()
    c.agregar([firmada("alice", "bob", 30), carga(1, "carol", 20)])
    c.agregar([contrato.alta(PRIVADA["organizador"], 2, CAROL, "usuario", "carol", FECHA, cadena=cadena_actual()),
               firmada("bob", "alice", 5)])
    c.agregar([firmada("carol", "alice", 3, nonce=1), contrato.baja(PRIVADA["organizador"], 3, BOB, "", FECHA, cadena=cadena_actual())])
    e, malos = estado.reconstruir(c.bloques, REGLAS)
    assert malos == [] and e.raiz() == c.estado.raiz() and e.altura == 3
    assert e.saldos[ALICE] == 100 - 30 + 5 + 3 and e.saldos[CAROL] == 17 and BOB not in e.cuentas
    assert e.cuentas[CAROL]["tipo"] == "usuario" and e.nonces[contrato.EMISION] == 3
    assert e.saldos[MINERO] == 3 * contrato.recompensa("0")


def test_el_minero_no_necesita_alta_en_la_cadena_para_cobrar():
    c = Cadena()
    c.agregar([firmada("alice", "bob", 1)], minero=CUENTA["zoe"])
    assert c.estado.saldos[CUENTA["zoe"]] == 10 and CUENTA["zoe"] not in c.estado.cuentas


def test_el_organizador_sale_del_genesis_si_no_se_conoce():
    e = estado.Estado()
    assert estado.aplicar_bloque(e, Cadena().genesis, estado.Reglas()) == ([], [])
    assert e.organizador == ORG and e.cuentas[ORG]["tipo"] == "organizador"


# ── Lo que el operador no puede hacer sin que se note ──

def test_un_saldo_inventado_cambia_la_raiz_y_el_bloque_siguiente_no_vale():
    c = Cadena()
    tocado = c.estado.copia()
    tocado.sumar(ALICE, 1_000_000)
    bloque = c.armar([firmada("alice", "bob", 500_000)], raiz_estado=tocado.raiz())
    problemas = c.verificar(bloque)
    assert any("raíz de estado" in p for p in problemas) and any("no tiene saldo" in p for p in problemas)


def test_un_alta_sin_firma_no_cuenta():
    c = Cadena()
    zoe = CUENTA["zoe"]
    c.agregar([carga(1, "zoe", 50)])  # tiene saldo, pero nunca tuvo un alta
    problemas = c.verificar(c.armar([contrato.transaccion(PRIVADA["zoe"], BOB, 10, 1, FECHA, cadena=cadena_actual())]))
    assert problemas == [f"{contrato.transaccion(PRIVADA['zoe'], BOB, 10, 1, FECHA, cadena=cadena_actual())['id']}: el origen no tiene un "
                         "alta en la cadena (cuenta no aprobada)"]
    assert zoe not in c.estado.cuentas


def test_un_alta_firmada_por_otro_no_vale():
    c = Cadena()
    trucha = contrato.alta(PRIVADA["alice"], 1, CUENTA["zoe"], "usuario", "zoe", FECHA, cadena=cadena_actual())
    assert any("firma: no es la del organizador" in p for p in c.verificar(c.armar([trucha])))


def test_un_bloque_editado_no_da_su_hash_y_reminado_no_encadena_con_la_copia():
    c = Cadena()
    bloque, _ = c.agregar([firmada("alice", "bob", 30)])
    copia = estado.Estado()
    estado.aplicar_bloque(copia, c.genesis, REGLAS)
    editado = json.loads(json.dumps(bloque))
    editado["transactions"][1]["monto"] = 1
    assert any("el hash no sale" in p for p in estado.aplicar_bloque(copia.copia(), editado, REGLAS)[0])
    assert any("firma" in p for p in estado.aplicar_bloque(copia.copia(), remina(editado), REGLAS)[0])


def test_una_coinbase_inflada_o_una_transaccion_repetida_no_valen():
    c = Cadena()
    tx = firmada("alice", "bob", 30)
    c.agregar([tx])
    inflado = c.armar([firmada("alice", "bob", 1, nonce=2)])
    inflado["transactions"][0]["monto"] = 10**6
    assert any("coinbase" in p for p in c.verificar(remina(inflado)))
    assert any("nonce 1, y le tocaba el 2" in p for p in c.verificar(c.armar([tx])))


def test_un_bloque_que_no_encadena_o_con_otra_altura_no_vale():
    c = Cadena()
    bloque = c.armar([firmada("alice", "bob", 1)])
    assert any("no encadena" in p for p in c.verificar(remina({**bloque, "previous_hash": "f" * 32})))
    assert any("altura" in p for p in c.verificar({**bloque, "altura": 7}))


def test_en_modo_estricto_el_estado_no_cambia_y_sin_estricto_se_aplica_igual():
    c = Cadena()
    raiz = c.estado.raiz()
    malo = c.armar([firmada("alice", "bob", 10_000)])
    assert estado.aplicar_bloque(c.estado, malo, REGLAS)[0] and c.estado.raiz() == raiz and c.estado.altura == 0
    problemas, _ = estado.aplicar_bloque(c.estado, malo, REGLAS, estricto=False)
    assert problemas and c.estado.altura == 1  # lo inválido no se aplicó, el resto sí


# ── Permisos y altas delegadas (el escalador) ──

def test_una_clave_con_permiso_da_de_alta_y_de_baja_solo_sus_mineros():
    c = Cadena()
    escalador = PRIVADA["escalador"]
    c.agregar([contrato.permiso(PRIVADA["organizador"], 1, CUENTA["escalador"], "minero-cpu-", FECHA, cadena=cadena_actual())])
    vm = CUENTA["vm0"]
    c.agregar([contrato.alta(escalador, 1, vm, "minero", "minero-cpu-0", FECHA, delegada=True, cadena=cadena_actual())])
    assert c.estado.cuentas[vm] == {"tipo": "minero", "nombre": "minero-cpu-0", "alta": FECHA}
    usuario = contrato.alta(escalador, 2, CUENTA["zoe"], "usuario", "minero-cpu-1", FECHA, delegada=True, cadena=cadena_actual())
    otro_nombre = contrato.alta(escalador, 2, CUENTA["zoe"], "minero", "gpu-1", FECHA, delegada=True, cadena=cadena_actual())
    a_bob = contrato.baja(escalador, 2, BOB, "", FECHA, delegada=True, cadena=cadena_actual())
    for tx in (usuario, otro_nombre, a_bob):
        assert any("sin permiso" in p for p in c.verificar(c.armar([tx])))
    c.agregar([contrato.baja(escalador, 2, vm, "la VM se borró", FECHA, delegada=True, cadena=cadena_actual())])
    assert vm not in c.estado.cuentas and c.estado.nonces[CUENTA["escalador"]] == 2


# ── Alta repetida (BC-93) ──

def test_un_alta_repetida_no_vale_ni_en_otro_bloque_ni_en_el_mismo():
    c = Cadena()
    zoe = CUENTA["zoe"]
    c.agregar([contrato.alta(PRIVADA["organizador"], 1, zoe, "usuario", "zoe", FECHA, cadena=cadena_actual())])
    otra = contrato.alta(PRIVADA["organizador"], 2, zoe, "usuario", "zoe", FECHA, cadena=cadena_actual())
    assert any(estado.ALTA_REPETIDA in p for p in c.verificar(c.armar([otra])))
    a_alice = contrato.alta(PRIVADA["organizador"], 2, ALICE, "usuario", "alice", FECHA, cadena=cadena_actual())  # alta del génesis
    assert any(estado.ALTA_REPETIDA in p for p in c.verificar(c.armar([a_alice])))
    dos_juntas = [contrato.alta(PRIVADA["organizador"], 2, CUENTA["vm0"], "minero", "gpu-1", FECHA, cadena=cadena_actual()),
                  contrato.alta(PRIVADA["organizador"], 3, CUENTA["vm0"], "minero", "gpu-1", FECHA, cadena=cadena_actual())]
    assert any(estado.ALTA_REPETIDA in p for p in c.verificar(c.armar(dos_juntas)))


def test_despues_de_una_baja_se_puede_volver_a_dar_de_alta():
    c = Cadena()
    zoe = CUENTA["zoe"]
    c.agregar([contrato.alta(PRIVADA["organizador"], 1, zoe, "usuario", "zoe", FECHA, cadena=cadena_actual())])
    c.agregar([contrato.baja(PRIVADA["organizador"], 2, zoe, "se fue", FECHA, cadena=cadena_actual())])
    c.agregar([contrato.alta(PRIVADA["organizador"], 3, zoe, "usuario", "zoe", FECHA, cadena=cadena_actual())])
    assert c.estado.cuentas[zoe]["nombre"] == "zoe"


def test_el_escalador_tampoco_repite_el_alta_pero_si_despues_de_su_propia_baja():
    """Una baja delegada (la VM se borró) no veta la cuenta: el escalador la puede volver a dar de alta."""
    c = Cadena()
    escalador, vm = PRIVADA["escalador"], CUENTA["vm0"]
    c.agregar([contrato.permiso(PRIVADA["organizador"], 1, CUENTA["escalador"], "minero-cpu-", FECHA, cadena=cadena_actual())])
    c.agregar([contrato.alta(escalador, 1, vm, "minero", "minero-cpu-0", FECHA, delegada=True, cadena=cadena_actual())])
    repetida = contrato.alta(escalador, 2, vm, "minero", "minero-cpu-0", FECHA, delegada=True, cadena=cadena_actual())
    assert any(estado.ALTA_REPETIDA in p for p in c.verificar(c.armar([repetida])))
    c.agregar([contrato.baja(escalador, 2, vm, "la VM se borró", FECHA, delegada=True, cadena=cadena_actual())])
    c.agregar([contrato.alta(escalador, 3, vm, "minero", "minero-cpu-0", FECHA, delegada=True, cadena=cadena_actual())])
    assert vm in c.estado.cuentas


def test_sin_permiso_o_con_el_permiso_revocado_no_puede():
    c = Cadena()
    alta = contrato.alta(PRIVADA["escalador"], 1, CUENTA["vm0"], "minero", "minero-cpu-0", FECHA, delegada=True, cadena=cadena_actual())
    assert any("sin permiso" in p for p in c.verificar(c.armar([alta])))
    c.agregar([contrato.permiso(PRIVADA["organizador"], 1, CUENTA["escalador"], "minero-cpu-", FECHA, cadena=cadena_actual())])
    c.agregar([contrato.permiso(PRIVADA["organizador"], 2, CUENTA["escalador"], "", FECHA, cadena=cadena_actual())])
    assert CUENTA["escalador"] not in c.estado.permisos
    assert any("sin permiso" in p for p in c.verificar(c.armar([alta])))


def test_el_organizador_no_se_da_de_baja():
    c = Cadena()
    baja = contrato.baja(PRIVADA["organizador"], 1, ORG, "", FECHA, cadena=cadena_actual())
    assert any("organizador no se da" in p for p in c.verificar(c.armar([baja])))


# ── Lo que hace el NCT antes de armar: filtrar ──

def test_filtrar_saca_lo_que_no_vale_y_deja_lo_demas_en_orden():
    c = Cadena()
    buena, sin_saldo = firmada("alice", "bob", 10), firmada("bob", "carol", 10_000)
    sin_alta = contrato.transaccion(PRIVADA["zoe"], ALICE, 1, 1, FECHA, cadena=cadena_actual())
    otra = firmada("alice", "carol", 5, nonce=2)
    validas, rechazadas = estado.filtrar(c.estado, [buena, sin_saldo, sin_alta, otra])
    assert validas == [buena, otra] and [tx for tx, _ in rechazadas] == [sin_saldo, sin_alta]
    assert c.estado.saldos[ALICE] == 100  # filtrar no toca el estado


# ── La participación contra la lista publicada (BC-82) ──

def con_epoca(c, epoca, detalle, fondo=contrato.FONDO_EPOCA):
    """Llega al primer bloque de la época siguiente y lo arma con shares-<e> y los pagos que salen de `detalle`."""
    while c.estado.altura + 1 < (epoca + 1) * contrato.BLOQUES_POR_EPOCA:
        c.agregar([])
    c.publicadas[epoca] = detalle
    raiz = contrato.raiz_shares(detalle)
    shares = {m: d["shares"] for m, d in detalle.items()}
    rachas = {m: d["racha"] for m, d in detalle.items()}
    pagos = [contrato.shares_epoca(epoca, fondo, raiz, c.estado.timestamp)]
    pagos += [contrato.participacion(epoca, m, monto, c.estado.timestamp)
              for m, monto in contrato.repartir(fondo, shares, rachas).items()]
    return pagos


def test_el_pago_de_la_epoca_se_recalcula_desde_la_lista_publicada():
    c = Cadena()
    pagos = con_epoca(c, 0, {MINERO: {"shares": 30, "racha": 1}, MINERO2: {"shares": 10, "racha": 1}})
    _, avisos = c.agregar(pagos)
    assert avisos == [] and c.estado.shares[0] == contrato.raiz_shares(c.publicadas[0])
    pagos = con_epoca(c, 1, {MINERO: {"shares": 5, "racha": 2}})
    c.agregar(pagos)


def test_un_pago_inflado_o_una_lista_cambiada_se_notan():
    c = Cadena()
    detalle = {MINERO: {"shares": 30, "racha": 1}, MINERO2: {"shares": 10, "racha": 1}}
    pagos = con_epoca(c, 0, detalle)
    inflado = [*pagos[:1], {**pagos[1], "monto": pagos[1]["monto"] + 50}, *pagos[2:]]
    assert any("no son el reparto" in p for p in c.verificar(c.armar(inflado)))
    c.publicadas[0] = {MINERO: {"shares": 300, "racha": 1}, MINERO2: {"shares": 10, "racha": 1}}
    c.reglas._detalles.clear()
    assert any("no da la raíz" in p for p in c.verificar(c.armar(pagos)))


def test_una_racha_inventada_se_nota():
    c = Cadena()
    c.agregar(con_epoca(c, 0, {MINERO2: {"shares": 10, "racha": 1}}))
    pagos = con_epoca(c, 1, {MINERO: {"shares": 10, "racha": 3}, MINERO2: {"shares": 10, "racha": 2}})
    assert any("racha 3 y le corresponde 1" in p for p in c.verificar(c.armar(pagos)))


def test_sin_la_lista_publicada_queda_un_aviso_y_un_pago_sin_lista_no_vale():
    c = Cadena(shares=lambda epoca: None)
    pagos = con_epoca(c, 0, {MINERO: {"shares": 1, "racha": 1}})
    assert c.verificar(c.armar(pagos[1:])) == [f"{pagos[1]['id']}: un pago sin la lista de shares de la época (shares-0)"]
    _, avisos = c.agregar(pagos)
    assert avisos == ["época 0: el reparto entre los mineros no se verificó (no se consiguió la lista de shares; el "
                      "total sí: 100 de 100)"]


def test_sin_la_lista_publicada_los_pagos_igual_no_suman_mas_que_el_fondo():
    """BC-98: si /api/epocas/<n> no responde, el reparto no se puede verificar, pero el total sí."""
    c = Cadena(shares=lambda epoca: None)
    pagos = con_epoca(c, 0, {MINERO: {"shares": 1, "racha": 1}})
    inflado = [pagos[0], {**pagos[1], "monto": 10**9}]
    assert any("suman 1000000000, más que el fondo (100)" in p for p in c.verificar(c.armar(inflado)))
    dos = [pagos[0], contrato.participacion(0, MINERO, 60, c.estado.timestamp),
           contrato.participacion(0, MINERO2, 60, c.estado.timestamp)]
    dos = [dos[0], *sorted(dos[1:], key=lambda tx: tx["destino"])]
    assert any("suman 120" in p for p in c.verificar(c.armar(dos)))
    # Un fondo declarado más grande tampoco: se compara con el de las reglas.
    otro_fondo = con_epoca(c, 0, {MINERO: {"shares": 1, "racha": 1}}, fondo=10**6)
    assert any("declara un fondo de 1000000" in p for p in c.verificar(c.armar(otro_fondo)))


def test_un_pago_fuera_del_primer_bloque_de_la_epoca_no_vale():
    c = Cadena()
    pago = contrato.participacion(0, MINERO, 10, c.estado.timestamp)
    assert any("fuera de lugar" in p for p in c.verificar(c.armar([pago])))


# ── v4 (BC-95): una baja del organizador no la deshace un alta delegada ──

def test_una_baja_del_organizador_no_la_deshace_el_escalador():
    c = Cadena()
    escalador, vm = PRIVADA["escalador"], CUENTA["vm0"]
    c.agregar([contrato.permiso(PRIVADA["organizador"], 1, CUENTA["escalador"], "minero-cpu-", FECHA, cadena=c.id)])
    c.agregar([contrato.alta(escalador, 1, vm, "minero", "minero-cpu-0", FECHA, delegada=True, cadena=c.id)])
    c.agregar([contrato.baja(PRIVADA["organizador"], 2, vm, "minaba mal", FECHA, cadena=c.id)])
    assert vm not in c.estado.cuentas and vm in c.estado.vetadas
    assert f"{vm}:vetada:0:0" in dict(c.estado.hojas()).values()  # está en la raíz de estado
    de_nuevo = contrato.alta(escalador, 2, vm, "minero", "minero-cpu-0", FECHA, delegada=True, cadena=c.id)
    assert any(estado.VETADA_POR_ORGANIZADOR in p for p in c.verificar(c.armar([de_nuevo])))
    # El organizador sí la puede reactivar, y deja de estar vetada.
    c.agregar([contrato.alta(PRIVADA["organizador"], 3, vm, "minero", "minero-cpu-0", FECHA, cadena=c.id)])
    assert vm in c.estado.cuentas and vm not in c.estado.vetadas
    restaurado = estado.Estado.desde_dict(json.loads(json.dumps(c.estado.a_dict())))
    assert restaurado.raiz() == c.estado.raiz() and restaurado.genesis == c.id


def test_el_snapshot_lleva_las_vetadas_el_genesis_y_el_piso():
    c = Cadena(prefijo_minimo=2)
    c.agregar([contrato.alta(PRIVADA["organizador"], 1, CUENTA["zoe"], "usuario", "zoe", FECHA, cadena=c.id)],
              prefijo="00")
    c.agregar([contrato.baja(PRIVADA["organizador"], 2, CUENTA["zoe"], "", FECHA, cadena=c.id)], prefijo="00")
    copia = estado.Estado.desde_dict(json.loads(json.dumps(c.estado.a_dict())))
    assert (copia.vetadas, copia.genesis, copia.prefijo_minimo) == ({CUENTA["zoe"]}, c.id, 2)
    assert copia.raiz() == c.estado.raiz()


# ── v4 (BC-97): el piso de prefijo ──

def test_un_bloque_con_menos_prefijo_que_el_piso_del_genesis_no_vale():
    c = Cadena(prefijo_minimo=2)
    assert c.estado.prefijo_minimo == 2
    assert any("el piso de la cadena es 2" in p for p in c.verificar(c.armar([], prefijo="0")))
    c.agregar([], prefijo="00")


def test_un_nodo_nuevo_rechaza_la_historia_reminada_con_prefijo_bajo():
    """El operador rehace la cadena desde el génesis con prefijo 1 (gratis): un nodo que arranca de cero no la acepta."""
    honesta = Cadena(prefijo_minimo=2)
    for _ in range(3):
        honesta.agregar([], prefijo="00")
    reminada = Cadena(prefijo_minimo=2)
    reminada.reglas.prefijo_minimo = 0
    bloque = reminada.armar([], prefijo="0")
    reminada.bloques.append(bloque)
    _, malos = estado.reconstruir(reminada.bloques, estado.Reglas(organizador=ORG))
    assert malos and malos[0][0] == 1 and "el piso de la cadena es 2" in malos[0][1][0]
    assert estado.reconstruir(honesta.bloques, estado.Reglas(organizador=ORG))[1] == []


def test_el_nodo_puede_exigir_un_piso_mayor_que_el_del_genesis():
    c = Cadena()  # piso 1 en el génesis
    c.agregar([], prefijo="0")
    _, malos = estado.reconstruir(c.bloques, estado.Reglas(organizador=ORG, prefijo_minimo=2))
    assert malos and "el piso de la cadena es 2" in malos[0][1][0]


def test_unas_reglas_repetidas_o_mal_formadas_en_el_genesis_no_valen():
    genesis = contrato.bloque_genesis({ALICE: 1}, FECHA, organizador=ORG, prefijo_minimo=3)
    for transacciones in ([*genesis["transactions"], contrato.reglas_genesis(1, FECHA)],
                          [genesis["transactions"][0], {**genesis["transactions"][1], "datos": "prefijo_minimo:0"},
                           *genesis["transactions"][2:]]):
        texto = contrato._texto(contrato.CEROS, FECHA, transacciones, contrato.RAIZ_VACIA)
        trucho = {**genesis, "transactions": transacciones, "block_hash": contrato.hash_bloque(texto, 0)}
        problemas, _ = estado.aplicar_bloque(estado.Estado(), trucho, REGLAS)
        assert any("reglas del génesis" in p for p in problemas)


# ── v4 (BC-99): lo firmado para otra cadena ──

def test_lo_firmado_para_la_cadena_anterior_no_se_puede_volver_a_mandar():
    """Al reiniciar, los nonces vuelven a 1: una transferencia y una carga de la cadena vieja no valen en la nueva."""
    vieja = Cadena()
    tx, recarga = firmada("alice", "bob", 10, cadena=vieja.id), carga(1, "alice", 10**6, cadena=vieja.id)
    vieja.agregar([tx, recarga])
    nueva = Cadena(saldos={ALICE: 100, BOB: 50, CAROL: 1})  # otro génesis: otra cadena
    assert nueva.id != vieja.id
    problemas = nueva.verificar(nueva.armar([tx, recarga]))
    assert any(p.startswith(f"transferencia {tx['id']} inválida: firma") for p in problemas)
    assert any(p.startswith("carga carga-1 inválida: firma") for p in problemas)
    nueva.agregar([firmada("alice", "bob", 10, cadena=nueva.id), carga(1, "alice", 10**6, cadena=nueva.id)])

