"""`python -m claves saldo --verificar` (BC-84): baja la cadena, la verifica acá y compara con el backend y los nodos."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from claves import herramienta
from comun import contrato, estado
from comun.tests.cadena import Cadena
from comun.tests.claves import CUENTA, FECHA, PRIVADA, carga, firmada, cadena_actual


class Servidor:
    """Un backend (bloques, épocas, prueba, cuentas) y un nodo (atestación) sobre una Cadena, que pueden mentir."""

    def __init__(self, cadena):
        self.cadena, self.saldo_trucho, self.atestacion = cadena, None, None
        falso = self

        class Manejador(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                ruta = urlparse(self.path)
                c = falso.cadena
                if ruta.path == "/api/bloques":
                    q = parse_qs(ruta.query)
                    desde, limite = int(q["desde"][0]), int(q["limite"][0])
                    cuerpo = c.bloques[desde:desde + limite]
                elif ruta.path.startswith("/api/epocas/"):
                    detalle = c.publicadas.get(int(ruta.path.rsplit("/", 1)[1]))
                    cuerpo = {"detalle": detalle} if detalle else None
                elif ruta.path.startswith("/api/prueba/"):
                    cuenta = ruta.path.rsplit("/", 1)[1]
                    cuerpo = {"altura": c.estado.altura, "block_hash": c.estado.punta, **c.estado.prueba(cuenta)}
                elif ruta.path.startswith("/api/cuentas/"):
                    cuenta = ruta.path.rsplit("/", 1)[1]
                    saldo = falso.saldo_trucho if falso.saldo_trucho is not None else c.estado.saldos.get(cuenta, 0)
                    cuerpo = {"cuenta": cuenta, "saldo": saldo}
                elif ruta.path == "/api/atestacion":
                    cuerpo = falso.atestacion or atestar(PRIVADA["nodo"], c.estado)
                else:
                    cuerpo = None
                datos = json.dumps(cuerpo).encode()
                self.send_response(200 if cuerpo is not None else 404)
                self.send_header("Content-Length", str(len(datos)))
                self.end_headers()
                self.wfile.write(datos)

        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Manejador)
        self.url = f"http://127.0.0.1:{self.servidor.server_port}"
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()


def atestar(privada, e, **cambios):
    datos = {"nodo": contrato.clave_publica(privada), "altura": e.altura, "punta": e.punta, "raiz": e.raiz(),
             "tiempo": int(time.time()), **cambios}
    datos["firma"] = contrato.firmar(privada, contrato.mensaje_atestacion(**{k: datos[k] for k in
                                                                            ("nodo", "altura", "punta", "raiz", "tiempo")}))
    return datos


@pytest.fixture
def servidor():
    c = Cadena()
    c.agregar([firmada("alice", "bob", 30), carga(1, "carol", 7)])
    c.agregar([contrato.alta(PRIVADA["organizador"], 2, CUENTA["carol"], "usuario", "carol", FECHA, cadena=cadena_actual())])
    s = Servidor(c)
    yield s
    s.servidor.shutdown()


def verificar(servidor, nodos=()):
    salidas = []
    codigo = herramienta.verificar_saldo(herramienta.Backend(servidor.url), CUENTA["alice"], list(nodos),
                                         salida=salidas.append)
    return codigo, salidas


def test_todo_coincide(servidor):
    codigo, salidas = verificar(servidor, [servidor.url])
    assert codigo == 0, salidas
    assert salidas[1] == f"Saldo de {CUENTA['alice']}: 70 (calculado acá)"
    assert sum(s.startswith("✓") for s in salidas) == 3


def test_un_backend_que_infla_el_saldo_se_nota(servidor):
    servidor.saldo_trucho = 1_000_000
    codigo, salidas = verificar(servidor)
    assert codigo == 1 and "✗ backend /api/cuentas: saldo 1000000" in salidas


def test_un_nodo_con_otra_punta_o_una_atestacion_trucha_se_nota(servidor):
    e = servidor.cadena.estado
    servidor.atestacion = atestar(PRIVADA["nodo"], e, punta="f" * 32)
    assert verificar(servidor, [servidor.url])[0] == 1
    servidor.atestacion = {**atestar(PRIVADA["nodo"], e), "raiz": "0" * 32}  # tocada después de firmar
    codigo, salidas = verificar(servidor, [servidor.url])
    assert codigo == 1 and any("no está bien firmada" in s for s in salidas)


def test_un_bloque_adulterado_no_verifica(servidor):
    servidor.cadena.bloques[1] = json.loads(json.dumps(servidor.cadena.bloques[1]))
    servidor.cadena.bloques[1]["transactions"][1]["monto"] = 1
    codigo, salidas = verificar(servidor)
    assert codigo == 1 and salidas[0].startswith("✗ el bloque 1 NO verifica")


def test_desde_la_linea_de_comandos(servidor, tmp_path):
    archivo = tmp_path / "alice.clave"
    archivo.write_text(PRIVADA["alice"])
    assert herramienta.principal(["saldo", str(archivo), "--url", servidor.url, "--verificar", "--nodo", servidor.url],
                                 salida=lambda _: None) == 0
    assert estado.Estado  # usa comun/estado.py, el mismo código que los nodos
