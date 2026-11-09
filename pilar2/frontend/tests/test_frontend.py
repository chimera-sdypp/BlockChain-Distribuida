"""La página y el SSE de /eventos, con un servidor de verdad (uvicorn) y sin RabbitMQ."""

import http.client
import json
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from comun import web
from frontend import app as frontend


def puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def armar(keepalive=15):
    difusor = frontend.Difusor()
    app = web.crear_app("frontend", {})
    frontend.agregar_rutas(app, difusor, keepalive=keepalive)
    return app, difusor


@pytest.fixture
def servidor():
    """Levanta la app en un puerto libre y devuelve (puerto, difusor)."""
    def levantar(keepalive=15):
        app, difusor = armar(keepalive)
        puerto = puerto_libre()
        servidores.append(web.servir(app, puerto, en_hilo=True))
        limite = time.monotonic() + 5
        while time.monotonic() < limite:
            try:
                socket.create_connection(("127.0.0.1", puerto), timeout=0.2).close()
                return puerto, difusor
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("no levantó el servidor")

    servidores = []
    yield levantar
    for s in servidores:
        s.should_exit = True


class LectorSSE:
    """Lee eventos SSE de /eventos: [(evento, datos)] y los comentarios (keep-alive)."""

    def __init__(self, puerto):
        self.conexion = http.client.HTTPConnection("127.0.0.1", puerto, timeout=5)
        self.conexion.request("GET", "/eventos")
        self.respuesta = self.conexion.getresponse()

    def siguiente(self):
        """El próximo bloque SSE: ("evento", nombre, datos) o ("comentario", texto, None)."""
        lineas = []
        while True:
            linea = self.respuesta.readline().decode().rstrip("\n")
            if linea == "":
                if lineas:
                    break
                continue
            lineas.append(linea)
        if lineas[0].startswith(":"):
            return "comentario", lineas[0][1:].strip(), None
        campos = dict(l.split(": ", 1) for l in lineas)
        return "evento", campos["event"], json.loads(campos["data"])

    def cerrar(self):
        self.conexion.close()


def test_la_pagina_se_sirve_en_la_raiz():
    app, _ = armar()
    respuesta = TestClient(app).get("/")
    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/html")
    assert "/eventos" in respuesta.text and "/api/bloques" in respuesta.text


def test_un_aviso_llega_por_sse_a_todos_los_navegadores(servidor):
    puerto, difusor = servidor()
    uno, otro = LectorSSE(puerto), LectorSSE(puerto)
    assert uno.respuesta.getheader("content-type").startswith("text/event-stream")
    assert uno.siguiente()[0] == otro.siguiente()[0] == "comentario"  # el saludo al conectar

    difusor.difundir("bloque.confirmado", {"tarea": "t1", "block_hash": "00ab", "altura": 3})

    esperado = ("evento", "bloque.confirmado", {"tarea": "t1", "block_hash": "00ab", "altura": 3})
    assert uno.siguiente() == esperado
    assert otro.siguiente() == esperado
    uno.cerrar()
    otro.cerrar()


def test_el_sse_manda_keepalive(servidor):
    puerto, _ = servidor(keepalive=0.2)
    lector = LectorSSE(puerto)
    lector.siguiente()  # saludo
    assert lector.siguiente() == ("comentario", "keep-alive", None)
    lector.cerrar()


def test_la_metrica_de_clientes_sube_y_baja(servidor):
    puerto, difusor = servidor()
    lector = LectorSSE(puerto)
    lector.siguiente()
    assert difusor.clientes() == 1
    lector.cerrar()
    difusor.difundir("bloque.confirmado", {})  # el envío falla y el servidor nota la desconexión
    limite = time.monotonic() + 5
    while difusor.clientes() and time.monotonic() < limite:
        time.sleep(0.05)
    assert difusor.clientes() == 0
