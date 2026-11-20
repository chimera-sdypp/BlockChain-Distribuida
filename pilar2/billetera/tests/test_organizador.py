"""El modo organizador de la billetera (BC-87) firma exactamente lo mismo que comun/contrato.py.

Ed25519 es determinístico: con la misma clave y el mismo texto, la firma de WebCrypto (node, como el navegador) y la
de `cryptography` tienen que ser idénticas. Sin node, se saltea.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from claves.herramienta import jwk
from comun import contrato
from comun.tests.claves import CUENTA, PRIVADA

NODE = shutil.which("node")
CORRER = Path(__file__).parent / "firmar_organizador.cjs"
ORG = PRIVADA["organizador"]
T = 1_795_000_000

pytestmark = pytest.mark.skipif(NODE is None, reason="necesita node (la billetera es JavaScript)")


def test_las_actas_las_cargas_y_el_pedido_son_los_de_python(tmp_path):
    alice, bob = CUENTA["alice"], CUENTA["bob"]
    cuerpo = json.dumps({"cuenta": alice})
    entrada = {"jwk": jwk(ORG), "pedido": {"metodo": "POST", "ruta": "/api/organizador/rechazar", "tiempo": T,
                                            "cuerpo": cuerpo},
               "txs": [{"tipo": "alta", "numero": 7, "destino": alice, "datos": "usuario:alice", "timestamp": T},
                       {"tipo": "carga", "numero": 8, "destino": alice, "monto": 500, "timestamp": T},
                       {"tipo": "baja", "numero": 9, "destino": bob, "datos": "sin solución imposible", "timestamp": T},
                       {"tipo": "baja", "numero": 10, "destino": bob, "datos": "", "timestamp": T},
                       {"tipo": "permiso", "numero": 11, "destino": bob, "datos": "minero-cpu-", "timestamp": T},
                       {"tipo": "permiso", "numero": 12, "destino": bob, "datos": "", "timestamp": T}]}
    archivo = tmp_path / "entrada.json"
    archivo.write_text(json.dumps(entrada))
    resultado = subprocess.run([NODE, str(CORRER), str(archivo)], capture_output=True, text=True, timeout=60)
    assert resultado.returncode == 0, resultado.stderr
    salida = json.loads(resultado.stdout)

    esperadas = [contrato.alta(ORG, 7, alice, "usuario", "alice", T), contrato.carga(ORG, 8, alice, 500, T),
                 contrato.baja(ORG, 9, bob, "sin solución imposible", T), contrato.baja(ORG, 10, bob, "", T),
                 contrato.permiso(ORG, 11, bob, "minero-cpu-", T), contrato.permiso(ORG, 12, bob, "", T)]
    assert salida["txs"] == esperadas
    for tx in salida["txs"]:
        validar = contrato.validar_carga if tx["id"].startswith("carga-") else contrato.validar_acta
        assert validar(tx, CUENTA["organizador"]) == []
    encabezados = contrato.firmar_pedido(ORG, "POST", "/api/organizador/rechazar", cuerpo.encode(), tiempo=T,
                                         quien=contrato.ORGANIZADOR)
    assert salida["pedido"] == encabezados["X-Firma"]
