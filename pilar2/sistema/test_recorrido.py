"""El recorrido de una transferencia por los pasos 1 a 11 del diagrama del Pilar 2."""

import threading

from comun import contrato

from .conftest import api, bloques, esperar, nuevo_usuario, saldo, transferir


def verificar_bloque(bloque):
    """NCT.3, otra vez desde afuera: el hash sale del texto y el nonce, y cumple el prefijo."""
    texto = contrato.texto_bloque(bloque["previous_hash"], bloque["timestamp"], bloque["transactions"])
    assert contrato.hash_bloque(texto, bloque["nonce"]) == bloque["block_hash"]
    assert contrato.cumple(bloque["block_hash"], bloque["prefijo"])


def test_transferencia_valida_termina_en_un_bloque_encadenado(minero):
    origen, destino = "alice", nuevo_usuario("destino")
    antes = saldo(origen)
    id_ = transferir(origen, destino, 7)                                   # pasos 1 y 2
    tx, vistos = esperar(id_)                                              # 3 a 10
    assert tx["estado"] == "confirmada", tx
    assert vistos[-1] == "confirmada" and "rechazada" not in vistos
    codigo, bloque = api("GET", f"/api/bloques/{tx['bloque']}")
    assert codigo == 200
    assert id_ in [t["id"] for t in bloque["transactions"]]
    verificar_bloque(bloque)
    codigo, anterior = api("GET", f"/api/bloques/{bloque['previous_hash']}")
    assert codigo == 200 and anterior["altura"] == bloque["altura"] - 1   # extiende la cadena
    assert saldo(origen) == antes - 7 and saldo(destino) == 7
    assert saldo(bloque["minero"]) >= contrato.recompensa(bloque["prefijo"])  # cobró la recompensa


def test_sin_saldo_queda_rechazada(minero):
    pobre = nuevo_usuario("pobre")
    tx, _ = esperar(transferir(pobre, "bob", 1))
    assert tx["estado"] == "rechazada" and tx["motivo"] == "saldo insuficiente"
    assert saldo(pobre) == 0


def test_doble_gasto_simultaneo_una_se_rechaza(minero):
    usuario = nuevo_usuario("doble")
    tx, _ = esperar(transferir("alice", usuario, 10))
    assert tx["estado"] == "confirmada"
    ids, barrera = [], threading.Barrier(2)

    def gastar():
        barrera.wait()
        ids.append(transferir(usuario, "bob", 10))

    hilos = [threading.Thread(target=gastar) for _ in range(2)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    estados = sorted(esperar(id_)[0]["estado"] for id_ in ids)
    assert estados == ["confirmada", "rechazada"]
    assert saldo(usuario) == 0


def test_la_cadena_esta_integra():
    """Los últimos bloques encadenan, cada hash se verifica y ninguna transacción está en dos."""
    lista = bloques(100)
    assert lista, "no hay bloques"
    for nuevo, viejo in zip(lista, lista[1:]):
        assert nuevo["previous_hash"] == viejo["block_hash"]
        assert nuevo["altura"] == viejo["altura"] + 1
    vistas = set()
    for bloque in lista:
        if bloque["altura"] > 0:
            verificar_bloque(bloque)
        ids = {t["id"] for t in bloque["transactions"]}
        assert not ids & vistas
        vistas |= ids
