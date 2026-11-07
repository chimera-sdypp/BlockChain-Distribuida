from fastapi.testclient import TestClient
from prometheus_client import Counter

from comun import web

PEDIDOS = Counter("prueba_pedidos", "Contador de prueba")


def _falla():
    raise ConnectionError("no hay red")


def test_health_ok():
    cliente = TestClient(web.crear_app("prueba", {"redis": lambda: True}))
    respuesta = cliente.get("/health")
    assert respuesta.status_code == 200
    assert respuesta.json() == {"prueba": "ok", "redis": "ok"}


def test_health_con_una_dependencia_caida_da_503():
    cliente = TestClient(web.crear_app("prueba", {"rabbitmq": lambda: False, "redis": _falla}))
    respuesta = cliente.get("/health")
    assert respuesta.status_code == 503
    assert respuesta.json() == {"prueba": "degradado", "rabbitmq": "caido", "redis": "caido"}


def test_metrics_para_prometheus():
    PEDIDOS.inc()
    respuesta = TestClient(web.crear_app("prueba", {})).get("/metrics")
    assert respuesta.status_code == 200
    assert "prueba_pedidos_total 1.0" in respuesta.text
