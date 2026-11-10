import json
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from comun import contrato
from minero_cpu.busqueda import Buscador
from minero_cpu.minero import Gateway, Minero, TokenInvalido

TOKEN = "token-de-prueba"
VECTOR = json.loads((Path(__file__).parents[2] / "comun" / "vector_prueba.json").read_text())


def menor_nonce(bloque, desde, hasta, prefijo):
    return next((n for n in range(desde, hasta + 1) if contrato.cumple(contrato.hash_bloque(bloque, n), prefijo)), None)


@pytest.fixture(scope="module")
def buscador():
    b = Buscador(procesos=2)
    yield b
    b.cerrar()


# --- Búsqueda ---------------------------------------------------------------------------------
def test_encuentra_el_menor_nonce_del_vector(buscador):
    nonce, cortado = buscador.buscar(VECTOR["texto"], 0, 200_000, VECTOR["prefijo"])
    assert (nonce, cortado) == (VECTOR["nonce"], False)
    assert contrato.hash_bloque(VECTOR["texto"], nonce) == VECTOR["block_hash"]


def test_rango_que_empieza_despues_y_cruza_trozos(buscador):
    # Con prefijo de 3 caracteres hay varias soluciones: tiene que dar la menor del rango, como el Hit 7.
    bloque, prefijo, desde, hasta = "prueba;", "abc", 70_000, 250_000
    esperado = menor_nonce(bloque, desde, hasta, prefijo)
    assert esperado is not None
    assert buscador.buscar(bloque, desde, hasta, prefijo) == (esperado, False)


def test_rango_sin_solucion(buscador):
    assert buscador.buscar("prueba;", 0, 50_000, "f" * 32) == (None, False)
    assert buscador.probados == 50_001


def test_cortar_abandona_el_rango(buscador):
    threading.Timer(0.3, buscador.cortar).start()
    inicio = time.monotonic()
    assert buscador.buscar("prueba;", 0, 10**9, "f" * 32) == (None, True)
    assert time.monotonic() - inicio < 5
    # Después de un corte vuelve a andar normal.
    assert buscador.buscar(VECTOR["texto"], 0, 2000, VECTOR["prefijo"]) == (VECTOR["nonce"], False)


# --- Contra un gateway de mentira con la API de CONTRATO.md -----------------------------------
class GatewayFalso:
    def __init__(self, rangos=(), bench=None):
        self.rangos = list(rangos)
        self.bench = bench
        self.recibido = []        # (ruta, cuerpo)
        self.cortar = False
        falso = self

        class Manejador(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _responder(self, codigo, cuerpo=None):
                datos = json.dumps(cuerpo).encode() if cuerpo is not None else b""
                self.send_response(codigo)
                self.send_header("Content-Length", str(len(datos)))
                self.end_headers()
                self.wfile.write(datos)

            def _autorizado(self):
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._responder(401, {"detail": "token inválido"})
                    return False
                return True

            def do_GET(self):
                if not self._autorizado():
                    return
                falso.recibido.append((self.path, None))
                if self.path != "/tasks/next?minero=cpu-prueba":
                    self._responder(400, {"error": "falta ?minero=<id>"})
                elif falso.rangos:
                    self._responder(200, falso.rangos.pop(0))
                else:
                    self._responder(204)

            def do_POST(self):
                if not self._autorizado():
                    return
                cuerpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                falso.recibido.append((self.path, cuerpo))
                if self.path == "/hello":
                    self._responder(200, falso.bench or {"registrado": True})
                elif self.path == "/keepalive":
                    self._responder(200, {"cortar": falso.cortar})
                elif self.path == "/bench":
                    self._responder(204)
                else:
                    self._responder(202, {"aceptado": True})

        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Manejador)
        self.url = f"http://127.0.0.1:{self.servidor.server_port}"
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()

    def de(self, ruta):
        return [cuerpo for r, cuerpo in self.recibido if r == ruta]

    def cerrar(self):
        self.servidor.shutdown()


def rango(numero, desde, hasta, prefijo, bloque=VECTOR["texto"]):
    return {"tarea": "t1", "rango": numero, "bloque": bloque, "desde": desde, "hasta": hasta, "prefijo": prefijo}


@pytest.fixture
def armar():
    creados = []

    def _armar(rangos=(), bench=None, token=TOKEN, latido=5):
        falso = GatewayFalso(rangos, bench)
        minero = Minero(Gateway(falso.url, token), "cpu-prueba", procesos=2, espera=0.1, latido=latido)
        creados.append((falso, minero))
        return falso, minero

    yield _armar
    for falso, minero in creados:
        minero.detener()
        falso.cerrar()


def test_minero_nuevo_resuelve_la_tarea_modelo(armar):
    bench = dict(contrato.BENCH, hasta=99_999)  # la de verdad es de 2^24; acá una más corta
    falso, minero = armar(bench=bench)
    minero.presentarse()
    hola = falso.de("/hello")[0]
    assert hola["minero"] == "cpu-prueba" and hola["tipo"] == "cpu" and hola["placa"]
    resultado = falso.de("/bench")[0]
    assert resultado["minero"] == "cpu-prueba"
    assert resultado["segundos"] > 0 and resultado["hashes_por_segundo"] > 0


def test_minero_conocido_no_hace_bench(armar):
    falso, minero = armar()
    minero.presentarse()
    assert falso.de("/bench") == []


def test_devuelve_el_nonce_y_despues_sin_solucion(armar):
    falso, minero = armar([rango(0, 0, 4999, VECTOR["prefijo"]), rango(1, 5000, 9999, "f" * 32)])
    assert minero.un_rango() and minero.un_rango()
    assert not minero.un_rango()  # 204: no hay más trabajo
    con, sin = falso.de("/results")
    assert con == {"tarea": "t1", "rango": 0, "minero": "cpu-prueba",
                   "nonce": VECTOR["nonce"], "hash": VECTOR["block_hash"]}
    assert sin == {"tarea": "t1", "rango": 1, "minero": "cpu-prueba", "sin_solucion": True}


def test_si_el_gateway_dice_cortar_deja_el_rango_sin_resultado(armar):
    falso, minero = armar([rango(0, 0, 10**9, "f" * 32)], latido=0.2)
    falso.cortar = True
    threading.Thread(target=minero.trabajar, daemon=True).start()
    limite = time.monotonic() + 10
    while time.monotonic() < limite and len([r for r, _ in falso.recibido if r.startswith("/tasks/next")]) < 2:
        time.sleep(0.1)
    assert len([r for r, _ in falso.recibido if r.startswith("/tasks/next")]) >= 2  # volvió a pedir trabajo
    assert falso.de("/results") == []
    aviso = falso.de("/keepalive")[0]
    assert aviso["minero"] == "cpu-prueba" and aviso["rango"] == {"tarea": "t1", "rango": 0}


def test_token_invalido(armar):
    _, minero = armar(token="otro")
    with pytest.raises(TokenInvalido):
        minero.presentarse()
