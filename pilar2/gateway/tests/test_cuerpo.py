"""BC-94: el gateway no lee cuerpos de cualquiera ni de cualquier tamaño."""

import json

from fastapi.testclient import TestClient

from comun import contrato, web
from comun.tests.claves import CUENTA, PRIVADA
from gateway import app as gateway

MAXIMO = 1000


class Cuerpo:
    """Un cuerpo en pedazos (chunked, sin Content-Length) que anota si alguien lo leyó."""

    def __init__(self, contenido, pedazo=100):
        self.pedazos = [contenido[i:i + pedazo] for i in range(0, len(contenido), pedazo)]
        self.leido = False

    def __iter__(self):
        self.leido = True
        yield from self.pedazos


def armar(redis, colas):
    rabbit, nombres = colas
    g = gateway.Gateway(redis, rabbit, cola_rangos=nombres["rangos"], cola_resultados=nombres["resultados"])
    app = web.crear_app("gateway", {})
    gateway.agregar_rutas(app, g, cuerpo_maximo=MAXIMO)
    return TestClient(app)


def firmado(cuerpo, nombre="minero", tiempo=None, ruta="/keepalive"):
    return contrato.firmar_pedido(PRIVADA[nombre], "POST", ruta, cuerpo, tiempo=tiempo)


def test_un_content_length_mayor_que_el_maximo_da_413(redis, colas):
    cuerpo = json.dumps({"relleno": "x" * MAXIMO}).encode()
    respuesta = armar(redis, colas).post("/keepalive", content=cuerpo, headers=firmado(cuerpo))
    assert respuesta.status_code == 413


def test_un_cuerpo_sin_content_length_que_pasa_el_maximo_da_413(redis, colas):
    contenido = json.dumps({"relleno": "x" * MAXIMO}).encode()
    cuerpo = Cuerpo(contenido)
    respuesta = armar(redis, colas).post("/keepalive", content=cuerpo, headers=firmado(contenido))
    assert respuesta.status_code == 413 and cuerpo.leido


def test_un_pedido_fuera_de_hora_da_401_sin_leer_el_cuerpo(redis, colas):
    contenido = b"{}"
    cuerpo = Cuerpo(contenido)
    respuesta = armar(redis, colas).post("/keepalive", content=cuerpo, headers=firmado(contenido, tiempo=1))
    assert respuesta.status_code == 401 and "X-Tiempo" in respuesta.json()["error"]
    assert not cuerpo.leido


def test_un_pedido_sin_cuenta_da_401_sin_leer_el_cuerpo(redis, colas):
    cuerpo = Cuerpo(b"{}")
    respuesta = armar(redis, colas).post("/keepalive", content=cuerpo, headers={"X-Tiempo": "1"})
    assert respuesta.status_code == 401 and not cuerpo.leido


def test_un_minero_nuevo_con_un_cuerpo_normal_sigue_pidiendo_su_alta(redis, colas):
    cuerpo = json.dumps({"minero": CUENTA["nuevo"], "tipo": "cpu", "placa": "x", "alias": "nuevo-1"}).encode()
    respuesta = armar(redis, colas).post("/hello", content=cuerpo, headers=firmado(cuerpo, "nuevo", ruta="/hello"))
    assert respuesta.status_code == 403
    assert json.loads(redis.hget(contrato.ALTAS_PENDIENTES, CUENTA["nuevo"]))["nombre"] == "nuevo-1"
