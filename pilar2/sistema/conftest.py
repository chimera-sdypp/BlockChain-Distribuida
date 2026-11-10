"""Pruebas del sistema desplegado (BC-30): le hablan al Ingress y al gateway como cualquier cliente.

Se saltean si no está SISTEMA_URL. Las corre k8s/probar_sistema.sh, que arma las variables:
  SISTEMA_URL   el Ingress (http://localhost en k3s)
  GATEWAY_URL   el gateway (su LoadBalancer: http://localhost:8081 en k3s)
  MINER_TOKEN   el del Secret
  NAMESPACE     donde está desplegado (blockchain), para las pruebas de falla con kubectl
"""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest

from minero_cpu.minero import Gateway, Minero

URL = os.environ.get("SISTEMA_URL", "").rstrip("/")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
TOKEN = os.environ.get("MINER_TOKEN", "")
NAMESPACE = os.environ.get("NAMESPACE", "blockchain")
CERRADOS = ("confirmada", "rechazada")


def pytest_collection_modifyitems(config, items):
    if URL:
        return
    saltear = pytest.mark.skip(reason="SISTEMA_URL=<Ingress> para probar el sistema desplegado")
    for item in items:
        if "sistema" in item.nodeid.split("/")[0:2]:
            item.add_marker(saltear)


# --- La API, como la usa el frontend ----------------------------------------------------------
def api(metodo, ruta, cuerpo=None, intentos=30):
    """(código, JSON). Reintenta si el Ingress no llega (un pod que se está reemplazando)."""
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    for intento in range(intentos):
        pedido = urllib.request.Request(URL + ruta, data=datos, method=metodo,
                                        headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(pedido, timeout=10) as respuesta:
                return respuesta.status, json.loads(respuesta.read() or b"null")
        except urllib.error.HTTPError as error:
            if error.code in (502, 503, 504) and intento < intentos - 1:
                time.sleep(1)
                continue
            return error.code, json.loads(error.read() or b"null")
        except (urllib.error.URLError, OSError):
            if intento == intentos - 1:
                raise
            time.sleep(1)


def transferir(origen, destino, monto):
    codigo, cuerpo = api("POST", "/api/transacciones", {"origen": origen, "destino": destino, "monto": monto})
    assert codigo == 202, cuerpo
    return cuerpo["id"]


def esperar(id_, limite=180):
    """Sigue la transacción hasta que se cierra. Devuelve (tx, estados vistos en orden)."""
    vistos, fin = [], time.monotonic() + limite
    while time.monotonic() < fin:
        codigo, tx = api("GET", f"/api/transacciones/{id_}")
        if codigo == 200:
            if not vistos or vistos[-1] != tx["estado"]:
                vistos.append(tx["estado"])
            if tx["estado"] in CERRADOS:
                return tx, vistos
        time.sleep(0.5)
    raise AssertionError(f"la transacción {id_} no se cerró en {limite} s (estados: {vistos})")


def saldo(usuario):
    return api("GET", f"/api/saldos/{usuario}")[1]["saldo"]


def bloques(limite=100):
    return api("GET", f"/api/bloques?limite={limite}")[1]


def nuevo_usuario(prefijo="u"):
    return f"{prefijo}-{uuid.uuid4().hex[:10]}"


# --- kubectl, para las pruebas de falla -------------------------------------------------------
def kubectl(*argumentos, timeout=180):
    resultado = subprocess.run(["kubectl", "-n", NAMESPACE, *argumentos], capture_output=True, text=True,
                               timeout=timeout)
    assert resultado.returncode == 0, resultado.stderr
    return resultado.stdout.strip()


def pods(app):
    return kubectl("get", "pods", "-l", f"app={app}", "--field-selector=status.phase=Running",
                   "-o", "jsonpath={.items[*].metadata.name}").split()


# --- Un minero CPU de prueba ------------------------------------------------------------------
class MineroDePrueba:
    """El minero CPU corriendo en este proceso; se puede parar y volver a arrancar."""

    def __init__(self, minero_id):
        self.id = minero_id
        self.minero = None

    def arrancar(self):
        if self.minero is None:
            self.minero = Minero(Gateway(GATEWAY_URL, TOKEN), self.id, procesos=2, espera=0.5, latido=5)
            self.minero.presentarse()
            threading.Thread(target=self.minero.trabajar, daemon=True).start()

    def parar(self):
        if self.minero is not None:
            self.minero.detener()
            self.minero = None


@pytest.fixture(scope="session")
def minero():
    m = MineroDePrueba(nuevo_usuario("prueba"))
    m.arrancar()
    yield m
    m.parar()
