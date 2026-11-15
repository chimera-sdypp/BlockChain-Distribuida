"""La herramienta de claves (BC-55): el «servicio que genera las claves» que pidió el profe.

    python -m claves generar alice.clave            # la privada queda en el archivo (600); muestra la pública
    python -m claves publica alice.clave
    python -m claves exportar alice.clave           # JWK para importar en el navegador (la sección «Transferir»)
    python -m claves firmar alice.clave --destino <cuenta> --monto 5 --url http://localhost [--enviar]
    python -m claves cargar organizador.clave --destino <cuenta> --monto 1000 --url http://localhost
    python -m claves aprobar <cuenta> --tipo minero --nombre gpu-tomas    # escribe en Redis (REDIS_*)
    python -m claves rafaga --url http://localhost -n 20 alice.clave bob.clave carol.clave

`aprobar` habla directo con Redis: es lo que hace el organizador hasta que esté el panel (BC-58).
El resto le habla al backend como cualquier cliente. La privada no sale nunca del archivo: lo que se
manda es la transacción firmada.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from comun import config, contrato

CERRADOS = ("confirmada", "rechazada")


def guardar_privada(ruta, privada):
    """Crea el archivo con permisos 600; si ya existe, no lo pisa."""
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as salida:
        salida.write(privada + "\n")


def leer_privada(ruta):
    privada = Path(ruta).read_text().strip()
    contrato.clave_publica(privada)  # que sea una clave Ed25519
    return privada


def _b64url(datos):
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def jwk(privada):
    """La privada en JWK (RFC 8037), como la importa WebCrypto: {kty: OKP, crv: Ed25519, d, x}."""
    return {"kty": "OKP", "crv": "Ed25519", "d": _b64url(bytes.fromhex(privada)),
            "x": _b64url(bytes.fromhex(contrato.clave_publica(privada)))}


class Backend:
    """Lo poco del backend que usa la herramienta: la cuenta (para el nonce) y mandar transacciones."""

    def __init__(self, url, timeout=10):
        self.url, self.timeout = url.rstrip("/"), timeout

    def pedir(self, metodo, ruta, cuerpo=None):
        datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
        pedido = urllib.request.Request(self.url + ruta, data=datos, method=metodo,
                                        headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as respuesta:
                return respuesta.status, json.loads(respuesta.read() or b"null")
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read() or b"null")
            except ValueError:
                return error.code, None

    def nonce(self, cuenta):
        """El último nonce aceptado de la cuenta (pendiente o confirmado): el próximo es +1."""
        codigo, datos = self.pedir("GET", f"/api/cuentas/{cuenta}")
        if codigo != 200:
            raise SystemExit(f"No se pudo leer la cuenta {cuenta}: {codigo} {datos}")
        return datos["nonce"]

    def mandar(self, tx):
        return self.pedir("POST", "/api/transacciones", tx)

    def estado(self, id_):
        codigo, tx = self.pedir("GET", f"/api/transacciones/{id_}")
        return tx if codigo == 200 else None


def aprobar(redis, cuenta, tipo, nombre, ahora=None):
    """Lo que hace el organizador al aprobar una cuenta: entra en `cuentas` (contrato v2)."""
    if not contrato.cuenta_valida(cuenta):
        raise ValueError("la cuenta tiene que ser una clave pública Ed25519 en hex (64 caracteres)")
    if tipo not in contrato.TIPOS_CUENTA:
        raise ValueError(f"tipo: {' | '.join(contrato.TIPOS_CUENTA)}")
    if nombre and not contrato.nombre_valido(nombre):
        raise ValueError("nombre: de 1 a 32 letras, números, «_», «.» o «-»")
    datos = {"tipo": tipo, "nombre": nombre, "alta": int(time.time() if ahora is None else ahora)}
    redis.hset(contrato.CUENTAS, cuenta, json.dumps(datos))
    redis.hdel(contrato.ALTAS_PENDIENTES, cuenta)
    return datos


def rafaga(backend, privadas, cantidad, monto_maximo=5, esperar=600, salida=print):
    """N transferencias chiquitas en ronda entre las cuentas (A → B → C → A…), firmadas y con su nonce
    llevado acá, y sigue cada una hasta que se confirma o se rechaza. Devuelve {id: estado}."""
    cuentas = [contrato.clave_publica(p) for p in privadas]
    nonces = {c: backend.nonce(c) for c in cuentas}
    ids = []
    for i in range(cantidad):
        privada, origen, destino = privadas[i % len(privadas)], cuentas[i % len(cuentas)], cuentas[(i + 1) % len(cuentas)]
        nonces[origen] += 1
        tx = contrato.transaccion(privada, destino, 1 + i % monto_maximo, nonces[origen])
        codigo, respuesta = backend.mandar(tx)
        if codigo != 202:
            nonces[origen] -= 1
            salida(f"✗ {origen[:8]}… → {destino[:8]}… {tx['monto']}: {codigo} {respuesta}")
            continue
        ids.append(respuesta["id"])
        salida(f"→ {origen[:8]}… → {destino[:8]}… {tx['monto']:>2}  nonce {tx['nonce']}  {respuesta['id'][:10]}")
    estados, limite = {}, time.monotonic() + esperar
    while ids and time.monotonic() < limite:
        estados = {id_: (backend.estado(id_) or {}).get("estado", "?") for id_ in ids}
        cuenta = {e: list(estados.values()).count(e) for e in sorted(set(estados.values()))}
        salida("  " + " · ".join(f"{e}={n}" for e, n in cuenta.items()))
        if all(e in CERRADOS for e in estados.values()):
            break
        time.sleep(5)
    return estados


def principal(argumentos=None, salida=print):
    parser = argparse.ArgumentParser(prog="python -m claves", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("generar", help="genera un par y guarda la privada en ARCHIVO (permisos 600)")
    p.add_argument("archivo")
    p = sub.add_parser("publica", help="la cuenta (clave pública) de ARCHIVO")
    p.add_argument("archivo")
    p = sub.add_parser("exportar", help="la privada de ARCHIVO en JWK, para importarla en el navegador")
    p.add_argument("archivo")
    for nombre, ayuda in (("firmar", "arma y firma una transferencia"), ("cargar", "una carga de saldo del organizador")):
        p = sub.add_parser(nombre, help=ayuda)
        p.add_argument("archivo", help="la clave del origen (en «cargar», la del organizador)")
        p.add_argument("--destino", required=True, help="la cuenta destino (64 hex)")
        p.add_argument("--monto", type=int, required=True)
        p.add_argument("--nonce", type=int, help="si no está, lo pide al backend (--url): el último + 1")
        p.add_argument("--url", help="el backend (http://<Ingress>)")
        p.add_argument("--enviar", action="store_true", help="la manda al backend (si no, sólo la muestra)")
    p = sub.add_parser("aprobar", help="aprueba una cuenta en Redis (lo que hace el organizador hasta BC-58)")
    p.add_argument("cuenta")
    p.add_argument("--tipo", choices=contrato.TIPOS_CUENTA, default="usuario")
    p.add_argument("--nombre", default="")
    p = sub.add_parser("rafaga", help="N transferencias en ronda entre las cuentas, y las sigue")
    p.add_argument("archivos", nargs="+", help="las claves de las cuentas (necesitan saldo y estar aprobadas)")
    p.add_argument("--url", required=True)
    p.add_argument("-n", "--cantidad", type=int, default=10)
    p.add_argument("--esperar", type=int, default=600, help="segundos siguiendo el estado")

    a = parser.parse_args(argumentos)
    if a.comando == "generar":
        privada, publica = contrato.nueva_clave()
        guardar_privada(a.archivo, privada)
        salida(publica)
    elif a.comando == "publica":
        salida(contrato.clave_publica(leer_privada(a.archivo)))
    elif a.comando == "exportar":
        salida(json.dumps(jwk(leer_privada(a.archivo))))
    elif a.comando in ("firmar", "cargar"):
        privada = leer_privada(a.archivo)
        backend = Backend(a.url) if a.url else None
        if a.nonce is None and backend is None:
            parser.error("hace falta --nonce o --url (para pedirle el último al backend)")
        origen = contrato.EMISION if a.comando == "cargar" else contrato.clave_publica(privada)
        nonce = a.nonce if a.nonce is not None else backend.nonce(origen) + 1
        if a.comando == "cargar":
            tx = contrato.carga(privada, nonce, a.destino, a.monto, int(time.time()))
        else:
            tx = contrato.transaccion(privada, a.destino, a.monto, nonce)
        salida(json.dumps(tx))
        if a.enviar:
            if backend is None:
                parser.error("--enviar necesita --url")
            codigo, respuesta = backend.mandar(tx)
            salida(f"{codigo} {json.dumps(respuesta)}")
            return 0 if codigo == 202 else 1
    elif a.comando == "aprobar":
        from comun import redisdb
        datos = aprobar(redisdb.conectar(config.cargar("claves")), a.cuenta, a.tipo, a.nombre)
        salida(f"aprobada: {a.cuenta} {json.dumps(datos)}")
    elif a.comando == "rafaga":
        estados = rafaga(Backend(a.url), [leer_privada(r) for r in a.archivos], a.cantidad, esperar=a.esperar,
                         salida=salida)
        return 0 if estados and all(e in CERRADOS for e in estados.values()) else 1
    return 0


def main():
    try:
        sys.exit(principal())
    except (FileExistsError, FileNotFoundError, ValueError) as error:
        sys.exit(f"Error: {error}")
