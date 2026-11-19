"""El nodo verificador (BC-83) y el validador (BC-86) contra una fuente de mentira que puede mentir."""

import json

import pytest

from comun import contrato
from comun.tests.cadena import Cadena, remina
from comun.tests.claves import CUENTA, FECHA, PRIVADA, carga, firmada
from nodo.almacen import Almacen
from nodo.app import Nodo

ORG = PRIVADA["organizador"]


class FuenteFalsa:
    """La API pública, sobre una lista de bloques que el test puede cambiar (lo que haría el operador)."""

    def __init__(self, cadena):
        self.cadena, self.firmas, self.viva = cadena, {}, True
        self.bloques_servidos = None  # si no es None, sirve esta lista en vez de la de la cadena

    def _lista(self):
        return self.bloques_servidos if self.bloques_servidos is not None else self.cadena.bloques

    def bloques(self, desde, limite=100):
        return [json.loads(json.dumps(b)) for b in self._lista()[desde:desde + limite]]

    def epoca(self, numero):
        return self.cadena.publicadas.get(numero)

    def punta(self):
        return self._lista()[-1]

    def pedir(self, metodo, ruta, cuerpo=None):
        if ruta == "/api/firmas":
            previa = self.firmas.get((cuerpo["validador"], cuerpo["altura"]))
            if previa and previa["block_hash"] != cuerpo["block_hash"]:
                return 409, {"error": "doble"}
            self.firmas[(cuerpo["validador"], cuerpo["altura"])] = cuerpo
            return 200, {"resultado": "aceptada"}
        if ruta == "/api/finalidad":
            ultimas = {}
            for (validador, _), firma in sorted(self.firmas.items(), key=lambda par: par[0][1]):
                ultimas[validador] = firma
            return 200, {"firmas": ultimas}
        raise AssertionError(ruta)


@pytest.fixture
def cadena():
    c = Cadena()
    c.agregar([firmada("alice", "bob", 30), carga(1, "carol", 7)])
    c.agregar([contrato.alta(ORG, 2, CUENTA["carol"], "usuario", "carol", FECHA)])
    return c


def nodo_sobre(fuente, tmp_path, nombre="nodo", **opciones):
    return Nodo(Almacen(tmp_path / f"{nombre}.sqlite"), fuente, PRIVADA[nombre], **opciones)


def test_baja_la_cadena_la_verifica_y_queda_con_el_mismo_estado(cadena, tmp_path):
    nodo = nodo_sobre(FuenteFalsa(cadena), tmp_path)
    nodo.arrancar()
    assert nodo.sincronizar() == 3 and nodo.estado.raiz() == cadena.estado.raiz()
    cadena.agregar([firmada("bob", "carol", 5)])
    assert nodo.sincronizar() == 1 and nodo.sincronizar() == 0
    assert nodo.estado.altura == 3 and nodo.almacen.altura() == 3


def test_al_reiniciar_sigue_desde_su_copia_sin_bajar_de_nuevo(cadena, tmp_path):
    fuente = FuenteFalsa(cadena)
    primero = nodo_sobre(fuente, tmp_path)
    primero.arrancar()
    primero.sincronizar()
    otro = nodo_sobre(fuente, tmp_path)
    otro.arrancar()
    assert otro.estado.altura == 2 and otro.estado.raiz() == cadena.estado.raiz()


def test_un_bloque_con_un_saldo_inventado_se_rechaza_y_no_se_sigue(cadena, tmp_path):
    fuente = FuenteFalsa(cadena)
    nodo = nodo_sobre(fuente, tmp_path)
    nodo.arrancar()
    nodo.sincronizar()
    tocado = cadena.estado.copia()
    tocado.sumar(CUENTA["carol"], 10**6)  # el operador hizo HINCRBY saldos y el NCT armó el bloque con eso
    malo = cadena.armar([contrato.transaccion(PRIVADA["carol"], CUENTA["alice"], 500_000, 1, FECHA)],
                        raiz_estado=tocado.raiz())
    fuente.bloques_servidos = cadena.bloques + [malo]
    assert nodo.sincronizar() == 0 and nodo.estado.altura == 2
    assert nodo.rechazado["altura"] == 3 and any("raíz de estado" in m for m in nodo.rechazado["motivos"])
    assert nodo.almacen.rechazos()[0]["block_hash"] == malo["block_hash"]


