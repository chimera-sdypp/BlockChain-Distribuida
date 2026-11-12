"""«Matar partes del sistema y ver que siga funcionando»: un minero a mitad de un rango, una
réplica de cada servicio y el primario de Redis. Después de cada caída, una transferencia tiene
que terminar confirmada y la cadena seguir íntegra."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .conftest import GATEWAY_URL, TOKEN, esperar, kubectl, nuevo_usuario, pods, transferir
from . import test_recorrido

PILAR2 = Path(__file__).parents[1]


def confirmada_igual():
    tx, _ = esperar(transferir("alice", nuevo_usuario("falla"), 1), limite=240)
    assert tx["estado"] == "confirmada", tx
    test_recorrido.test_la_cadena_esta_integra()


def logs_gateway():
    return kubectl("logs", "-l", "app=gateway", "--tail=-1", "--prefix")


def test_minero_muerto_a_mitad_de_un_rango(minero, tmp_path):
    """El préstamo vence a los 30 s, el gateway devuelve el rango a la cola y otro resuelve el bloque."""
    minero.parar()  # que el rango lo tome la víctima
    victima = nuevo_usuario("victima")
    salida = tmp_path / "victima.log"
    entorno = dict(os.environ, GATEWAY_URL=GATEWAY_URL, MINER_TOKEN=TOKEN, MINERO_ID=victima, PROCESOS="1",
                   LOGS_DIR=str(tmp_path))
    with salida.open("w") as archivo:
        proceso = subprocess.Popen([sys.executable, "-m", "minero_cpu"], cwd=PILAR2, env=entorno,
                                   stdout=archivo, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        limite = time.monotonic() + 60
        while "Bench:" not in salida.read_text() and "Registrado" not in salida.read_text():
            assert time.monotonic() < limite, salida.read_text()
            time.sleep(0.5)
        id_ = transferir("alice", nuevo_usuario("falla"), 1)
        limite = time.monotonic() + 90
        while "rango" not in salida.read_text().split("Bench:")[-1]:
            assert time.monotonic() < limite, "la víctima no tomó ningún rango"
            time.sleep(0.2)
        time.sleep(1)
        os.killpg(proceso.pid, signal.SIGKILL)  # a mitad del rango, sin aviso
    finally:
        if proceso.poll() is None:
            os.killpg(proceso.pid, signal.SIGKILL)
    limite = time.monotonic() + 90
    while f"de {victima} venció" not in logs_gateway():
        assert time.monotonic() < limite, "el gateway no devolvió el rango de la víctima"
        time.sleep(2)
    minero.arrancar()  # otro minero resuelve el bloque
    tx, _ = esperar(id_, limite=240)
    assert tx["estado"] == "confirmada"
    test_recorrido.test_la_cadena_esta_integra()


@pytest.mark.parametrize("app", ["nct", "trp", "gateway"])
def test_matar_una_replica(minero, app):
    victima = pods(app)[0]
    kubectl("delete", "pod", victima, "--wait=false")
    confirmada_igual()
    kubectl("rollout", "status", f"deployment/{app}", "--timeout=180s")
    assert len(pods(app)) == 2


def primario_redis(salvo=None):
    """Le pregunta a un Sentinel (de un pod que no sea `salvo`, el que se está borrando)."""
    for _ in range(30):
        for pod in pods("redis"):
            if pod == salvo:
                continue
            try:
                host = kubectl("exec", pod, "-c", "sentinel", "--", "redis-cli", "-p", "26379",
                               "sentinel", "get-master-addr-by-name", "mymaster", timeout=15).split()[0]
                return host.split(".")[0]  # redis-N.redis-headless… → redis-N
            except (AssertionError, IndexError, subprocess.TimeoutExpired):
                continue
        time.sleep(2)
    raise AssertionError("ningún Sentinel contesta")


def test_matar_el_primario_de_redis(minero):
    """Sentinel promueve una réplica; lo ya confirmado sigue ahí y lo nuevo se confirma."""
    tx_previa, _ = esperar(transferir("alice", nuevo_usuario("previa"), 1))
    assert tx_previa["estado"] == "confirmada"
    viejo = primario_redis()
    kubectl("delete", "pod", viejo, "--wait=false")
    limite = time.monotonic() + 90
    while primario_redis(salvo=viejo) == viejo:
        assert time.monotonic() < limite, "Sentinel no eligió otro primario"
        time.sleep(2)
    confirmada_igual()
    assert esperar(tx_previa["id"])[0]["estado"] == "confirmada"  # no se perdió
    kubectl("rollout", "status", "statefulset/redis", "--timeout=180s")
