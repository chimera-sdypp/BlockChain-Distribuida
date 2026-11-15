"""Pruebas del sistema desplegado (BC-30): le hablan al Ingress y al gateway como cualquier cliente.

Se saltean si no está SISTEMA_URL. Las corre k8s/probar_sistema.sh, que arma las variables:
  SISTEMA_URL   el Ingress (http://localhost en k3s)
  GATEWAY_URL   el gateway (su LoadBalancer: http://localhost:8081 en k3s)
  NAMESPACE     donde está desplegado (blockchain), para aprobar cuentas y las pruebas de falla con kubectl

Contrato v2: las transferencias van firmadas. alice, bob y carol son las cuentas de prueba del génesis
(comun/tests/claves.py); las demás se generan acá y, si hace falta, se aprueban con k8s/aprobar.sh.
"""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from comun import contrato
from comun.tests.claves import CUENTA, PRIVADA
from minero_cpu.minero import Gateway, Minero

URL = os.environ.get("SISTEMA_URL", "").rstrip("/")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
NAMESPACE = os.environ.get("NAMESPACE", "blockchain")
CERRADOS = ("confirmada", "rechazada")
APROBAR = Path(__file__).parents[1] / "k8s" / "aprobar.sh"
DEL_GENESIS = ("alice", "bob", "carol")


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


# --- Cuentas y transferencias firmadas ---------------------------------------------------------
PRIVADAS = {CUENTA[n]: PRIVADA[n] for n in DEL_GENESIS}  # cuenta → privada, de las que se pueden firmar acá
_nonces, _lock = {}, threading.Lock()


def cuenta(usuario):
    """«alice», «bob» o «carol», o una cuenta en hex."""
    return CUENTA[usuario] if usuario in DEL_GENESIS else usuario


def aprobar(cuenta_, tipo="usuario", nombre=""):
    """Lo que hace el organizador (BC-58): la cuenta entra en `cuentas` del Redis desplegado."""
    resultado = subprocess.run([str(APROBAR), cuenta_, tipo, nombre], capture_output=True, text=True, timeout=60,
                               env=dict(os.environ, NAMESPACE=NAMESPACE))
    assert resultado.returncode == 0, resultado.stderr


def nuevo_usuario(prefijo="u", aprobada=False, tipo="usuario"):
    """Una cuenta nueva con su par de claves (la privada queda acá para poder firmar)."""
    privada, publica = contrato.nueva_clave()
    PRIVADAS[publica] = privada
    if aprobada:
        aprobar(publica, tipo, f"{prefijo}-{publica[:6]}")
    return publica


def siguiente_nonce(cuenta_):
    """El nonce que le toca: el último que aceptó el sistema + 1, y de ahí en adelante se lleva acá."""
    with _lock:
        if cuenta_ not in _nonces:
            _nonces[cuenta_] = api("GET", f"/api/cuentas/{cuenta_}")[1]["nonce"]
        _nonces[cuenta_] += 1
        return _nonces[cuenta_]


def firmada(origen, destino, monto, nonce=None):
    origen = cuenta(origen)
    nonce = siguiente_nonce(origen) if nonce is None else nonce
    return contrato.transaccion(PRIVADAS[origen], cuenta(destino), monto, nonce)


def transferir(origen, destino, monto):
    codigo, cuerpo = api("POST", "/api/transacciones", firmada(origen, destino, monto))
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
    return api("GET", f"/api/saldos/{cuenta(usuario)}")[1]["saldo"]


def bloques(limite=100):
    return api("GET", f"/api/bloques?limite={limite}")[1]


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
def minero_aprobado(alias):
    """(privada, cuenta) de un minero nuevo, ya aprobado por el «organizador»."""
    privada, publica = contrato.nueva_clave()
    aprobar(publica, "minero", alias)
    return privada, publica


class MineroDePrueba:
    """El minero CPU corriendo en este proceso, con su clave aprobada; se puede parar y volver a arrancar."""

    def __init__(self, alias):
        self.alias = alias
        self.privada, self.cuenta = minero_aprobado(alias)
        self.minero = None

    def arrancar(self):
        if self.minero is None:
            self.minero = Minero(Gateway(GATEWAY_URL, self.privada), self.alias, procesos=2, espera=0.5, latido=5)
            self.minero.presentarse()
            threading.Thread(target=self.minero.trabajar, daemon=True).start()

    def parar(self):
        if self.minero is not None:
            self.minero.detener()
            self.minero = None


@pytest.fixture(scope="session")
def minero():
    m = MineroDePrueba(f"prueba-{uuid.uuid4().hex[:6]}")
    m.arrancar()
    yield m
    m.parar()
