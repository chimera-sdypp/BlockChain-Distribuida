"""Nodo verificador (BC-83) y validador (BC-86): una copia propia de la cadena que verifica cada bloque entero.

    python -m nodo --fuente https://blockchain-chimera.duckdns.org --datos ~/nodo-chimera            # verificador
    python -m nodo --fuente https://… --datos ~/validador --validador                                  # validador

Lo corre cualquiera, sin permiso ni túnel: sólo lee la API pública (`/api/bloques?orden=asc`, `/api/epocas/<n>`).
Guarda su propia copia en un SQLite (no usa el Redis compartido) y, con `comun/estado.py` en modo estricto, verifica
cada bloque contra **su** copia: hash y prefijo, que encadene con **su** punta, la raíz de transacciones y la de
estado, cada firma, nonce y saldo, las actas, la coinbase y la participación contra la lista de shares publicada.
Si cierra, lo aplica; si no, lo rechaza, lo anota con el motivo, alerta (`blockchain_nodo_rechazos_total`) y no
sigue. Si la fuente le muestra otro bloque a una altura que ya verificó, es una historia reescrita
(`blockchain_nodo_conflictos_total`): no la sigue.

No le cree a nadie: al arrancar recorre su copia (o arranca de cero) y baja el resto desde el génesis. Publica lo que
verificó firmado con su clave (`/api/atestacion`), así una billetera puede citar lo que dijo cada nodo.

**Validador** (BC-86, opcional): con `--validador`, si su clave tiene un alta de tipo `validador` en la cadena,
firma `firma:<altura>:<block_hash>` del último bloque que verificó y lo manda a `POST /api/firmas`. Firma siempre
hacia adelante y una sola vez por altura (lo anota en su disco antes de mandarla). Con 2 de 3 validadores, el bloque
queda finalizado. Sin validadores, el sistema anda igual: sólo no se finaliza nada.
"""

import argparse
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge

from comun import contrato, estado as estado_cadena, registro, web

from .almacen import Almacen

log = logging.getLogger("nodo")

ALTURA = Gauge("blockchain_nodo_altura", "Altura de la copia verificada de este nodo")
ATRASO = Gauge("blockchain_nodo_atraso", "Bloques que la fuente tiene y este nodo todavía no verificó")
PUNTA = Gauge("blockchain_nodo_punta", "Los primeros 8 hex de la punta de este nodo, como número (para comparar nodos)")
RECHAZOS = Counter("blockchain_nodo_rechazos", "Bloques que este nodo rechazó porque no cumplen el contrato")
CONFLICTOS = Counter("blockchain_nodo_conflictos", "Veces que la fuente mostró otro bloque a una altura ya verificada")
FIRMADOS = Counter("blockchain_nodo_firmas", "Bloques que firmó este nodo como validador")
FINALIZADA_NODO = Gauge("blockchain_nodo_finalizada", "La altura finalizada según las firmas que verificó este nodo")

LOTE = 100


class Fuente:
    """La API pública del sistema (la del operador: el nodo no le cree, verifica todo lo que baja)."""

    def __init__(self, url, timeout=20):
        self.url, self.timeout = url.rstrip("/"), timeout
        self.viva = False

    def pedir(self, metodo, ruta, cuerpo=None):
        datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
        pedido = urllib.request.Request(self.url + ruta, data=datos, method=metodo,
                                        headers={"Content-Type": "application/json", "User-Agent": "nodo-verificador"})
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as respuesta:
                self.viva = True
                return respuesta.status, json.loads(respuesta.read() or b"null")
        except urllib.error.HTTPError as error:
            self.viva = True
            try:
                return error.code, json.loads(error.read() or b"null")
            except ValueError:
                return error.code, None
        except (urllib.error.URLError, OSError, ValueError):
            self.viva = False
            raise

    def bloques(self, desde, limite=LOTE):
        codigo, datos = self.pedir("GET", f"/api/bloques?orden=asc&desde={desde}&limite={limite}")
        if codigo != 200 or not isinstance(datos, list):
            raise OSError(f"la fuente contestó {codigo} a /api/bloques")
        return datos

    def epoca(self, numero):
        codigo, datos = self.pedir("GET", f"/api/epocas/{numero}")
        return datos.get("detalle") if codigo == 200 and isinstance(datos, dict) else None

    def punta(self):
        codigo, datos = self.pedir("GET", "/api/bloques?limite=1")
        return datos[0] if codigo == 200 and datos else None


