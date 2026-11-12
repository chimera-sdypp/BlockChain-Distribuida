"""El monitoreo del sistema desplegado (BC-37): lo que ve quien abre Prometheus, Loki o Grafana.

Le habla a sus APIs HTTP. k8s/probar_sistema.sh abre los port-forwards y arma las variables:
  PROMETHEUS_URL, LOKI_URL, GRAFANA_URL   (por defecto, localhost:9090, :3100 y :3000)
  GRAFANA_USUARIO, GRAFANA_CLAVE          las del Secret grafana-admin
"""

import base64
import json
import os
import threading
import time
import urllib.parse
import urllib.request

import pytest

from minero_cpu.minero import Gateway, Minero

from .conftest import GATEWAY_URL, TOKEN, esperar, kubectl, nuevo_usuario, pods, transferir

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "").rstrip("/")
LOKI = os.environ.get("LOKI_URL", "").rstrip("/")
GRAFANA = os.environ.get("GRAFANA_URL", "").rstrip("/")
GRAFANA_USUARIO = os.environ.get("GRAFANA_USUARIO", "admin")
GRAFANA_CLAVE = os.environ.get("GRAFANA_CLAVE", "")

ALERTAS = {"SinMinerosGPU", "ColaRangosAcumulada", "SinBloquesNuevos", "ServicioCaido"}
# Los que escriben en el log en el recorrido de una transferencia. El escalador suele estar apagado, y
# RabbitMQ y Redis llegan a Loki igual, pero en régimen normal pueden pasar horas sin escribir nada.
SERVICIOS = ("backend", "frontend", "nct", "trp", "gateway")

pytestmark = pytest.mark.skipif(not (PROMETHEUS and LOKI and GRAFANA),
                                reason="PROMETHEUS_URL, LOKI_URL y GRAFANA_URL para probar el monitoreo")


def pedir(url, cabeceras=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=cabeceras or {}), timeout=10) as respuesta:
        return json.loads(respuesta.read())


def prometheus(ruta):
    cuerpo = pedir(PROMETHEUS + ruta)
    assert cuerpo["status"] == "success", cuerpo
    return cuerpo["data"]


def grafana(ruta):
    clave = base64.b64encode(f"{GRAFANA_USUARIO}:{GRAFANA_CLAVE}".encode()).decode()
    return pedir(GRAFANA + ruta, {"Authorization": f"Basic {clave}"})


def loki(consulta, desde=3600, limite=1000):
    """[(labels, pod, línea)] de las últimas `desde` segundos."""
    parametros = urllib.parse.urlencode({"query": consulta, "limit": limite, "start": int((time.time() - desde) * 1e9)})
    cuerpo = pedir(f"{LOKI}/loki/api/v1/query_range?{parametros}")
    return [(flujo["stream"], flujo["stream"].get("pod"), linea)
            for flujo in cuerpo["data"]["result"] for _, linea, *_ in flujo["values"]]


def hasta_que(condicion, limite, mensaje, cada=5):
    fin = time.monotonic() + limite
    while True:
        resultado = condicion()
        if resultado:
            return resultado
        assert time.monotonic() < fin, mensaje()
        time.sleep(cada)


# --- Prometheus -------------------------------------------------------------------------------
def targets(pool):
    return [t for t in prometheus("/api/v1/targets?state=active")["activeTargets"] if t["scrapePool"] == pool]


@pytest.mark.parametrize("monitor", ["blockchain-apps", "rabbitmq"])
def test_los_targets_del_sistema_estan_up(monitor):
    pool = f"podMonitor/blockchain/{monitor}/0"
    hasta_que(lambda: targets(pool) and all(t["health"] == "up" for t in targets(pool)), 60,
              lambda: f"{pool}: {[(t['labels'].get('pod'), t['health'], t['lastError']) for t in targets(pool)]}")


def reglas():
    return {regla["name"]: regla for grupo in prometheus("/api/v1/rules?type=alert")["groups"]
            for regla in grupo["rules"] if "blockchain-alertas" in grupo["file"]}


def test_las_cuatro_alertas_estan_cargadas():
    assert set(reglas()) == ALERTAS
    assert all(regla["health"] != "err" for regla in reglas().values()), reglas()


