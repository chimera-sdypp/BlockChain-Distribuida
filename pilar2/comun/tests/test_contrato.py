import json
from pathlib import Path

import pytest

from comun import contrato

VECTOR = json.loads((Path(__file__).parent.parent / "vector_prueba.json").read_text())


def test_vector_de_prueba():
    genesis = contrato.bloque_genesis(VECTOR["genesis"]["saldos"], VECTOR["genesis"]["timestamp"])
    assert genesis["block_hash"] == VECTOR["genesis"]["block_hash"] == VECTOR["previous_hash"]
    texto = contrato.texto_bloque(VECTOR["previous_hash"], VECTOR["timestamp"], VECTOR["transacciones"])
    assert texto == VECTOR["texto"]
    assert contrato.raiz_transacciones(VECTOR["transacciones"]) == VECTOR["raiz"]
    hash_ = contrato.hash_bloque(texto, VECTOR["nonce"])
    assert hash_ == VECTOR["block_hash"]
    assert contrato.cumple(hash_, VECTOR["prefijo"])


def test_el_texto_del_bloque_mide_siempre_77():
    muchas = [{"id": f"tx-{i}", "origen": "alice", "destino": "bob", "monto": 1, "timestamp": 1} for i in range(1000)]
    assert len(contrato.texto_bloque(contrato.CEROS, 1793836860, muchas)) == 77


def test_cambiar_una_transaccion_cambia_el_hash():
    otras = [dict(VECTOR["transacciones"][0], monto=251), VECTOR["transacciones"][1]]
    texto = contrato.texto_bloque(VECTOR["previous_hash"], VECTOR["timestamp"], otras)
    assert contrato.hash_bloque(texto, VECTOR["nonce"]) != VECTOR["block_hash"]


def test_transaccion_valida():
    assert contrato.validar_transaccion({"origen": "alice", "destino": "bob", "monto": 10}) == []
    assert contrato.validar_transaccion({"origen": "alice", "destino": "bob", "monto": contrato.MONTO_MAXIMO}) == []


@pytest.mark.parametrize("minero, valido", [
    ("gpu-tomas", True), ("cpu_vm_3", True), ("", False), ("vm:1", False), ("x" * 33, False), (7, False),
])
def test_id_del_minero_con_formato_de_usuario(minero, valido):
    assert contrato.usuario_valido(minero) is valido


def test_el_genesis_tiene_altura_0():
    assert contrato.bloque_genesis({"alice": 1}, 1)["altura"] == 0


@pytest.mark.parametrize("datos, campo", [
    ({"destino": "bob", "monto": 10}, "origen"),
    ({"origen": "a:b", "destino": "bob", "monto": 10}, "origen"),
    ({"origen": "alice", "destino": "x" * 33, "monto": 10}, "destino"),
    ({"origen": "alice", "destino": "bob", "monto": 0}, "monto"),
    ({"origen": "alice", "destino": "bob", "monto": 1.5}, "monto"),
    ({"origen": "alice", "destino": "bob", "monto": True}, "monto"),
    ({"origen": "alice", "destino": "bob", "monto": contrato.MONTO_MAXIMO + 1}, "monto"),
    ({"origen": "alice", "destino": "alice", "monto": 10}, "distintos"),
    ({"origen": "genesis", "destino": "bob", "monto": 10}, "origen: «genesis»"),
    ({"origen": "alice", "destino": "genesis", "monto": 10}, "destino: «genesis»"),
    ([1, 2], "objeto"),
])
def test_transaccion_invalida(datos, campo):
    errores = contrato.validar_transaccion(datos)
    assert any(campo in error for error in errores), errores


def test_recompensa_crece_con_el_prefijo():
    assert contrato.recompensa("000") == 30
    assert contrato.recompensa("00000", base=2) == 10
