"""La herramienta de claves (BC-55 y BC-78): la billetera de línea de comandos. Nada pasa por una página de la nube.

Para cualquiera:

    python -m claves generar zoe.clave                  # la privada queda en el archivo (600); muestra la pública
    python -m claves publica zoe.clave
    python -m claves exportar zoe.clave                 # JWK, para importar en la billetera del navegador
    python -m claves alta zoe.clave --tipo usuario --nombre zoe --url https://…      # pide el alta
    python -m claves saldo zoe.clave --url https://…    # lo que dice el backend (con --verificar, lo calcula, BC-84)
    python -m claves transferir zoe.clave --destino <cuenta> --monto 5 --url https://…
    python -m claves firmar zoe.clave --destino <cuenta> --monto 5 --nonce 3      # sólo la firma (no la manda)
    python -m claves rafaga --url https://… -n 20 alice.clave bob.clave carol.clave

Para el organizador (contrato v3, BC-79): cada decisión es una transacción firmada con su clave que entra en la
cadena; nada se escribe directo en Redis.

    python -m claves pendientes --url https://…         # los pedidos de alta
    python -m claves aprobar organizador.clave --cuenta <c> --tipo minero --nombre gpu-1 [--carga 500] --url …
    python -m claves baja organizador.clave --cuenta <c> --motivo "…" --url …
    python -m claves cargar organizador.clave --destino <c> --monto 1000 --url … --enviar
    python -m claves permiso organizador.clave --clave <escalador> --prefijo minero-cpu- --url …
    python -m claves rechazar organizador.clave --cuenta <c> --url …    # borra un pedido (fuera de la cadena)
    python -m claves reactivar organizador.clave --cuenta <c> --url …   # un minero suspendido (fuera de la cadena)

La privada no sale nunca del archivo: lo que se manda es la transacción firmada.
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

from comun import contrato

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
    if privada.startswith("{"):  # un .jwk bajado de la billetera del navegador
        privada = _de_b64url(json.loads(privada)["d"]).hex()
    contrato.clave_publica(privada)  # que sea una clave Ed25519
    return privada


def _b64url(datos):
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def _de_b64url(texto):
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def jwk(privada):
    """La privada en JWK (RFC 8037), como la importa WebCrypto: {kty: OKP, crv: Ed25519, d, x}."""
    return {"kty": "OKP", "crv": "Ed25519", "d": _b64url(bytes.fromhex(privada)),
            "x": _b64url(bytes.fromhex(contrato.clave_publica(privada)))}


# Cloudflare (workers.dev) rechaza con 403 (error 1010) el User-Agent por defecto de urllib.
AGENTE = "chimera-claves"


class Backend:
    """Lo que usa la herramienta del backend: cuentas, nonces, transacciones, pedidos y el panel firmado."""

    def __init__(self, url, timeout=10):
        self.url, self.timeout = url.rstrip("/"), timeout

    def pedir(self, metodo, ruta, cuerpo=None, encabezados=None):
        datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
        pedido = urllib.request.Request(self.url + ruta, data=datos, method=metodo,
                                        headers={"Content-Type": "application/json", "User-Agent": AGENTE, **(encabezados or {})})
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as respuesta:
                return respuesta.status, json.loads(respuesta.read() or b"null")
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read() or b"null")
            except ValueError:
                return error.code, None

    def cuenta(self, cuenta):
        codigo, datos = self.pedir("GET", f"/api/cuentas/{cuenta}")
        if codigo != 200:
            raise SystemExit(f"No se pudo leer la cuenta {cuenta}: {codigo} {datos}")
        return datos

    def nonce(self, cuenta):
        """El último nonce aceptado de la cuenta (pendiente o confirmado): el próximo es +1."""
        return self.cuenta(cuenta)["nonce"]

    def mandar(self, tx):
        return self.pedir("POST", "/api/transacciones", tx)

    def estado(self, id_):
        codigo, tx = self.pedir("GET", f"/api/transacciones/{id_}")
        return tx if codigo == 200 else None

    def firmado(self, privada, metodo, ruta, cuerpo=None):
        """Un pedido al panel (`/api/organizador/*`), firmado como el organizador (X-Organizador). Sin cuerpo, una
        lectura (los pendientes o los suspendidos, BC-88)."""
        crudo = json.dumps(cuerpo).encode() if cuerpo is not None else b""
        encabezados = contrato.firmar_pedido(privada, metodo, ruta, crudo, quien=contrato.ORGANIZADOR)
        pedido = urllib.request.Request(self.url + ruta, data=crudo or None, method=metodo,
                                        headers={"Content-Type": "application/json", "User-Agent": AGENTE, **encabezados})
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as respuesta:
                return respuesta.status, json.loads(respuesta.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")


def esperar_validada(backend, id_, segundos=20, pausa=0.5):
    """Espera a que el NCT la valide (deja de estar «recibida»). Devuelve la transacción como la ve el backend."""
    limite, tx = time.monotonic() + segundos, None
    while time.monotonic() < limite:
        tx = backend.estado(id_)
        if tx and tx.get("estado") != "recibida":
            return tx
        time.sleep(pausa)
    return tx


def mandar_y_seguir(backend, txs, salida=print, esperar=True):
    """Manda las transacciones en orden y espera a que el NCT valide cada una. Devuelve 0 si todas quedaron
    pendientes o confirmadas."""
    resultado = 0
    for tx in txs:
        codigo, respuesta = backend.mandar(tx)
        if codigo != 202:
            salida(f"✗ {tx['id']}: {codigo} {json.dumps(respuesta)}")
            return 1
        if not esperar:
            salida(f"→ {tx['id']}: enviada")
            continue
        validada = esperar_validada(backend, respuesta["id"]) or {}
        estado = validada.get("estado", "?")
        salida(f"{'✓' if estado in ('pendiente', 'confirmada') else '✗'} {respuesta['id']}: {estado}"
               + (f" ({validada.get('motivo')})" if validada.get("motivo") else ""))
        if estado not in ("pendiente", "confirmada"):
            resultado = 1
    return resultado


def actas_de_aprobacion(privada_organizador, nonce, cuenta, tipo, nombre, carga=None, ahora=None):
    """El alta (y, si viene, la carga para esa cuenta) con los nonces de «emision» que siguen a `nonce`."""
    timestamp = int(time.time() if ahora is None else ahora)
    txs = [contrato.alta(privada_organizador, nonce + 1, cuenta, tipo, nombre, timestamp)]
    if carga:
        txs.append(contrato.carga(privada_organizador, nonce + 2, cuenta, carga, timestamp))
    return txs


def aprobar(backend, privada_organizador, cuenta, tipo, nombre, carga=None, salida=print, esperar=True):
    """Lo que hace el organizador al aprobar una cuenta (v3): firma `alta-<n>` y la manda. Queda aprobada cuando
    entra en un bloque (un minero ya puede entrar al gateway apenas el NCT la valida)."""
    if not contrato.cuenta_valida(cuenta):
        raise ValueError("la cuenta tiene que ser una clave pública Ed25519 en hex (64 caracteres)")
    if tipo not in contrato.TIPOS_ALTA:
        raise ValueError(f"tipo: {' | '.join(contrato.TIPOS_ALTA)}")
    if not contrato.nombre_valido(nombre):
        raise ValueError("nombre: de 1 a 32 letras, números, «_», «.» o «-»")
    if carga and tipo != "usuario":
        raise ValueError("la carga es sólo para usuarios (los mineros cobran minando)")
    txs = actas_de_aprobacion(privada_organizador, backend.nonce(contrato.EMISION), cuenta, tipo, nombre, carga)
    return mandar_y_seguir(backend, txs, salida, esperar)


def bajar_cadena(backend, lote=100):
    """Todos los bloques, del génesis a la punta (GET /api/bloques?orden=asc)."""
    bloques = []
    while True:
        codigo, nuevos = backend.pedir("GET", f"/api/bloques?orden=asc&desde={len(bloques)}&limite={lote}")
        if codigo != 200 or not isinstance(nuevos, list):
            raise SystemExit(f"El backend contestó {codigo} a /api/bloques")
        bloques += nuevos
        if len(nuevos) < lote:
            return bloques


def verificar_saldo(backend, cuenta, nodos=(), organizador="", salida=print):
    """BC-84: el saldo que calcula la herramienta verificando la cadena entera con comun/estado.py, comparado con el
    backend (su número y su prueba de Merkle) y con las atestaciones firmadas de los nodos. 0 si todo coincide."""
    from comun import estado as estado_cadena

    def shares(epoca):
        codigo, datos = backend.pedir("GET", f"/api/epocas/{epoca}")
        return datos.get("detalle") if codigo == 200 and isinstance(datos, dict) else None

    bloques = bajar_cadena(backend)
    reglas = estado_cadena.Reglas(organizador=organizador, shares=shares)
    estado, malos = estado_cadena.reconstruir(bloques, reglas)
    if malos:
        altura, problemas = malos[0]
        salida(f"✗ el bloque {altura} NO verifica: {'; '.join(problemas)}")
        return 1
    hashes = {b["altura"]: b["block_hash"] for b in bloques}
    raiz, saldo = estado.raiz(), estado.saldos.get(cuenta, 0)
    hoja = dict(estado.hojas()).get(cuenta)
    salida(f"Cadena verificada: altura {estado.altura}, raíz de estado {raiz}")
    salida(f"Saldo de {cuenta}: {saldo} (calculado acá)")
    bien = True
    codigo, prueba = backend.pedir("GET", f"/api/prueba/{cuenta}")
    if codigo == 200 and prueba.get("altura") == estado.altura:
        rama_ok = prueba.get("hoja") is None or estado_cadena.raiz_de_rama(prueba["hoja"], prueba["rama"]) == prueba["raiz"]
        coincide = rama_ok and prueba["raiz"] == raiz and prueba.get("hoja") == hoja
        bien &= coincide
        salida(f"{'✓' if coincide else '✗'} backend /api/prueba: {'coincide' if coincide else 'dice ' + str(prueba.get('hoja'))}")
        codigo, datos = backend.pedir("GET", f"/api/cuentas/{cuenta}")
        if codigo == 200:
            bien &= datos["saldo"] == saldo
            salida(f"{'✓' if datos['saldo'] == saldo else '✗'} backend /api/cuentas: saldo {datos['saldo']}")
    else:
        salida("· el backend va por otra altura (entró un bloque): volvé a verificar para compararlo")
    for url in nodos:
        try:
            codigo, a = Backend(url).pedir("GET", "/api/atestacion")
        except OSError as error:
            salida(f"· {url}: no respondió ({error})")
            continue
        firma_ok = codigo == 200 and contrato.verificar_firma(
            a.get("nodo"), contrato.mensaje_atestacion(a["nodo"], a["altura"], a["punta"], a["raiz"], a["tiempo"]),
            a.get("firma"))
        if not firma_ok:
            bien = False
            salida(f"✗ {url}: la atestación no está bien firmada")
        elif a["altura"] == estado.altura:
            coincide = a["punta"] == estado.punta and a["raiz"] == raiz
            bien &= coincide
            salida(f"{'✓' if coincide else '✗'} nodo {url}: {'coincide' if coincide else 'otra punta: ' + a['punta']}")
        elif a["altura"] < estado.altura:
            coincide = hashes.get(a["altura"]) == a["punta"]
            bien &= coincide
            salida(f"{'✓' if coincide else '✗'} nodo {url}: va atrás (altura {a['altura']})"
                   f"{'' if coincide else ', con otro bloque a esa altura'}")
        else:
            salida(f"· nodo {url}: va adelante (altura {a['altura']}): volvé a verificar para compararlo")
    return 0 if bien else 1


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


def _url(parser, a):
    if not a.url:
        parser.error("hace falta --url (el backend: https://<dominio> o http://localhost:8080)")
    return Backend(a.url)


def _cuenta(parser, valor):
    if not contrato.cuenta_valida(valor):
        parser.error("la cuenta es una clave pública Ed25519 en hex (64 caracteres en minúscula)")
    return valor


def principal(argumentos=None, salida=print):
    parser = argparse.ArgumentParser(prog="python -m claves", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("generar", help="genera un par y guarda la privada en ARCHIVO (permisos 600)")
    p.add_argument("archivo")
    p = sub.add_parser("publica", help="la cuenta (clave pública) de ARCHIVO")
    p.add_argument("archivo")
    p = sub.add_parser("exportar", help="la privada de ARCHIVO en JWK, para importarla en el navegador")
    p.add_argument("archivo")
    p = sub.add_parser("alta", help="pide el alta de la cuenta de ARCHIVO (la aprueba el organizador)")
    p.add_argument("archivo")
    p.add_argument("--tipo", choices=contrato.TIPOS_ALTA, default="usuario")
    p.add_argument("--nombre", required=True)
    p.add_argument("--url")
    p = sub.add_parser("saldo", help="el saldo, el nonce y si está aprobada la cuenta de ARCHIVO (o --cuenta)")
    p.add_argument("archivo", nargs="?")
    p.add_argument("--cuenta")
    p.add_argument("--url")
    p.add_argument("--verificar", action="store_true",
                   help="baja la cadena, la verifica acá y compara el saldo con el backend y los nodos (BC-84)")
    p.add_argument("--nodo", action="append", default=[], help="un nodo verificador para comparar (se repite)")
    p.add_argument("--organizador", default="", help="la cuenta del organizador que se espera en el génesis")
    for nombre, ayuda in (("firmar", "arma y firma una transferencia"), ("cargar", "una carga de saldo del organizador"),
                          ("transferir", "firma una transferencia con el nonce siguiente y la manda")):
        p = sub.add_parser(nombre, help=ayuda)
        p.add_argument("archivo", help="la clave del origen (en «cargar», la del organizador)")
        p.add_argument("--destino", required=True, help="la cuenta destino (64 hex)")
        p.add_argument("--monto", type=int, required=True)
        p.add_argument("--nonce", type=int, help="si no está, lo pide al backend (--url): el último + 1")
        p.add_argument("--url", help="el backend (https://<dominio>)")
        p.add_argument("--enviar", action="store_true", help="la manda al backend (si no, sólo la muestra)")
    p = sub.add_parser("pendientes", help="organizador: los pedidos de alta que esperan (firmado: no son públicos)")
    p.add_argument("archivo", help="la clave del organizador")
    p.add_argument("--url")
    p = sub.add_parser("aprobar", help="organizador: firma el alta de una cuenta (y opcionalmente una carga)")
    p.add_argument("archivo", help="la clave del organizador")
    p.add_argument("--cuenta", required=True)
    p.add_argument("--tipo", choices=contrato.TIPOS_ALTA, default="usuario")
    p.add_argument("--nombre", required=True)
    p.add_argument("--carga", type=int, help="saldo inicial para un usuario (carga-<n> en el mismo paso)")
    p.add_argument("--url")
    p = sub.add_parser("baja", help="organizador: firma la baja de una cuenta")
    p.add_argument("archivo", help="la clave del organizador")
    p.add_argument("--cuenta", required=True)
    p.add_argument("--motivo", default="")
    p.add_argument("--url")
    p = sub.add_parser("permiso", help="organizador: deja a otra clave dar de alta y de baja mineros con un prefijo")
    p.add_argument("archivo", help="la clave del organizador")
    p.add_argument("--clave", required=True, help="la clave que recibe el permiso (la del escalador)")
    p.add_argument("--prefijo", required=True, help="el prefijo de nombre de sus mineros; vacío («») lo revoca")
    p.add_argument("--url")
    for nombre, ayuda in (("rechazar", "organizador: borra un pedido de alta (no va a la cadena)"),
                          ("reactivar", "organizador: un minero suspendido vuelve a recibir rangos")):
        p = sub.add_parser(nombre, help=ayuda)
        p.add_argument("archivo", help="la clave del organizador")
        p.add_argument("--cuenta", required=True)
        p.add_argument("--url")
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
    elif a.comando == "alta":
        if not contrato.nombre_valido(a.nombre):
            parser.error("nombre: de 1 a 32 letras, números, «_», «.» o «-»")
        codigo, respuesta = _url(parser, a).pedir("POST", "/api/altas",
                                                  contrato.pedido_alta(leer_privada(a.archivo), a.tipo, a.nombre))
        salida(f"{codigo} {json.dumps(respuesta)}")
        return 0 if codigo in (202, 409) else 1
    elif a.comando == "saldo":
        cuenta = a.cuenta or (contrato.clave_publica(leer_privada(a.archivo)) if a.archivo else None)
        if not cuenta:
            parser.error("hace falta ARCHIVO o --cuenta")
        if a.verificar:
            return verificar_saldo(_url(parser, a), cuenta, a.nodo, organizador=a.organizador, salida=salida)
        datos = _url(parser, a).cuenta(cuenta)
        salida(json.dumps(datos, ensure_ascii=False))
    elif a.comando in ("firmar", "cargar", "transferir"):
        privada = leer_privada(a.archivo)
        backend = Backend(a.url) if a.url else None
        if a.nonce is None and backend is None:
            parser.error("hace falta --nonce o --url (para pedirle el último al backend)")
        origen = contrato.EMISION if a.comando == "cargar" else contrato.clave_publica(privada)
        nonce = a.nonce if a.nonce is not None else backend.nonce(origen) + 1
        if a.comando == "cargar":
            tx = contrato.carga(privada, nonce, _cuenta(parser, a.destino), a.monto, int(time.time()))
        else:
            tx = contrato.transaccion(privada, _cuenta(parser, a.destino), a.monto, nonce)
        if a.comando == "transferir":
            if backend is None:
                parser.error("transferir necesita --url")
            return mandar_y_seguir(backend, [tx], salida)
        salida(json.dumps(tx))
        if a.enviar:
            if backend is None:
                parser.error("--enviar necesita --url")
            codigo, respuesta = backend.mandar(tx)
            salida(f"{codigo} {json.dumps(respuesta)}")
            return 0 if codigo == 202 else 1
    elif a.comando == "pendientes":
        codigo, datos = _url(parser, a).firmado(leer_privada(a.archivo), "GET", "/api/organizador/altas")
        if codigo != 200:
            salida(f"{codigo} {json.dumps(datos)}")
            return 1
        for pedido in (datos or {}).get("pendientes", []):
            salida(f"{pedido['cuenta']}  {pedido.get('tipo'):<9} {pedido.get('nombre')}")
        return 0 if codigo == 200 else 1
    elif a.comando == "aprobar":
        return aprobar(_url(parser, a), leer_privada(a.archivo), _cuenta(parser, a.cuenta), a.tipo, a.nombre,
                       a.carga, salida)
    elif a.comando in ("baja", "permiso"):
        backend, privada = _url(parser, a), leer_privada(a.archivo)
        nonce, ahora = backend.nonce(contrato.EMISION) + 1, int(time.time())
        if a.comando == "baja":
            tx = contrato.baja(privada, nonce, _cuenta(parser, a.cuenta), a.motivo, ahora)
        else:
            if a.prefijo and not contrato.nombre_valido(a.prefijo):
                parser.error("el prefijo: de 1 a 32 letras, números, «_», «.» o «-»")
            tx = contrato.permiso(privada, nonce, _cuenta(parser, a.clave), a.prefijo, ahora)
        return mandar_y_seguir(backend, [tx], salida)
    elif a.comando in ("rechazar", "reactivar"):
        codigo, respuesta = _url(parser, a).firmado(leer_privada(a.archivo), "POST", f"/api/organizador/{a.comando}",
                                                    {"cuenta": _cuenta(parser, a.cuenta)})
        salida(f"{codigo} {json.dumps(respuesta)}")
        return 0 if codigo == 200 else 1
    elif a.comando == "rafaga":
        estados = rafaga(Backend(a.url), [leer_privada(r) for r in a.archivos], a.cantidad, esperar=a.esperar,
                         salida=salida)
        return 0 if estados and all(e in CERRADOS for e in estados.values()) else 1
    return 0


def main():
    try:
        sys.exit(principal())
    except (FileExistsError, FileNotFoundError, ValueError, KeyError) as error:
        sys.exit(f"Error: {error}")
