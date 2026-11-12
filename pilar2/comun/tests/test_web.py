from fastapi.testclient import TestClient
from prometheus_client import Counter
from prometheus_client.parser import text_string_to_metric_families

from comun import registro, web

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


def errores(cliente):
    """{(servicio, tipo): valor} de blockchain_errores_total, leído por /metrics."""
    valores = {}
    for familia in text_string_to_metric_families(cliente.get("/metrics").text):
        for muestra in familia.samples:
            if muestra.name == "blockchain_errores_total":
                valores[muestra.labels["servicio"], muestra.labels["tipo"]] = muestra.value
    return valores


def test_metrics_cuenta_los_errores_del_log_por_servicio_y_tipo(tmp_path):
    log = registro.configurar("contador", "INFO", tmp_path)
    log = registro.configurar("contador", "INFO", tmp_path)  # dos veces, como en los tests
    cliente = TestClient(web.crear_app("contador", {}))
    antes = errores(cliente)

    log.warning("no cuenta")
    log.error("sin excepción")
    try:
        raise ConnectionError("no hay red")
    except ConnectionError:
        log.exception("con excepción")
    despues = errores(cliente)

    def sumo(tipo):
        clave = ("contador", tipo)
        return despues.get(clave, 0) - antes.get(clave, 0)

    assert sumo("log") == 1
    assert sumo("ConnectionError") == 1
    assert sum(despues.values()) - sum(antes.values()) == 2