def test_una_historia_reescrita_es_un_conflicto(cadena, tmp_path):
    fuente = FuenteFalsa(cadena)
    nodo = nodo_sobre(fuente, tmp_path)
    nodo.arrancar()
    nodo.sincronizar()
    # El operador saca la transferencia del bloque 1 y remina todo lo que sigue.
    otra = Cadena()
    otra.agregar([carga(1, "carol", 7)])
    otra.agregar([contrato.alta(ORG, 2, CUENTA["carol"], "usuario", "carol", FECHA)])
    fuente.bloques_servidos = otra.bloques
    assert nodo.sincronizar() == 0
    assert nodo.conflicto == {"altura": 2, "mia": cadena.estado.punta, "fuente": otra.estado.punta}


def test_un_bloque_editado_en_redis_no_da_su_hash(cadena, tmp_path):
    fuente = FuenteFalsa(cadena)
    editados = json.loads(json.dumps(cadena.bloques))
    editados[1]["transactions"][1]["monto"] = 1
    fuente.bloques_servidos = editados
    nodo = nodo_sobre(fuente, tmp_path)
    nodo.arrancar()
    assert nodo.sincronizar() == 1 and nodo.estado.altura == 0  # sólo el génesis
    assert any("hash no sale" in m for m in nodo.rechazado["motivos"])
    fuente.bloques_servidos = [editados[0], remina(editados[1])]
    assert nodo.sincronizar() == 0 and any("firma" in m for m in nodo.rechazado["motivos"])


def test_la_atestacion_va_firmada_por_el_nodo(cadena, tmp_path):
    nodo = nodo_sobre(FuenteFalsa(cadena), tmp_path)
    nodo.arrancar()
    nodo.sincronizar()
    a = nodo.atestacion()
    assert (a["altura"], a["punta"], a["raiz"]) == (2, cadena.estado.punta, cadena.estado.raiz())
    mensaje = contrato.mensaje_atestacion(a["nodo"], a["altura"], a["punta"], a["raiz"], a["tiempo"])
    assert contrato.verificar_firma(CUENTA["nodo"], mensaje, a["firma"])


# ── Validadores (BC-86) ──

def con_validadores(cadena, *nombres):
    cadena.agregar([contrato.alta(ORG, 3 + i, CUENTA[n], "validador", n, FECHA) for i, n in enumerate(nombres)])


def test_un_nodo_que_no_es_validador_en_la_cadena_no_firma(cadena, tmp_path):
    nodo = nodo_sobre(FuenteFalsa(cadena), tmp_path, validador=True)
    nodo.arrancar()
    nodo.sincronizar()
    assert nodo.firmar_si_toca() is None


def test_dos_de_tres_validadores_finalizan_y_cada_uno_firma_una_vez_por_altura(cadena, tmp_path):
    con_validadores(cadena, "v1", "v2", "v3")
    fuente = FuenteFalsa(cadena)
    v1, v2 = (nodo_sobre(fuente, tmp_path, n, validador=True) for n in ("v1", "v2"))
    for v in (v1, v2):
        v.arrancar()
        v.sincronizar()
    firma = v1.firmar_si_toca()
    assert firma["altura"] == 3 and contrato.validar_firma_bloque(firma) == []
    assert v1.firmar_si_toca() is None  # ya la mandó
    assert v1.finalidad()["altura"] == -1  # 1 de 3: falta una
    v2.firmar_si_toca()
    assert v1.finalidad() == {"altura": 3, "validadores": 3, "umbral": 2, "firmas_validas": 2}
    cadena.agregar([firmada("bob", "alice", 1)])
    v1.sincronizar()
    assert v1.firmar_si_toca()["altura"] == 4
    assert v1.almacen.meta("ultima_firma") == {"altura": 4, "block_hash": cadena.estado.punta}


def test_un_validador_no_firma_un_bloque_invalido_ni_una_historia_reescrita(cadena, tmp_path):
    con_validadores(cadena, "v1")
    fuente = FuenteFalsa(cadena)
    v1 = nodo_sobre(fuente, tmp_path, "v1", validador=True)
    v1.arrancar()
    v1.sincronizar()
    v1.firmar_si_toca()
    malo = cadena.armar([firmada("bob", "alice", 10**9)])
    fuente.bloques_servidos = cadena.bloques + [malo]
    v1.sincronizar()
    assert v1.estado.altura == 3 and v1.firmar_si_toca() is None and len(fuente.firmas) == 1
    # Aunque la fuente le muestre otra cadena, la altura 3 ya la firmó: nunca firma otro bloque ahí.
    otra = Cadena()
    otra.agregar([])
    fuente.bloques_servidos = otra.bloques + [otra.armar([])]
    v1.sincronizar()
    assert v1.conflicto and v1.firmar_si_toca() is None
