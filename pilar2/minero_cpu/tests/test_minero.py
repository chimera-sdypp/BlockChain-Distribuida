import json
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from comun import contrato
from comun.tests.claves import CUENTA, PRIVADA
from minero_cpu.busqueda import Buscador
from minero_cpu.minero import ClaveRechazada, Gateway, Minero, cargar_clave

VECTOR = json.loads((Path(__file__).parents[2] / "comun" / "vector_prueba.json").read_text())
PREFIJO = VECTOR["tarea"]["prefijo"]


def menor_nonce(bloque, desde, hasta, prefijo):
    return next((n for n in range(desde, hasta + 1) if contrato.cumple(contrato.hash_bloque(bloque, n), prefijo)), None)


@pytest.fixture(scope="module")
def buscador():
    b = Buscador(procesos=2)
    yield b
    b.cerrar()


# --- Búsqueda ---------------------------------------------------------------------------------
def test_encuentra_el_menor_nonce_del_vector(buscador):
    nonce, cortado = buscador.buscar(VECTOR["texto"], 0, 200_000, PREFIJO)
    assert (nonce, cortado) == (VECTOR["nonce"], False)
    assert contrato.hash_bloque(VECTOR["texto"], nonce) == VECTOR["block_hash"]


def shares_esperados(bloque, desde, hasta, prefijo):
    corto = contrato.prefijo_share(prefijo)
    return [n for n in range(desde, hasta + 1) if contrato.cumple(contrato.hash_bloque(bloque, n), corto)]


def test_junta_los_shares_hasta_la_solucion(buscador):
    # Shares: los que cumplen el prefijo menos un carácter («000»), hasta la solución incluida.
    buscador.buscar(VECTOR["texto"], 0, 200_000, PREFIJO)
    esperados = shares_esperados(VECTOR["texto"], 0, VECTOR["nonce"], PREFIJO)
    assert buscador.shares.tomar() == esperados and esperados[-1] == VECTOR["nonce"]
    assert buscador.shares.tomar() == []  # ya se los llevaron


# Una «z» no aparece nunca en un hash: con este prefijo no hay solución, sólo shares («00», uno cada 256).
SIN_SOLUCION_CON_SHARES = "00z"


def test_no_junta_mas_shares_que_el_tope_por_rango(buscador):
    assert buscador.buscar("prueba;", 0, 200_000, SIN_SOLUCION_CON_SHARES) == (None, False)  # unos 780 shares
    assert len(buscador.shares.tomar()) == contrato.SHARES_POR_RANGO


def test_con_prefijo_de_un_caracter_no_hay_shares(buscador):
    buscador.buscar("prueba;", 0, 5000, "f" * 1)
    assert buscador.shares.tomar() == []


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
    assert buscador.buscar(VECTOR["texto"], 54_000, 60_000, PREFIJO) == (VECTOR["nonce"], False)


# --- Contra un gateway de mentira con la API de CONTRATO.md -----------------------------------
class GatewayFalso:
    """Verifica la firma de cada pedido (401) y que el minero esté aprobado (403), como el de verdad."""

    def __init__(self, rangos=(), bench=None, aprobados=(CUENTA["minero"],)):
        self.rangos = list(rangos)
        self.bench = bench
        self.aprobados = set(aprobados)
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

            def _firmado(self, metodo, cuerpo):
                minero, motivo = contrato.verificar_pedido(self.headers, metodo, self.path, cuerpo)
                if motivo:
                    self._responder(401, {"error": motivo})
                    return None
                if minero not in falso.aprobados:
                    self._responder(403, {"error": "no aprobado"})
                    return None
                return minero

            def do_GET(self):
                if not self._firmado("GET", b""):
                    return
                falso.recibido.append((self.path, None))
                if self.path != "/tasks/next":
                    self._responder(404)
                elif falso.rangos:
                    self._responder(200, falso.rangos.pop(0))
                else:
                    self._responder(204)

            def do_POST(self):
                crudo = self.rfile.read(int(self.headers["Content-Length"]))
                if not self._firmado("POST", crudo):
                    return
                cuerpo = json.loads(crudo)
                falso.recibido.append((self.path, cuerpo))
                if self.path == "/hello":
                    self._responder(200, falso.bench or {"registrado": True})
                elif self.path == "/keepalive":
                    self._responder(200, {"cortar": falso.cortar})
                elif self.path == "/bench":
                    self._responder(204)
                else:
                    self._responder(202, {"shares": len(cuerpo.get("shares", []))})

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

    def _armar(rangos=(), bench=None, nombre="minero", latido=5, aprobados=(CUENTA["minero"],)):
        falso = GatewayFalso(rangos, bench, aprobados)
        minero = Minero(Gateway(falso.url, PRIVADA[nombre]), "cpu-prueba", procesos=2, espera=0.1, latido=latido,
                        espera_aprobacion=0.2)
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
    assert hola["minero"] == CUENTA["minero"] and hola["tipo"] == "cpu" and hola["placa"]
    resultado = falso.de("/bench")[0]
    assert resultado["minero"] == CUENTA["minero"]
    assert resultado["segundos"] > 0 and resultado["hashes_por_segundo"] > 0


