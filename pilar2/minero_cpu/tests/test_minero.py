import json
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from comun import contrato
from comun.tests.claves import CUENTA, PRIVADA, cadena_actual
from minero_cpu import busqueda
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


def test_el_hashrate_es_el_del_ultimo_rango_aunque_el_minero_quede_parado(monkeypatch):
    reloj = [100.0]
    monkeypatch.setattr(busqueda.time, "monotonic", lambda: reloj[0])

    class Falso(busqueda.Ritmo):
        parar, shares = threading.Event(), busqueda.Shares()

        def _recorrer(self, *_):
            reloj[0] += 1
            self.probados = 500                          # a mitad del rango: 500 h/s
            assert self.hashes_por_segundo() == 500
            reloj[0] += 1
            self.probados = 2_000
            return None, False

    b = Falso()
    assert b.hashes_por_segundo() == 0                   # todavía no buscó
    b.buscar("bloque", 0, 1_999, "0")
    assert b.hashes_por_segundo() == 1_000
    reloj[0] += 3_600                                    # una hora sin rangos: antes daba 0,55 h/s
    assert b.hashes_por_segundo() == 1_000


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
        self.rechazados = []      # las rutas que contestó con 403
        self.cortar = False
        self.presentarse = False  # lo que contesta el keep-alive, hasta el próximo /bench
        self.genesis = cadena_actual()  # v4: lo que contesta GET /cadena y contra lo que verifica las firmas
        self.lecturas_cadena = 0
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
                minero, motivo = contrato.verificar_pedido(self.headers, metodo, self.path, cuerpo, cadena=falso.genesis)
                if motivo:
                    self._responder(401, {"error": motivo})
                    return None
                if minero not in falso.aprobados:
                    falso.rechazados.append(self.path)
                    self._responder(403, {"error": "no aprobado"})
                    return None
                return minero

            def do_GET(self):
                if self.path == "/cadena":
                    falso.lecturas_cadena += 1
                    return self._responder(200, {"genesis": falso.genesis})
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
                    self._responder(200, {"cortar": falso.cortar, **({"presentarse": True} if falso.presentarse else {})})
                elif self.path == "/bench":
                    falso.presentarse = False
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


def test_bench_corto_calienta_y_mide_despues(armar):
    """Una vuelta corta (GPU): la primera, con la placa fría, no cuenta para el bench."""
    class BuscadorFrio:
        def __init__(self):
            self.vueltas = 0

        def buscar(self, bloque, desde, hasta, prefijo):
            self.vueltas += 1
            time.sleep(0.2 if self.vueltas == 1 else 0.01)
            return None, False

        def cortar(self):
            pass

        def cerrar(self):
            pass

    falso, minero = armar(bench=dict(contrato.BENCH, hasta=9_999))
    minero.buscador.cerrar()
    minero.buscador = BuscadorFrio()
    minero.presentarse()
    resultado = falso.de("/bench")[0]
    assert minero.buscador.vueltas > 2
    assert resultado["hashes_por_segundo"] > 10_000 / 0.05  # con la vuelta fría saldría 50 000


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


def test_si_la_cadena_se_reinicia_relee_el_genesis_y_repite_el_pedido(armar):
    """v4: lo firmado lleva el génesis. Con la cadena nueva la firma vieja da 401: el minero lo relee y sigue."""
    falso, minero = armar()
    minero.presentarse()
    assert falso.lecturas_cadena == 1
    falso.genesis = "ab" * 16  # la cadena se reinició
    minero.presentarse()
    assert falso.lecturas_cadena == 2 and minero.gateway.cadena == "ab" * 16
    assert len(falso.de("/hello")) == 2


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
    # BC-95: la pública queda al lado (en una VM la publica arrancar.sh; la privada no sale).
    assert (tmp_path / "claves" / "minero.clave.pub").read_text().strip() == contrato.clave_publica(primera)


def test_la_clave_de_la_semilla_depende_del_alias():
    uno, otro = cargar_clave("minero-gpu-0", semilla="s"), cargar_clave("minero-gpu-1", semilla="s")
    assert uno != otro and cargar_clave("minero-gpu-0", semilla="s") == uno
    assert cargar_clave("x", privada=PRIVADA["minero"], semilla="s") == PRIVADA["minero"]
    with pytest.raises(ValueError):
        cargar_clave("x", privada="no es una clave")