def cargar_clave(archivo, privada=None):
    """La clave del nodo: NODO_CLAVE, o el archivo (si no existe, se genera con permisos 600)."""
    if privada:
        contrato.clave_publica(privada)
        return privada
    ruta = Path(archivo)
    if ruta.exists():
        return ruta.read_text().strip()
    privada, publica = contrato.nueva_clave()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as salida:
        salida.write(privada + "\n")
    log.info("Clave nueva del nodo en %s: %s", ruta, publica)
    return privada


class Nodo:
    def __init__(self, almacen, fuente, privada, validador=False, organizador="", fondo_epoca=contrato.FONDO_EPOCA,
                 snapshot_cada=100):
        self.almacen, self.fuente, self.privada = almacen, fuente, privada
        self.cuenta = contrato.clave_publica(privada)
        self.validador, self.snapshot_cada = validador, snapshot_cada
        self.reglas = estado_cadena.Reglas(organizador=organizador, fondo_epoca=fondo_epoca, shares=self._shares)
        self.estado = estado_cadena.Estado()
        self.lock = threading.RLock()
        self.conflicto = None      # {altura, mia, fuente}: la fuente reescribió algo que ya estaba verificado
        self.rechazado = None      # {altura, block_hash, motivos}: el último bloque que no se aceptó
        self.atraso = 0
        self._enviada = None       # la última firma que la fuente recibió

    # ── Arranque: la copia propia ──

    def arrancar(self):
        """Del snapshot propio (si da la raíz de su contenido) y los bloques guardados después; si no, de cero."""
        with self.lock:
            guardado = self.almacen.meta("snapshot")
            try:
                self.estado = estado_cadena.Estado.desde_dict(guardado) if guardado else estado_cadena.Estado()
            except (ValueError, KeyError):
                log.warning("El snapshot propio no sirve: se recorre la copia desde el génesis")
                self.estado = estado_cadena.Estado()
            reglas = estado_cadena.Reglas(organizador=self.reglas.organizador, fondo_epoca=self.reglas.fondo_epoca,
                                          shares=self._shares, verificar_firmas=False)  # ya los verificó al guardarlos
            for bloque in self.almacen.bloques(desde=self.estado.altura + 1):
                problemas, _ = estado_cadena.aplicar_bloque(self.estado, bloque, reglas)
                if problemas:
                    raise RuntimeError(f"la copia propia no cierra en el bloque {bloque['altura']}: {problemas}")
            self._metricas()
            log.info("Copia propia: altura %d, raíz de estado %s", self.estado.altura, self.estado.raiz())

    def _shares(self, epoca):
        """La lista de shares de la época: la propia si ya se comprobó, o la que publica la fuente (Reglas la compara
        con la raíz de la cadena antes de usarla)."""
        propia = self.almacen.epoca(epoca)
        if propia is not None:
            return propia
        try:
            return self.fuente.epoca(epoca)
        except OSError:
            return None

    # ── Seguir la cadena ──

    def sincronizar(self):
        """Baja y verifica lo nuevo. Devuelve cuántos bloques aplicó."""
        with self.lock:
            if self.conflicto:
                return 0
            aplicados = 0
            while True:
                desde = max(self.estado.altura, 0)
                lote = self.fuente.bloques(desde)
                if self.estado.altura >= 0:
                    if not lote:
                        self._revisar_si_retrocedio()
                        break
                    if lote[0].get("altura") != self.estado.altura:
                        break
                    if lote[0].get("block_hash") != self.estado.punta:
                        self._conflicto(self.estado.altura, lote[0].get("block_hash"))
                        break
                    lote = lote[1:]
                if not lote:
                    break
                for bloque in lote:
                    if not self._aplicar(bloque):
                        return aplicados
                    aplicados += 1
                if len(lote) < LOTE - 1:
                    break
            self._medir_atraso()
            return aplicados

    def _aplicar(self, bloque):
        problemas, avisos = estado_cadena.aplicar_bloque(self.estado, bloque, self.reglas)
        if problemas:
            self.rechazado = {"altura": self.estado.altura + 1, "block_hash": bloque.get("block_hash"),
                              "motivos": problemas}
            if self.almacen.rechazar(self.estado.altura + 1, bloque.get("block_hash"), problemas, int(time.time())):
                RECHAZOS.inc()
                log.error("Bloque %d (%s) RECHAZADO: %s", self.estado.altura + 1, bloque.get("block_hash"),
                          "; ".join(problemas[:5]))
            return False
        for aviso in avisos:
            log.warning("Bloque %d: %s", self.estado.altura, aviso)
        self.rechazado = None
        guardar = {k: v for k, v in bloque.items() if k not in ("firmas", "finalizado", "minero", "recompensa")}
        self.almacen.guardar_bloque(guardar)
        for epoca, detalle in list(self.reglas._detalles.items()):
            if detalle and self.estado.shares.get(epoca) == contrato.raiz_shares(detalle) \
                    and self.almacen.epoca(epoca) is None:
                self.almacen.guardar_epoca(epoca, detalle)
        if self.snapshot_cada and self.estado.altura % self.snapshot_cada == 0:
            self.almacen.meta("snapshot", self.estado.a_dict())
        self._metricas()
        return True

    def _revisar_si_retrocedio(self):
        """La fuente tiene menos bloques que los que ya verificó este nodo: si a su altura muestra otro bloque, o si
        perdió los que siguen, la historia cambió."""
        punta = self.fuente.punta()
        if punta and punta.get("altura", -1) < self.estado.altura:
            self._conflicto(punta["altura"], punta.get("block_hash"))

    def _conflicto(self, altura, de_la_fuente):
        if not self.conflicto:
            CONFLICTOS.inc()
            log.error("CONFLICTO: en la altura %d la fuente muestra %s y este nodo verificó %s (punta %d): la historia "
                      "se reescribió. No se sigue esa cadena.", altura, de_la_fuente, self._hash_en(altura),
                      self.estado.altura)
        self.conflicto = {"altura": altura, "mia": self._hash_en(altura), "fuente": de_la_fuente}

    def _hash_en(self, altura):
        if altura == self.estado.altura:
            return self.estado.punta
        propio = self.almacen.bloques(desde=altura, hasta=altura)
        return propio[0]["block_hash"] if propio else None

    def _medir_atraso(self):
        try:
            punta = self.fuente.punta()
        except OSError:
            return
        self.atraso = max(0, punta["altura"] - self.estado.altura) if punta else 0
        ATRASO.set(self.atraso)

    def _metricas(self):
        ALTURA.set(self.estado.altura)
        PUNTA.set(int(self.estado.punta[:8], 16) if self.estado.punta else 0)

    # ── Validador (BC-86) ──

    def es_validador(self):
        return self.estado.cuentas.get(self.cuenta, {}).get("tipo") == "validador"

    def firmar_si_toca(self):
        """Firma la punta verificada si es validador en la cadena, no hay conflicto y es más alta que la última que
        firmó. La anota en su disco antes de mandarla: nunca firma dos bloques distintos a la misma altura."""
        with self.lock:
            if not self.validador or self.conflicto or self.estado.altura < 1:
                return None
            if not self.es_validador():
                return None
            ultima = self.almacen.meta("ultima_firma") or {"altura": -1}
            if self.estado.altura < ultima["altura"]:
                return None
            if self.estado.altura == ultima["altura"] and ultima.get("block_hash") != self.estado.punta:
                return None  # ya firmó otro bloque a esta altura: nunca la segunda
            if self._enviada == (self.estado.altura, self.estado.punta):
                return None
            firma = contrato.firma_bloque(self.privada, self.estado.altura, self.estado.punta)
            if self.estado.altura > ultima["altura"]:
                self.almacen.meta("ultima_firma", {"altura": firma["altura"], "block_hash": firma["block_hash"]})
                FIRMADOS.inc()
                log.info("Firmo el bloque %d (%s)", firma["altura"], firma["block_hash"])
        try:
            codigo, respuesta = self.fuente.pedir("POST", "/api/firmas", firma)
        except OSError as error:
            log.warning("No se pudo mandar la firma (se reintenta): %r", error)
            return None
        if codigo in (200, 201):
            self._enviada = (firma["altura"], firma["block_hash"])
        else:
            log.warning("La fuente contestó %s a la firma: %s", codigo, respuesta)
        return firma

    def finalidad(self):
        """La altura finalizada según las firmas que publica la fuente, verificadas acá: cada una del validador que
        dice, de un validador de la cadena y sobre el bloque que este nodo tiene a esa altura."""
        try:
            codigo, datos = self.fuente.pedir("GET", "/api/finalidad")
        except OSError:
            return None
        if codigo != 200 or not isinstance(datos, dict):
            return None
        with self.lock:
            validadores = {c for c, d in self.estado.cuentas.items() if d.get("tipo") == "validador"}
            ultimas = {}
            for firma in (datos.get("firmas") or {}).values():
                if firma.get("validador") not in validadores or contrato.validar_firma_bloque(firma):
                    continue
                propio = self.almacen.bloques(desde=firma["altura"], hasta=firma["altura"])
                if propio and propio[0]["block_hash"] == firma["block_hash"]:
                    ultimas[firma["validador"]] = firma["altura"]
            altura = contrato.altura_finalizada(validadores, ultimas)
        FINALIZADA_NODO.set(altura)
        return {"altura": altura, "validadores": len(validadores), "umbral": contrato.umbral_finalidad(len(validadores)),
                "firmas_validas": len(ultimas)}

    # ── Lo que publica ──

    def atestacion(self):
        with self.lock:
            tiempo = int(time.time())
            datos = {"nodo": self.cuenta, "altura": self.estado.altura, "punta": self.estado.punta,
                     "raiz": self.estado.raiz(), "tiempo": tiempo}
        datos["firma"] = contrato.firmar(self.privada, contrato.mensaje_atestacion(**datos))
        return datos

    def vuelta(self):
        """Lo que hace el hilo cada pocos segundos."""
        try:
            self.sincronizar()
            self.firmar_si_toca()
        except OSError as error:
            log.warning("La fuente no respondió: %r", error)