def estado(alerta):
    return reglas()[alerta]["state"]  # inactive, pending o firing


def test_sin_mineros_gpu_se_dispara_y_se_resuelve_con_un_minero_gpu_falso():
    """Sin GPU en la máquina está disparada desde el arranque. Un minero tipo «gpu» que sólo manda
    keep-alives la resuelve; al cortarlo, a los 15 s deja de contar y al minuto vuelve a dispararse."""
    hasta_que(lambda: estado("SinMinerosGPU") == "firing", 120,
              lambda: f"SinMinerosGPU quedó {estado('SinMinerosGPU')}: ¿hay un minero GPU vivo?")

    falso = Minero(Gateway(GATEWAY_URL, TOKEN), nuevo_usuario("gpu-falso"), procesos=1, latido=5, tipo="gpu",
                   placa="GPU de prueba")
    falso.presentarse()
    threading.Thread(target=falso._latir, daemon=True).start()  # vivo, pero sin tomar rangos
    try:
        hasta_que(lambda: estado("SinMinerosGPU") == "inactive", 90,
                  lambda: f"con un minero GPU vivo SinMinerosGPU quedó {estado('SinMinerosGPU')}")
    finally:
        falso.detener()

    hasta_que(lambda: estado("SinMinerosGPU") == "firing", 180,
              lambda: f"sin el minero GPU SinMinerosGPU quedó {estado('SinMinerosGPU')}")


# --- Loki -------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def con_logs(minero):
    """Una transferencia confirmada: deja líneas en el log de cada servicio del recorrido."""
    tx, _ = esperar(transferir("alice", nuevo_usuario("monitoreo"), 1), limite=240)
    assert tx["estado"] == "confirmada", tx
    time.sleep(5)  # Fluent Bit manda cada 1 s; Loki tarda un poco en dejarlas consultables


@pytest.mark.usefixtures("con_logs")
@pytest.mark.parametrize("servicio", SERVICIOS)
def test_loki_tiene_las_lineas_de_cada_servicio(servicio):
    lineas = loki(f'{{namespace="blockchain", app="{servicio}"}}', desde=3 * 86400, limite=20)
    assert lineas, f"Loki no tiene líneas de {servicio}"
    assert all(labels["app"] == servicio for labels, _, _ in lineas)
    assert all(pod for _, pod, _ in lineas), "cada línea trae su pod como metadato"


@pytest.mark.usefixtures("con_logs")
def test_loki_filtra_por_nivel():
    info = loki('{namespace="blockchain", nivel="INFO"}')
    assert info, "no hay líneas INFO"
    assert all(labels["nivel"] == "INFO" and " | INFO | " in linea for labels, _, linea in info)
    errores = loki('{namespace="blockchain", nivel="ERROR"}')
    assert all(labels["nivel"] == "ERROR" and " | ERROR | " in linea for labels, _, linea in errores)


@pytest.mark.usefixtures("con_logs")
def test_los_logs_de_un_pod_borrado_siguen_en_loki():
    lineas = loki('{namespace="blockchain", app="trp"}')
    vivos = set(pods("trp"))
    victima = next((pod for _, pod, _ in lineas if pod in vivos), None)
    assert victima, f"no hay líneas de ningún pod vivo del TrP ({vivos})"
    kubectl("delete", "pod", victima, "--wait=true")
    kubectl("rollout", "status", "deployment/trp", "--timeout=180s")
    assert victima not in pods("trp")
    assert loki(f'{{namespace="blockchain", app="trp"}} | pod="{victima}"'), f"se perdieron los logs de {victima}"


# --- Grafana ----------------------------------------------------------------------------------
@pytest.mark.parametrize("uid", ["prometheus", "loki"])
def test_el_datasource_responde_desde_grafana(uid):
    salud = grafana(f"/api/datasources/uid/{uid}/health")
    assert salud["status"] == "OK", salud


def test_el_dashboard_aparece_en_grafana():
    encontrados = hasta_que(lambda: grafana("/api/search?query=blockchain&type=dash-db"), 60,
                            lambda: "el sidecar no cargó el dashboard")
    assert "blockchain-dashboard" in {tablero["uid"] for tablero in encontrados}, encontrados
