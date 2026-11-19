"""El recorrido de una transferencia por los pasos 1 a 11 del diagrama del Pilar 2, con el contrato v2."""

import threading

from comun import contrato, estado
from comun.tests.claves import CUENTA

from .conftest import api, bloques, esperar, firmada, nuevo_usuario, saldo, transferir


def verificar_bloque(bloque):
    """NCT.3, otra vez desde afuera: el hash sale del texto (con la coinbase en la raíz) y el nonce, y cumple el prefijo."""
    texto = contrato.texto_de_bloque(bloque)  # v3: con la raíz de estado
    assert contrato.hash_bloque(texto, bloque["nonce"]) == bloque["block_hash"]
    assert contrato.cumple(bloque["block_hash"], bloque["prefijo"])
    coinbase = bloque["transactions"][0]
    assert coinbase == contrato.coinbase(bloque["altura"], bloque["minero"], contrato.recompensa(bloque["prefijo"]),
                                         bloque["timestamp"])


def test_transferencia_firmada_termina_en_un_bloque_encadenado(minero):
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
    assert saldo(bloque["minero"]) >= contrato.recompensa(bloque["prefijo"])  # cobró su coinbase


def test_sin_saldo_queda_rechazada(minero):
    pobre = nuevo_usuario("pobre", aprobada=True)
    tx, _ = esperar(transferir(pobre, "bob", 1))
    assert tx["estado"] == "rechazada" and tx["motivo"] == "saldo insuficiente"
    assert saldo(pobre) == 0


def test_una_cuenta_sin_aprobar_no_puede_transferir(minero):
    sin_aprobar = nuevo_usuario("sin-aprobar")
    tx, _ = esperar(transferir(sin_aprobar, "bob", 1))
    assert tx["estado"] == "rechazada" and tx["motivo"] == "cuenta no aprobada"


def test_una_firma_falsa_o_un_monto_cambiado_no_entran():
    """Nadie puede mandar una transferencia de bob sin la clave de bob."""
    tx = firmada("bob", "alice", 5, nonce=10**9)
    for trucha in ({**tx, "firma": "ab" * 64}, {**tx, "monto": 500}, {**tx, "origen": firmada("carol", "alice", 5, nonce=1)["origen"]}):
        codigo, cuerpo = api("POST", "/api/transacciones", {k: v for k, v in trucha.items() if k != "id"})
        assert codigo == 400 and any("firma" in e for e in cuerpo["errores"]), cuerpo


def test_reenviar_una_transaccion_da_409(minero):
    tx = firmada("alice", nuevo_usuario("reenvio"), 1)
    assert api("POST", "/api/transacciones", tx)[0] == 202
    codigo, cuerpo = api("POST", "/api/transacciones", tx)
    assert codigo == 409 and cuerpo["id"] == tx["id"]
    assert esperar(tx["id"])[0]["estado"] == "confirmada"


def test_doble_gasto_con_el_mismo_nonce_entra_una(minero):
    usuario = nuevo_usuario("doble", aprobada=True)
    tx, _ = esperar(transferir("alice", usuario, 10))
    assert tx["estado"] == "confirmada"
    # Dos transacciones distintas firmadas con el mismo nonce, a la vez: una se rechaza.
    gastos, ids, barrera = [firmada(usuario, d, 10, nonce=1) for d in ("bob", "carol")], [], threading.Barrier(2)

    def gastar(tx_):
        barrera.wait()
        ids.append(api("POST", "/api/transacciones", tx_)[1]["id"])

    hilos = [threading.Thread(target=gastar, args=(g,)) for g in gastos]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    cerradas = sorted((t["estado"], t.get("motivo", "")) for t in (esperar(id_)[0] for id_ in ids))
    assert cerradas == [("confirmada", ""), ("rechazada", "nonce repetido")]
    assert saldo(usuario) == 0


def test_la_cadena_entera_pasa_la_verificacion_de_un_nodo():
    """v3: bajada desde el génesis, la cadena cumple cada regla del contrato (comun/estado.py, en modo estricto, con
    las listas de shares publicadas) y el estado que sale de ella es el que muestra la API."""
    todos, desde = [], 0
    while True:
        lote = api("GET", f"/api/bloques?orden=asc&desde={desde}&limite=100")[1]
        if not lote:
            break
        todos += lote
        desde += len(lote)

    def shares(epoca):
        codigo, cuerpo = api("GET", f"/api/epocas/{epoca}")
        return cuerpo["detalle"] if codigo == 200 else None

    resultado, malos = estado.reconstruir(todos, estado.Reglas(shares=shares))
    assert malos == [] and resultado.altura == len(todos) - 1
    prueba = api("GET", f"/api/prueba/{CUENTA['alice']}")[1]
    if prueba["altura"] == resultado.altura:  # si no entró otro bloque en el medio
        assert prueba["raiz"] == resultado.raiz()
        assert saldo("alice") == resultado.saldos.get(CUENTA["alice"], 0)


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
