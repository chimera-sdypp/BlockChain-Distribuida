"""La herramienta de claves: archivos, JWK, firmar, cargar, aprobar y la ráfaga, contra un backend de mentira."""

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import redis as redislib

from claves import herramienta
from comun import config, contrato
from comun.tests.claves import CUENTA, PRIVADA


def test_generar_guarda_la_privada_con_permisos_600_y_muestra_la_publica(tmp_path):
    archivo, salidas = tmp_path / "alice.clave", []
    herramienta.principal(["generar", str(archivo)], salida=salidas.append)
    privada = archivo.read_text().strip()
    assert archivo.stat().st_mode & 0o777 == 0o600
    assert salidas == [contrato.clave_publica(privada)]
    herramienta.principal(["publica", str(archivo)], salida=salidas.append)
    assert salidas[1] == salidas[0]
    with pytest.raises(FileExistsError):  # no pisa una clave que ya existe
        herramienta.principal(["generar", str(archivo)])


def test_exportar_da_el_jwk_que_importa_webcrypto(tmp_path):
    archivo = tmp_path / "a.clave"
    archivo.write_text(PRIVADA["alice"])
    salidas = []
    herramienta.principal(["exportar", str(archivo)], salida=salidas.append)
    clave = json.loads(salidas[0])
    relleno = lambda t: base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))  # noqa: E731
    assert (clave["kty"], clave["crv"]) == ("OKP", "Ed25519")
    assert relleno(clave["d"]).hex() == PRIVADA["alice"] and relleno(clave["x"]).hex() == CUENTA["alice"]


class BackendFalso:
    """/api/cuentas/<cuenta> y /api/transacciones, con los nonces que va aceptando."""

    def __init__(self, nonces=None):
        self.nonces, self.recibidas = dict(nonces or {}), []
        falso = self

        class Manejador(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _responder(self, codigo, cuerpo):
                datos = json.dumps(cuerpo).encode()
                self.send_response(codigo)
                self.send_header("Content-Length", str(len(datos)))
                self.end_headers()
                self.wfile.write(datos)

            def do_GET(self):
                if self.path.startswith("/api/cuentas/"):
                    cuenta = self.path.rsplit("/", 1)[1]
                    return self._responder(200, {"cuenta": cuenta, "nonce": falso.nonces.get(cuenta, 0)})
                id_ = self.path.rsplit("/", 1)[1]
                return self._responder(200, {"id": id_, "estado": "confirmada"})

            def do_POST(self):
                tx = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                falso.recibidas.append(tx)
                if tx["origen"] == contrato.EMISION:
                    errores = contrato.validar_carga(tx, CUENTA["organizador"])
                else:
                    errores = contrato.validar_transaccion_firmada(tx)
                if errores:
                    return self._responder(400, {"errores": errores})
                id_ = tx["id"] if tx["origen"] == contrato.EMISION else contrato.id_transaccion(tx)
                return self._responder(202, {"id": id_})

        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Manejador)
        self.url = f"http://127.0.0.1:{self.servidor.server_port}"
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()

    def cerrar(self):
        self.servidor.shutdown()


@pytest.fixture
def backend():
    falso = BackendFalso({CUENTA["alice"]: 4, "emision": 2})
    yield falso
    falso.cerrar()


@pytest.fixture
def archivos(tmp_path):
    rutas = {}
    for nombre in ("alice", "bob", "carol", "organizador"):
        rutas[nombre] = tmp_path / f"{nombre}.clave"
        rutas[nombre].write_text(PRIVADA[nombre] + "\n")
    return rutas


def test_firmar_pide_el_nonce_al_backend_y_la_manda(backend, archivos):
    salidas = []
    codigo = herramienta.principal(["firmar", str(archivos["alice"]), "--destino", CUENTA["bob"], "--monto", "5",
                                    "--url", backend.url, "--enviar"], salida=salidas.append)
    [tx] = backend.recibidas
    assert codigo == 0 and tx["nonce"] == 5 and tx["origen"] == CUENTA["alice"] and tx["monto"] == 5
    assert contrato.validar_transaccion_firmada(tx) == []
    assert json.loads(salidas[0]) == tx and salidas[1].startswith("202")


def test_firmar_con_nonce_y_sin_backend_solo_la_muestra(archivos):
    salidas = []
    herramienta.principal(["firmar", str(archivos["alice"]), "--destino", CUENTA["bob"], "--monto", "5",
                           "--nonce", "9"], salida=salidas.append)
    assert json.loads(salidas[0])["nonce"] == 9


def test_cargar_usa_el_numero_siguiente_de_emision(backend, archivos):
    herramienta.principal(["cargar", str(archivos["organizador"]), "--destino", CUENTA["zoe"], "--monto", "1000",
                           "--url", backend.url, "--enviar"], salida=lambda _: None)
    [tx] = backend.recibidas
    assert tx["id"] == "carga-3" and contrato.validar_carga(tx, CUENTA["organizador"]) == []


def test_la_rafaga_lleva_los_nonces_de_cada_cuenta(backend, archivos):
    privadas = [PRIVADA[n] for n in ("alice", "bob", "carol")]
    estados = herramienta.rafaga(herramienta.Backend(backend.url), privadas, 7, salida=lambda _: None)
    assert len(estados) == 7 and set(estados.values()) == {"confirmada"}
    por_origen = {}
    for tx in backend.recibidas:
        por_origen.setdefault(tx["origen"], []).append(tx["nonce"])
    assert por_origen == {CUENTA["alice"]: [5, 6, 7], CUENTA["bob"]: [1, 2], CUENTA["carol"]: [1, 2]}
    assert [tx["destino"] for tx in backend.recibidas[:3]] == [CUENTA["bob"], CUENTA["carol"], CUENTA["alice"]]


@pytest.mark.skipif(os.environ.get("INTEGRACION") != "1", reason="necesita Redis (INTEGRACION=1)")
def test_aprobar_escribe_la_cuenta_y_saca_el_pedido_de_alta():
    c = config.cargar("prueba")
    redis = redislib.Redis(host=c.redis_host, port=c.redis_puerto, password=c.redis_clave or None, db=15,
                           decode_responses=True)
    redis.flushdb()
    try:
        redis.hset("altas:pendientes", CUENTA["minero"], "{}")
        herramienta.aprobar(redis, CUENTA["minero"], "minero", "gpu-tomas", ahora=1793836800)
        assert json.loads(redis.hget("cuentas", CUENTA["minero"])) == {"tipo": "minero", "nombre": "gpu-tomas",
                                                                       "alta": 1793836800}
        assert not redis.hexists("altas:pendientes", CUENTA["minero"])
        with pytest.raises(ValueError):
            herramienta.aprobar(redis, "alice", "usuario", "alice")
    finally:
        redis.flushdb()