def test_minero_conocido_no_hace_bench(armar):
    falso, minero = armar()
    minero.presentarse()
    assert falso.de("/bench") == []


def test_devuelve_el_nonce_con_sus_shares_y_despues_sin_solucion(armar):
    falso, minero = armar([rango(0, 50_000, 59_999, PREFIJO), rango(1, 5000, 9999, "f" * 32)])
    assert minero.un_rango() and minero.un_rango()
    assert not minero.un_rango()  # 204: no hay más trabajo
    con, sin = falso.de("/results")
    assert con == {"tarea": "t1", "rango": 0, "minero": CUENTA["minero"], "nonce": VECTOR["nonce"],
                   "hash": VECTOR["block_hash"],
                   "shares": shares_esperados(VECTOR["texto"], 50_000, VECTOR["nonce"], PREFIJO)}
    assert sin == {"tarea": "t1", "rango": 1, "minero": CUENTA["minero"], "sin_solucion": True, "shares": []}


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
    assert aviso["minero"] == CUENTA["minero"] and aviso["rango"] == {"tarea": "t1", "rango": 0}
    assert aviso["shares"] == []


def test_el_keepalive_lleva_los_shares_que_va_juntando(armar):
    # Un rango largo sin solución: mientras mina, cada keep-alive manda los shares nuevos.
    falso, minero = armar([rango(0, 0, 3_000_000, SIN_SOLUCION_CON_SHARES, bloque="prueba;")], latido=0.2)
    threading.Thread(target=minero.trabajar, daemon=True).start()
    limite = time.monotonic() + 20
    while time.monotonic() < limite and not falso.de("/results"):
        time.sleep(0.1)
    en_avisos = [n for aviso in falso.de("/keepalive") for n in aviso.get("shares", [])]
    en_resultado = falso.de("/results")[0]["shares"]
    assert en_avisos, "ningún keep-alive llevó shares"
    todos = en_avisos + en_resultado
    assert len(todos) == len(set(todos)) == contrato.SHARES_POR_RANGO  # ninguno dos veces, y el tope por rango
    assert all(contrato.cumple(contrato.hash_bloque("prueba;", n), "00") for n in todos)


def test_una_firma_que_el_gateway_no_acepta_corta_el_minero(armar, monkeypatch):
    _, minero = armar()
    firmar = contrato.firmar_pedido
    # Un reloj atrasado una hora: el gateway rechaza la firma (401) y el minero se corta.
    monkeypatch.setattr(contrato, "firmar_pedido", lambda *a, **k: firmar(*a, **{**k, "tiempo": int(time.time()) - 3600}))
    with pytest.raises(ClaveRechazada, match="reloj"):
        minero.presentarse()


def test_sin_aprobar_espera_y_sigue_cuando_lo_aprueban(armar):
    falso, minero = armar(aprobados=())
    threading.Timer(0.5, lambda: falso.aprobados.add(CUENTA["minero"])).start()
    minero.presentarse()
    assert falso.de("/hello")[0]["minero"] == CUENTA["minero"]


def test_la_clave_se_genera_una_vez_y_se_guarda_con_permisos_600(tmp_path):
    archivo = tmp_path / "claves" / "minero.clave"
    primera = cargar_clave("cpu-1", archivo=archivo)
    assert archivo.stat().st_mode & 0o777 == 0o600
    assert cargar_clave("cpu-1", archivo=archivo) == primera


def test_la_clave_de_la_semilla_depende_del_alias():
    uno, otro = cargar_clave("minero-gpu-0", semilla="s"), cargar_clave("minero-gpu-1", semilla="s")
    assert uno != otro and cargar_clave("minero-gpu-0", semilla="s") == uno
    assert cargar_clave("x", privada=PRIVADA["minero"], semilla="s") == PRIVADA["minero"]
    with pytest.raises(ValueError):
        cargar_clave("x", privada="no es una clave")