def agregar_rutas(app, nodo):
    @app.get("/api/atestacion")
    def atestacion():
        return nodo.atestacion()

    @app.get("/api/estado")
    def estado():
        with nodo.lock:
            return {"nodo": nodo.cuenta, "altura": nodo.estado.altura, "punta": nodo.estado.punta,
                    "raiz": nodo.estado.raiz(), "atraso": nodo.atraso, "conflicto": nodo.conflicto,
                    "rechazado": nodo.rechazado, "validador": nodo.validador and nodo.es_validador()}

    @app.get("/api/saldos")
    def saldos():
        with nodo.lock:
            return {c: s for c, s in nodo.estado.saldos.items() if s}

    @app.get("/api/cuentas/{cuenta}")
    def cuenta(cuenta: str):
        with nodo.lock:
            datos = nodo.estado.cuentas.get(cuenta) or {}
            return {"cuenta": cuenta, "saldo": nodo.estado.saldos.get(cuenta, 0), "nonce": nodo.estado.nonces.get(cuenta, 0),
                    "aprobada": bool(datos), "tipo": datos.get("tipo"), "nombre": datos.get("nombre"),
                    "altura": nodo.estado.altura}

    @app.get("/api/prueba/{cuenta}")
    def prueba(cuenta: str):
        with nodo.lock:
            return {"cuenta": cuenta, "altura": nodo.estado.altura, "block_hash": nodo.estado.punta,
                    **nodo.estado.prueba(cuenta)}

    @app.get("/api/bloques")
    def bloques(desde: int = 0, limite: int = 20):
        return nodo.almacen.bloques(desde=max(desde, 0), limite=min(max(limite, 1), LOTE))

    @app.get("/api/bloques/{hash_}")
    def bloque(hash_: str):
        encontrado = nodo.almacen.bloque(hash_)
        return encontrado if encontrado else JSONResponse({"error": "no está en la copia de este nodo"}, status_code=404)

    @app.get("/api/rechazos")
    def rechazos():
        return {"rechazos": nodo.almacen.rechazos(), "conflicto": nodo.conflicto}

    @app.get("/api/finalidad")
    def finalidad():
        return nodo.finalidad() or JSONResponse({"error": "la fuente no respondió"}, status_code=503)


