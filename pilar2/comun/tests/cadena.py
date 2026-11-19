"""Una cadena v3 armada en memoria (sin Redis), minando con prefijo «0»: la usan los tests del estado, del nodo y de
los validadores. Cada bloque cuesta unos pocos MD5."""

from comun import contrato, estado
from comun.tests.claves import CUENTA, FECHA

ORG = CUENTA["organizador"]
ALICE, BOB, MINERO = CUENTA["alice"], CUENTA["bob"], CUENTA["minero"]


class Cadena:
    """Arma bloques válidos encima de su propio estado, como el NCT, y deja tocarlos antes de verificarlos."""

    def __init__(self, saldos=None, organizador=ORG, shares=None):
        self.publicadas = {}
        self.reglas = estado.Reglas(organizador=organizador, shares=shares or self.publicadas.get)
        self.genesis = contrato.bloque_genesis(saldos or {ALICE: 100, BOB: 50}, FECHA, organizador=organizador,
                                               nombres={ALICE: "alice", BOB: "bob"})
        self.estado = estado.Estado()
        assert estado.aplicar_bloque(self.estado, self.genesis, self.reglas) == ([], [])
        self.bloques = [self.genesis]

    def armar(self, transacciones, minero=MINERO, prefijo="0", raiz_estado=None, timestamp=None):
        altura = self.estado.altura + 1
        tarea = {"previous_hash": self.estado.punta, "timestamp": timestamp or FECHA + altura * 60, "altura": altura,
                 "prefijo": prefijo, "transacciones": transacciones,
                 "raiz_estado": raiz_estado or self.estado.raiz()}
        texto = contrato.texto_para(tarea, minero)
        nonce = next(n for n in range(10**6) if contrato.cumple(contrato.hash_bloque(texto, n), prefijo))
        return {"previous_hash": tarea["previous_hash"], "timestamp": tarea["timestamp"], "altura": altura,
                "transactions": contrato.transacciones_del_bloque(tarea, minero), "nonce": nonce,
                "block_hash": contrato.hash_bloque(texto, nonce), "prefijo": prefijo,
                "raiz_estado": tarea["raiz_estado"]}

    def agregar(self, transacciones=(), **opciones):
        bloque = self.armar(list(transacciones), **opciones)
        problemas, avisos = estado.aplicar_bloque(self.estado, bloque, self.reglas)
        assert problemas == [], problemas
        self.bloques.append(bloque)
        return bloque, avisos

    def verificar(self, bloque):
        """Los problemas de `bloque` sobre una copia del estado (el original no cambia)."""
        return estado.aplicar_bloque(self.estado.copia(), bloque, self.reglas)[0]


def remina(bloque, prefijo="0"):
    """El mismo bloque, con su hash y su nonce recalculados (lo que haría el operador después de tocarlo)."""
    texto = contrato.texto_de_bloque(bloque)
    nonce = next(n for n in range(10**6) if contrato.cumple(contrato.hash_bloque(texto, n), prefijo))
    return {**bloque, "nonce": nonce, "block_hash": contrato.hash_bloque(texto, nonce)}
