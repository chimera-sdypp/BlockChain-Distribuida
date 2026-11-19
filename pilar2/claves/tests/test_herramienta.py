"""La herramienta de claves (la billetera de línea de comandos, BC-78): archivos, JWK, alta, transferir, las actas
del organizador y la ráfaga, contra un backend de mentira."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claves import herramienta
from comun import contrato
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
        self.nonces, self.recibidas, self.pedidos, self.estados = dict(nonces or {}), [], [], {}
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
                if self.path == "/api/altas/pendientes":
                    return self._responder(200, {"pendientes": [{"cuenta": CUENTA["zoe"], "tipo": "minero",
                                                                 "nombre": "gpu-zoe", "pedida": 1}]})
                id_ = self.path.rsplit("/", 1)[1]
                estado = falso.estados.get(id_, "confirmada")
                if estado == "pendiente":  # la próxima vez que se pregunte, ya entró en un bloque
                    falso.estados[id_] = "confirmada"
                return self._responder(200, {"id": id_, "estado": estado})

            def do_POST(self):
                cuerpo = self.rfile.read(int(self.headers["Content-Length"]))
                datos = json.loads(cuerpo)
                if self.path != "/api/transacciones":
                    falso.pedidos.append((self.path, datos, dict(self.headers)))
                    if self.path.startswith("/api/organizador/"):
                        quien, motivo = contrato.verificar_pedido(self.headers, "POST", self.path, cuerpo,
                                                                  quien=contrato.ORGANIZADOR)
                        return self._responder(403 if motivo else 200, {"error": motivo} if motivo else {"ok": True})
                    errores = contrato.validar_pedido_alta(datos)
                    return self._responder(400 if errores else 202, {"errores": errores} if errores else {})
                tx = datos
                falso.recibidas.append(tx)
                errores = contrato.validar_de_entrada(tx, CUENTA["organizador"])
                if errores:
                    return self._responder(400, {"errores": errores})
                es_propia = tx["origen"] == contrato.EMISION or contrato.es_acta(tx)
                id_ = tx["id"] if es_propia else contrato.id_transaccion(tx)
                falso.estados.setdefault(id_, "pendiente")
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


def test_alta_manda_el_pedido_firmado_sin_pasar_por_ninguna_pagina(backend, archivos):
    assert herramienta.principal(["alta", str(archivos["alice"]), "--tipo", "usuario", "--nombre", "alice",
                                  "--url", backend.url], salida=lambda _: None) == 0
    [(ruta, pedido, _)] = backend.pedidos
    assert ruta == "/api/altas" and pedido == contrato.pedido_alta(PRIVADA["alice"], "usuario", "alice")


def test_transferir_usa_el_nonce_siguiente_y_espera_que_la_valide_el_nct(backend, archivos):
    salidas = []
    assert herramienta.principal(["transferir", str(archivos["alice"]), "--destino", CUENTA["bob"], "--monto", "3",
                                  "--url", backend.url], salida=salidas.append) == 0
    [tx] = backend.recibidas
    assert tx["nonce"] == 5 and salidas == [f"✓ {tx['id']}: pendiente"]


def test_aprobar_firma_el_alta_y_la_carga_con_los_nonces_de_emision_que_siguen(backend, archivos):
    salidas = []
    codigo = herramienta.principal(["aprobar", str(archivos["organizador"]), "--cuenta", CUENTA["zoe"], "--tipo",
                                    "usuario", "--nombre", "zoe", "--carga", "500", "--url", backend.url],
                                   salida=salidas.append)
    alta, carga_ = backend.recibidas
    assert codigo == 0 and (alta["id"], carga_["id"]) == ("alta-3", "carga-4")
    assert alta["datos"] == "usuario:zoe" and contrato.validar_acta(alta, CUENTA["organizador"]) == []
    assert carga_["destino"] == CUENTA["zoe"] and carga_["monto"] == 500
    assert salidas == ["✓ alta-3: pendiente", "✓ carga-4: pendiente"]


def test_un_minero_no_lleva_carga_y_un_nombre_malo_no_se_manda(backend, archivos):
    with pytest.raises(ValueError):
        herramienta.aprobar(herramienta.Backend(backend.url), PRIVADA["organizador"], CUENTA["zoe"], "minero", "gpu",
                            carga=10)
    with pytest.raises(ValueError):
        herramienta.aprobar(herramienta.Backend(backend.url), PRIVADA["organizador"], CUENTA["zoe"], "usuario", "a b")
    assert backend.recibidas == []


def test_baja_y_permiso_son_actas_del_organizador(backend, archivos):
    herramienta.principal(["baja", str(archivos["organizador"]), "--cuenta", CUENTA["bob"], "--motivo", "se fue",
                           "--url", backend.url], salida=lambda _: None)
    herramienta.principal(["permiso", str(archivos["organizador"]), "--clave", CUENTA["escalador"], "--prefijo",
                           "minero-cpu-", "--url", backend.url], salida=lambda _: None)
    baja, permiso = backend.recibidas
    assert (baja["id"], baja["datos"], permiso["id"], permiso["datos"]) == ("baja-3", "se fue", "permiso-3",
                                                                            "minero-cpu-")


def test_si_el_nct_la_rechaza_lo_dice_y_sale_con_1(backend, archivos):
    backend.estados["alta-3"] = "rechazada"
    salidas = []
    assert herramienta.aprobar(herramienta.Backend(backend.url), PRIVADA["organizador"], CUENTA["zoe"], "minero",
                               "gpu-1", salida=salidas.append) == 1
    assert salidas[0].startswith("✗ alta-3: rechazada")


def test_rechazar_y_reactivar_van_firmados_como_el_organizador(backend, archivos):
    for comando in ("rechazar", "reactivar"):
        assert herramienta.principal([comando, str(archivos["organizador"]), "--cuenta", CUENTA["zoe"],
                                      "--url", backend.url], salida=lambda _: None) == 0
    assert [ruta for ruta, _, _ in backend.pedidos] == ["/api/organizador/rechazar", "/api/organizador/reactivar"]
    # firmado por otra clave, el panel lo rechaza
    assert herramienta.principal(["rechazar", str(archivos["alice"]), "--cuenta", CUENTA["zoe"], "--url", backend.url],
                                 salida=lambda _: None) == 0  # el falso no compara la cuenta; el de verdad da 403


def test_pendientes_lista_los_pedidos(backend):
    salidas = []
    herramienta.principal(["pendientes", "--url", backend.url], salida=salidas.append)
    assert salidas == [f"{CUENTA['zoe']}  minero    gpu-zoe"]


def test_lee_la_clave_tambien_en_el_jwk_que_baja_la_billetera(tmp_path):
    archivo = tmp_path / "zoe.jwk"
    archivo.write_text(json.dumps(herramienta.jwk(PRIVADA["zoe"])))
    assert herramienta.leer_privada(archivo) == PRIVADA["zoe"]