def seguir(nodo, intervalo, detenido):
    while not detenido.is_set():
        nodo.vuelta()
        detenido.wait(intervalo)


def principal(argumentos=None):
    parser = argparse.ArgumentParser(prog="python -m nodo", description=__doc__.split("\n\n")[0])
    parser.add_argument("--fuente", default=os.environ.get("FUENTE_URL"), help="la API pública (FUENTE_URL)")
    parser.add_argument("--datos", default=os.environ.get("NODO_DATOS", "datos-nodo"),
                        help="dónde guarda su copia (NODO_DATOS)")
    parser.add_argument("--clave", default=os.environ.get("NODO_CLAVE_ARCHIVO"),
                        help="la clave del nodo (si no existe, la genera); por defecto <datos>/nodo.clave")
    parser.add_argument("--validador", action="store_true", default=os.environ.get("VALIDADOR") == "1",
                        help="firma los bloques que verifica, si su clave es un validador en la cadena (VALIDADOR=1)")
    parser.add_argument("--organizador", default=os.environ.get("ORGANIZADOR_CLAVE", ""),
                        help="la cuenta del organizador que se espera en el génesis (si no, se toma la del génesis)")
    parser.add_argument("--puerto", type=int, default=int(os.environ.get("PUERTO", "8090")))
    parser.add_argument("--intervalo", type=float, default=float(os.environ.get("INTERVALO_SEGUNDOS", "3")))
    a = parser.parse_args(argumentos)
    if not a.fuente:
        parser.error("hace falta --fuente (o FUENTE_URL): la API pública del sistema")
    registro.configurar("nodo", os.environ.get("LOG_NIVEL", "INFO"), os.environ.get("LOGS_DIR", str(Path(a.datos) / "logs")))
    privada = cargar_clave(a.clave or Path(a.datos) / "nodo.clave", os.environ.get("NODO_CLAVE"))
    nodo = Nodo(Almacen(Path(a.datos) / "cadena.sqlite"), Fuente(a.fuente), privada, validador=a.validador,
                organizador=a.organizador, fondo_epoca=int(os.environ.get("FONDO_EPOCA", contrato.FONDO_EPOCA)))
    nodo.arrancar()
    log.info("Nodo %s siguiendo %s%s", nodo.cuenta, a.fuente, " (validador)" if a.validador else "")
    app = web.crear_app("nodo", {"fuente": lambda: nodo.fuente.viva, "cadena": lambda: not nodo.conflicto})
    agregar_rutas(app, nodo)
    detenido = threading.Event()
    threading.Thread(target=seguir, args=(nodo, a.intervalo, detenido), name="seguir", daemon=True).start()
    web.servir(app, a.puerto, en_hilo=False)


def main():
    principal()