def esperar(condicion, segundos=10):
    limite = time.monotonic() + segundos
    while time.monotonic() < limite and not condicion():
        time.sleep(0.05)
    return condicion()


def test_si_el_keepalive_pide_presentarse_repite_el_hello_y_el_bench(armar):
    """BC-71: la cadena se reinició con el minero andando y el gateway perdió su tipo y su bench."""
    falso, minero = armar(bench=dict(contrato.BENCH, hasta=9_999), latido=0.1)
    threading.Thread(target=minero.trabajar, daemon=True).start()
    assert esperar(lambda: falso.de("/keepalive"))
    assert falso.de("/hello") == []

    falso.presentarse = True
    assert esperar(lambda: falso.de("/bench"))
    hola = falso.de("/hello")[0]
    assert hola["alias"] == "cpu-prueba" and hola["tipo"] == "cpu"
    time.sleep(0.5)
    assert len(falso.de("/bench")) == 1  # una vez presentado no lo vuelve a hacer


def test_con_403_vuelve_al_hello_con_su_alias_y_hace_el_bench_cuando_lo_aprueban(armar):
    """Rechazado a mitad de camino (cadena reiniciada): deja de pedir rangos e insiste sólo con el /hello."""
    falso, minero = armar([rango(0, 5000, 9999, "f" * 32)], bench=dict(contrato.BENCH, hasta=9_999), latido=0.1)
    falso.aprobados.clear()
    threading.Thread(target=minero.trabajar, daemon=True).start()
    assert esperar(lambda: falso.rechazados.count("/hello") >= 3)  # espera_aprobacion es 0,2 s en los tests
    principal = [r for r in falso.rechazados if r != "/keepalive"]
    assert principal[0] == "/tasks/next" and set(principal[1:]) == {"/hello"}

    falso.aprobados.add(CUENTA["minero"])
    assert esperar(lambda: falso.de("/bench") and falso.de("/results"))
    assert falso.de("/hello")[0]["alias"] == "cpu-prueba"
    assert [r for r, _ in falso.recibido].index("/hello") < [r for r, _ in falso.recibido].index("/tasks/next")


class GatewayDeEpocas:
    """Contesta /epocas/<n> con lo publicado; anota lo que se pidió."""

    cuenta = CUENTA["minero"]

    def __init__(self, publicadas):
        self.publicadas, self.pedidos = publicadas, []

    def pedir(self, metodo, ruta, cuerpo=None):
        self.pedidos.append(ruta)
        epoca = int(ruta.rsplit("/", 1)[1])
        if epoca not in self.publicadas:
            return 200, {"epoca": epoca, "publicada": False, "shares": 0}
        return 200, {"epoca": epoca, "publicada": True, "shares": self.publicadas[epoca]}


def test_el_minero_compara_los_shares_aceptados_con_los_publicados_al_cerrar_la_epoca():
    """BC-82: si en la lista publicada le contaron menos de los que le aceptó el gateway, lo deja en el log."""
    minero = Minero(GatewayDeEpocas({0: 7, 1: 3}), "prueba", procesos=1)
    for altura, aceptados in ((3, 4), (9, 3), (12, 5)):
        minero._sumar_aceptados({"altura": altura}, {"shares": aceptados})
    minero.revisar_epocas(1)       # la época 0 se pagó: 7 aceptados, 7 publicados
    assert minero.contados_de_menos == [] and minero.aceptados == {1: 5}
    minero.revisar_epocas(1)       # no vuelve a preguntar por la 0
    minero.revisar_epocas(2)       # la 1: 5 aceptados, 3 publicados
    assert minero.contados_de_menos == [(1, 5, 3)] and minero.aceptados == {}
    assert minero.gateway.pedidos == ["/epocas/0", "/epocas/1"]
    minero.buscador.cerrar()


def test_si_el_gateway_no_contesta_la_epoca_el_minero_sigue():
    """La consulta de la época es un control: un gateway caído no puede frenar el trabajo."""
    class Caido(GatewayDeEpocas):
        def pedir(self, metodo, ruta, cuerpo=None):
            raise OSError("conexión rechazada")
    minero = Minero(Caido({}), "prueba", procesos=1)
    minero._sumar_aceptados({"altura": 3}, {"shares": 2})
    minero.revisar_epocas(1)
    assert minero.aceptados == {0: 2}  # queda para la próxima
    minero.buscador.cerrar()
