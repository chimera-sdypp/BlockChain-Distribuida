"""El verificador de la billetera (JavaScript) da lo mismo que comun/estado.py (Python) sobre las mismas cadenas (BC-84).

Lo corre `node` (Ed25519 con WebCrypto, como el navegador). Sin node, se saltea.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from comun import contrato, estado
from comun.tests.cadena import Cadena, remina
from comun.tests.claves import CUENTA, FECHA, PRIVADA, carga, firmada, cadena_actual

NODE = shutil.which("node")
CORRER = Path(__file__).parent / "correr_verificador.cjs"
VECTOR = json.loads((Path(__file__).parents[2] / "comun" / "vector_prueba.json").read_text())
ORG = PRIVADA["organizador"]

pytestmark = pytest.mark.skipif(NODE is None, reason="necesita node (el verificador es JavaScript)")


def correr(tmp_path, **entrada):
    archivo = tmp_path / "entrada.json"
    archivo.write_text(json.dumps(entrada))
    resultado = subprocess.run([NODE, str(CORRER), str(archivo)], capture_output=True, text=True, timeout=120)
    assert resultado.returncode == 0, resultado.stderr
    return json.loads(resultado.stdout)


def test_el_md5_es_el_de_python_tambien_con_acentos(tmp_path):
    textos = ["", "a", "bench;", "la VM se borró", "x" * 55, "y" * 56, "z" * 64, "ñ" * 200, VECTOR["texto"] + "54420"]
    assert correr(tmp_path, md5=textos)["md5"] == [contrato.md5(t) for t in textos]


def test_el_vector_da_las_mismas_raices_y_saldos(tmp_path):
    salida = correr(tmp_path, bloques=[VECTOR["genesis"]["bloque"], VECTOR["bloque"]], organizador=CUENTA["organizador"])
    assert [b["problemas"] for b in salida["bloques"]] == [[], []]
    assert salida["raiz"] == VECTOR["estado_bloque"]["raiz"] and salida["hojas"] == VECTOR["estado_bloque"]["hojas"]
    assert salida["estado"]["saldos"] == VECTOR["estado_bloque"]["saldos"]
    solo_genesis = correr(tmp_path, bloques=[VECTOR["genesis"]["bloque"]])
    assert solo_genesis["raiz"] == VECTOR["estado_genesis"]["raiz"]
    prueba = VECTOR["estado_genesis"]["prueba_alice"]
    assert correr(tmp_path, prueba=prueba)["prueba"] == VECTOR["estado_genesis"]["raiz"]


def cadena_larga():
    """Cargas, transferencias, actas (también delegadas), una baja, y el pago de dos épocas con su lista."""
    c = Cadena()
    escalador = PRIVADA["escalador"]
    c.agregar([firmada("alice", "bob", 30), carga(1, "carol", 7), contrato.alta(ORG, 2, CUENTA["carol"], "usuario",
                                                                                "carol", FECHA, cadena=cadena_actual())])
    c.agregar([contrato.permiso(ORG, 3, CUENTA["escalador"], "minero-cpu-", FECHA, cadena=cadena_actual()), firmada("carol", "alice", 2)])
    c.agregar([contrato.alta(escalador, 1, CUENTA["vm0"], "minero", "minero-cpu-0", FECHA, delegada=True, cadena=cadena_actual()),
               contrato.alta(ORG, 4, CUENTA["v1"], "validador", "v1", FECHA, cadena=cadena_actual()), contrato.baja(ORG, 5, CUENTA["bob"], "se fue",
                                                                                            FECHA, cadena=cadena_actual())])
    for epoca, detalle in ((0, {CUENTA["minero"]: {"shares": 30, "racha": 1}, CUENTA["vm0"]: {"shares": 7, "racha": 1}}),
                           (1, {CUENTA["minero"]: {"shares": 9, "racha": 2}})):
        while c.estado.altura + 1 < (epoca + 1) * contrato.BLOQUES_POR_EPOCA:
            c.agregar([])
        c.publicadas[epoca] = detalle
        raiz = contrato.raiz_shares(detalle)
        shares = {m: d["shares"] for m, d in detalle.items()}
        rachas = {m: d["racha"] for m, d in detalle.items()}
        pagos = [contrato.shares_epoca(epoca, contrato.FONDO_EPOCA, raiz, c.estado.timestamp)]
        pagos += [contrato.participacion(epoca, m, monto, c.estado.timestamp)
                  for m, monto in contrato.repartir(contrato.FONDO_EPOCA, shares, rachas).items()]
        c.agregar(pagos + [firmada("alice", "carol", 1, nonce=2 + epoca)])
    return c


def test_una_cadena_larga_da_el_mismo_estado_que_python(tmp_path):
    c = cadena_larga()
    salida = correr(tmp_path, bloques=c.bloques, organizador=CUENTA["organizador"],
                    shares={str(e): d for e, d in c.publicadas.items()})
    assert all(b["problemas"] == [] and b["avisos"] == [] for b in salida["bloques"]), salida["bloques"]
    assert salida["raiz"] == c.estado.raiz() and salida["hojas"] == [h for _, h in c.estado.hojas()]
    assert salida["estado"]["saldos"] == {k: v for k, v in c.estado.saldos.items()}
    assert salida["estado"]["cuentas"] == c.estado.cuentas and salida["estado"]["permisos"] == c.estado.permisos


def test_sin_la_lista_de_shares_avisa_y_con_una_lista_cambiada_rechaza(tmp_path):
    c = cadena_larga()
    sin = correr(tmp_path, bloques=c.bloques)
    assert sin["bloques"][10]["avisos"] and sin["raiz"] == c.estado.raiz()
    cambiada = {"0": {CUENTA["minero"]: {"shares": 300, "racha": 1}}}
    otra = correr(tmp_path, bloques=c.bloques, shares=cambiada)
    assert otra["bloques"][-1]["altura"] == 10 and "no da la raíz" in otra["bloques"][-1]["problemas"][0]


@pytest.mark.parametrize("tocar, motivo", [
    (lambda b: b["transactions"][1].update(monto=1), "el hash no sale"),
    (lambda b: b.update(remina({**b, "transactions": [{**b["transactions"][0], "monto": 10**6}] + b["transactions"][1:]})),
     "coinbase"),
    (lambda b: b.update(remina({**b, "raiz_estado": "f" * 32})), "raíz de estado"),
    (lambda b: b.update(remina({**b, "transactions": b["transactions"][:1] + [
        contrato.alta(PRIVADA["alice"], 6, CUENTA["zoe"], "usuario", "zoe", FECHA, cadena=cadena_actual())]})), "firma"),
])
def test_un_bloque_adulterado_se_rechaza_y_no_sigue(tmp_path, tocar, motivo):
    c = cadena_larga()
    bloques = json.loads(json.dumps(c.bloques))
    tocar(bloques[2])
    salida = correr(tmp_path, bloques=bloques, shares={str(e): d for e, d in c.publicadas.items()})
    ultimo = salida["bloques"][-1]
    assert ultimo["altura"] == 2 and any(motivo in p for p in ultimo["problemas"]), ultimo
    assert salida["estado"]["altura"] == 1  # el estado quedó en el bloque anterior


def test_la_finalidad_sale_de_firmas_verificadas_de_validadores_de_la_cadena(tmp_path):
    c = cadena_larga()
    c.agregar([contrato.alta(ORG, 6, CUENTA["v2"], "validador", "v2", FECHA, cadena=cadena_actual()),
               contrato.alta(ORG, 7, CUENTA["v3"], "validador", "v3", FECHA, cadena=cadena_actual())])
    h = {b["altura"]: b["block_hash"] for b in c.bloques}
    punta = c.estado.altura
    firmas = [contrato.firma_bloque(PRIVADA["v1"], punta, h[punta]), contrato.firma_bloque(PRIVADA["v2"], punta - 1, h[punta - 1]),
              contrato.firma_bloque(PRIVADA["alice"], punta, h[punta]),                       # no es validadora
              {**contrato.firma_bloque(PRIVADA["v3"], punta, h[punta]), "firma": "0" * 128}]  # firma mala
    salida = correr(tmp_path, bloques=c.bloques, shares={str(e): d for e, d in c.publicadas.items()}, firmas=firmas)
    assert salida["finalidad"] == {"altura": punta - 1, "validadores": 3, "umbral": 2, "firmasValidas": 2}
    validadores = {CUENTA["v1"], CUENTA["v2"], CUENTA["v3"]}
    assert contrato.altura_finalizada(validadores, {CUENTA["v1"]: punta, CUENTA["v2"]: punta - 1}) == punta - 1
    assert estado.Estado  # el mismo cálculo que el backend y el nodo


def test_un_alta_repetida_la_rechaza_igual_que_python(tmp_path):
    """BC-93: la regla nueva está en los dos verificadores."""
    c = Cadena()
    c.agregar([contrato.alta(ORG, 1, CUENTA["zoe"], "usuario", "zoe", FECHA, cadena=cadena_actual())])
    malo = c.armar([contrato.alta(ORG, 2, CUENTA["zoe"], "usuario", "zoe", FECHA, cadena=cadena_actual())])
    assert any(estado.ALTA_REPETIDA in p for p in c.verificar(malo))
    ultimo = correr(tmp_path, bloques=c.bloques + [malo])["bloques"][-1]
    assert ultimo["altura"] == 2 and any("la cuenta ya tiene alta" in p for p in ultimo["problemas"]), ultimo


# ── v4 (BC-95, BC-96): las mismas reglas nuevas en los dos verificadores ──

def test_el_piso_de_prefijo_del_genesis_lo_exige_igual_que_python(tmp_path):
    c = Cadena(prefijo_minimo=2)
    c.agregar([], prefijo="00")
    malo = c.armar([], prefijo="0")
    assert any("el piso de la cadena es 2" in p for p in c.verificar(malo))
    salida = correr(tmp_path, bloques=c.bloques + [malo])
    assert salida["estado"]["prefijoMinimo"] == 2 and salida["estado"]["genesis"] == c.id
    ultimo = salida["bloques"][-1]
    assert ultimo["altura"] == 2 and any("el piso de la cadena es 2" in p for p in ultimo["problemas"]), ultimo


def test_lo_firmado_para_otra_cadena_lo_rechaza_igual_que_python(tmp_path):
    vieja, nueva = Cadena(), Cadena(saldos={CUENTA["alice"]: 100, CUENTA["bob"]: 50, CUENTA["carol"]: 1})
    malo = nueva.armar([firmada("alice", "bob", 10, cadena=vieja.id), carga(1, "alice", 10**6, cadena=vieja.id)])
    assert nueva.verificar(malo)
    ultimo = correr(tmp_path, bloques=nueva.bloques + [malo])["bloques"][-1]
    assert ultimo["altura"] == 1 and any("firma" in p for p in ultimo["problemas"]), ultimo
    bueno = nueva.armar([firmada("alice", "bob", 10, cadena=nueva.id)])
    assert correr(tmp_path, bloques=nueva.bloques + [bueno])["bloques"][-1]["problemas"] == []


def test_una_baja_del_organizador_la_respeta_igual_que_python_y_da_la_misma_raiz(tmp_path):
    c = Cadena()
    escalador, vm = PRIVADA["escalador"], CUENTA["vm0"]
    c.agregar([contrato.permiso(ORG, 1, CUENTA["escalador"], "minero-cpu-", FECHA, cadena=c.id),
               contrato.alta(escalador, 1, vm, "minero", "minero-cpu-0", FECHA, delegada=True, cadena=c.id)])
    c.agregar([contrato.baja(ORG, 2, vm, "minaba mal", FECHA, cadena=c.id)])
    salida = correr(tmp_path, bloques=c.bloques)
    assert salida["raiz"] == c.estado.raiz() and salida["hojas"] == [h for _, h in c.estado.hojas()]
    assert f"{vm}:vetada:0:0" in salida["hojas"]
    malo = c.armar([contrato.alta(escalador, 2, vm, "minero", "minero-cpu-0", FECHA, delegada=True, cadena=c.id)])
    assert any(estado.VETADA_POR_ORGANIZADOR in p for p in c.verificar(malo))
    ultimo = correr(tmp_path, bloques=c.bloques + [malo])["bloques"][-1]
    assert any("baja del organizador" in p for p in ultimo["problemas"]), ultimo


def test_un_pago_de_epoca_sobre_el_fondo_lo_rechaza_aunque_no_haya_lista(tmp_path):
    c = Cadena(shares=lambda epoca: None)
    while c.estado.altura + 1 < contrato.BLOQUES_POR_EPOCA:
        c.agregar([])
    detalle = {CUENTA["minero"]: {"shares": 1, "racha": 1}}
    pagos = [contrato.shares_epoca(0, contrato.FONDO_EPOCA, contrato.raiz_shares(detalle), c.estado.timestamp),
             contrato.participacion(0, CUENTA["minero"], 10**9, c.estado.timestamp)]
    malo = c.armar(pagos)
    assert any("más que el fondo" in p for p in c.verificar(malo))
    ultimo = correr(tmp_path, bloques=c.bloques + [malo])["bloques"][-1]  # sin `shares`: /api/epocas no responde
    assert ultimo["altura"] == 10 and any("más que el fondo" in p for p in ultimo["problemas"]), ultimo
