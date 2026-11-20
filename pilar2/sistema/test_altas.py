"""BC-58 y BC-79 en el sistema desplegado: pedir el alta, aprobarla con un acta firmada (y saldo inicial) y operar.

Las actas se firman con la privada del organizador: ORGANIZADOR_ARCHIVO (la privada en hex) o, si no está, la
de prueba (`organizador` de comun/tests/claves.py), que es la que usa el k3s. Si el sistema no la acepta
(GKE tiene otro organizador), se saltean.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from comun import contrato
from comun.tests.claves import PRIVADA
from minero_cpu.minero import Gateway, Minero

from .conftest import GATEWAY_URL, ORGANIZADOR, PRIVADAS, URL, acta, api, aprobar, dar_de_baja, esperar, firmada, saldo


def panel(metodo, ruta, datos=None, privada=ORGANIZADOR):
    """Un pedido al panel (lo que no va a la cadena: rechazar y reactivar), firmado con X-Organizador."""
    cuerpo = json.dumps(datos).encode() if datos is not None else b""
    encabezados = contrato.firmar_pedido(privada, metodo, ruta, cuerpo, quien=contrato.ORGANIZADOR)
    pedido = urllib.request.Request(URL + ruta, data=cuerpo or None, method=metodo,
                                    headers={"Content-Type": "application/json", **encabezados})
    try:
        with urllib.request.urlopen(pedido, timeout=10) as respuesta:
            return respuesta.status, json.loads(respuesta.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


@pytest.fixture(scope="module", autouse=True)
def con_el_organizador():
    codigo, cuerpo = panel("GET", "/api/organizador/altas")
    if codigo == 403:
        pytest.skip(f"el sistema no acepta esta clave de organizador: {cuerpo}")
    assert codigo == 200, cuerpo


def pedir_alta(tipo="usuario"):
    privada, cuenta = contrato.nueva_clave()
    nombre = f"alta-{uuid.uuid4().hex[:6]}"
    codigo, cuerpo = api("POST", "/api/altas", contrato.pedido_alta(privada, tipo, nombre))
    assert codigo == 202, cuerpo
    return privada, cuenta, nombre


def test_un_usuario_aprobado_con_un_acta_y_saldo_puede_transferir(minero):
    privada, cuenta, nombre = pedir_alta()
    PRIVADAS[cuenta] = privada
    assert api("GET", f"/api/cuentas/{cuenta}")[1]["alta_pedida"] is True
    assert cuenta in [p["cuenta"] for p in panel("GET", "/api/organizador/altas")[1]["pendientes"]]

    aprobar(cuenta, "usuario", nombre)                     # el alta entra en un bloque
    assert api("GET", f"/api/cuentas/{cuenta}")[1]["aprobada"] is True
    assert cuenta not in [p["cuenta"] for p in panel("GET", "/api/organizador/altas")[1]["pendientes"]]
    ahora = int(time.time())
    acta(lambda n: contrato.carga(ORGANIZADOR, n, cuenta, 50, ahora), esperar_bloque=True)
    assert saldo(cuenta) == 50
    transferencia, _ = esperar(api("POST", "/api/transacciones", firmada(cuenta, "bob", 5))[1]["id"], limite=240)
    assert transferencia["estado"] == "confirmada", transferencia
    assert saldo(cuenta) == 45


def test_un_acta_o_una_carga_firmada_por_otra_clave_no_entra():
    _, cuenta, _ = pedir_alta()
    ahora = int(time.time())
    for trucha in (contrato.carga(PRIVADA["alice"], 1, cuenta, 1000, ahora),
                   contrato.alta(PRIVADA["alice"], 1, cuenta, "usuario", "trucha", ahora)):
        codigo, cuerpo = api("POST", "/api/transacciones", trucha)
        assert codigo == 400 and "organizador" in cuerpo["errores"][0]
    assert panel("POST", "/api/organizador/rechazar", {"cuenta": cuenta})[0] == 200


def test_el_panel_no_atiende_a_otra_clave():
    codigo, _ = panel("GET", "/api/organizador/cuentas", privada=PRIVADA["alice"])
    assert codigo == 403


def test_un_minero_nuevo_queda_pedido_y_al_aprobarlo_empieza_a_trabajar():
    privada, cuenta = contrato.nueva_clave()
    alias = f"alta-{uuid.uuid4().hex[:6]}"
    nuevo = Minero(Gateway(GATEWAY_URL, privada), alias, procesos=1, espera=0.5, latido=5, espera_aprobacion=2)
    hilo = threading.Thread(target=nuevo.presentarse, daemon=True)
    hilo.start()
    try:
        limite = time.monotonic() + 30
        while True:
            pendientes = panel("GET", "/api/organizador/altas")[1]["pendientes"]
            pedido = next((p for p in pendientes if p["cuenta"] == cuenta), None)
            if pedido:
                break
            assert time.monotonic() < limite, "el minero no quedó pedido"
            time.sleep(1)
        assert pedido["tipo"] == "minero" and pedido["nombre"] == alias

        aprobar(cuenta, "minero", alias)                   # validada por el NCT: ya puede minar
        hilo.join(timeout=90)                              # el /hello pasa y resuelve la tarea modelo
        assert not hilo.is_alive(), "el minero aprobado no terminó de presentarse"
        with urllib.request.urlopen(GATEWAY_URL.rstrip("/") + "/mineros", timeout=10) as respuesta:
            mineros = {m["id"]: m for m in json.load(respuesta)}   # los mineros los lista el gateway (BC-54)
        assert mineros[cuenta]["alias"] == alias and mineros[cuenta]["bench"] > 0
    finally:
        nuevo.detener()
        dar_de_baja(cuenta)
