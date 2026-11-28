"""Minero CPU: pide rangos al gateway por HTTP, los recorre y devuelve el resultado.

Corre fuera del cluster (VMs o cualquier máquina): sólo necesita la URL del gateway y su clave.
Cada pedido va firmado con la clave Ed25519 del minero (contrato v2, BC-57): su cuenta es la clave
pública, y el organizador la tiene que aprobar antes de que el gateway lo atienda. Desde el contrato v4 (BC-99) la
firma lleva el hash del génesis, que el minero lee de `GET /cadena` (y vuelve a leer si la cadena se reinicia).
"""

import hashlib
import json
import logging
import os
import platform
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from comun import config, contrato, registro

from .busqueda import Buscador

log = logging.getLogger("minero_cpu")

ESPERA_APROBACION = 30  # segundos entre intentos mientras el organizador no aprueba la cuenta
# Una GPU hace la tarea modelo en ~10 ms, y la primera vuelta la encuentra con el reloj bajo (salía 3 veces menos).
# Si una vuelta dura menos que esto, se calienta la placa este tiempo y se mide una tanda de al menos este tiempo.
MEDIR_AL_MENOS = 0.5


class ClaveRechazada(Exception):
    """El gateway no acepta la firma (401): la clave no es la del pedido o el reloj está corrido."""


def modelo_cpu():
    try:
        with open("/proc/cpuinfo") as cpuinfo:
            for linea in cpuinfo:
                if linea.startswith("model name"):
                    return linea.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "CPU"


def cargar_clave(alias, privada="", semilla="", archivo="minero.clave"):
    """La clave privada del minero, en este orden:

    - `privada`: la clave en hex (CLAVE_PRIVADA; así se la pasa el escalador a sus VMs);
    - `semilla`: se deriva con el alias, sha256("<semilla>:<alias>") (CLAVE_SEMILLA; la usan las
      réplicas del minero GPU en k8s, que tienen un nombre fijo);
    - `archivo`: la lee, o la genera la primera vez y la guarda con permisos 600 (CLAVE_ARCHIVO). Al lado deja la
      pública en `<archivo>.pub`: en una VM, la lee arrancar.sh para publicarla (BC-95: la privada no sale de la VM).
    """
    if privada:
        contrato.clave_publica(privada)  # que sea una clave: si no, que falle acá y no en cada pedido
        return privada
    if semilla:
        return hashlib.sha256(f"{semilla}:{alias}".encode()).hexdigest()
    ruta = Path(archivo)
    if ruta.exists():
        privada = ruta.read_text().strip()
    else:
        privada, publica = contrato.nueva_clave()
        ruta.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as salida:
            salida.write(privada + "\n")
        log.info("Clave nueva en %s: la cuenta del minero es %s", ruta, publica)
    publica = contrato.clave_publica(privada)
    Path(f"{ruta}.pub").write_text(publica + "\n")
    return privada


class Gateway:
    """Cliente HTTP del gateway (API en CONTRATO.md): cada pedido va firmado con la clave del minero."""

    def __init__(self, url, privada, timeout=10):
        self.url, self.privada, self.timeout = url.rstrip("/"), privada, timeout
        self.cuenta = contrato.clave_publica(privada)
        self.cadena = None  # v4: el hash del génesis, que va adelante de lo que se firma (se lee de GET /cadena)

    def leer_cadena(self):
        """El génesis de la cadena del gateway, sin firma. Los errores de red los levanta como URLError."""
        pedido = urllib.request.Request(self.url + "/cadena", method="GET")
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as respuesta:
                genesis = json.loads(respuesta.read()).get("genesis")
        except urllib.error.HTTPError as error:
            raise urllib.error.URLError(f"el gateway no dio el génesis ({error.code}): {_motivo(error)}") from None
        if not contrato.cadena_valida(genesis):
            raise urllib.error.URLError(f"el gateway dio un génesis que no es un hash: {genesis!r}")
        if genesis != self.cadena:
            log.info("La cadena del gateway es %s", genesis)
        self.cadena = genesis
        return genesis

    def pedir(self, metodo, ruta, cuerpo=None):
        """Devuelve (código, JSON o None). Los errores de red los levanta como URLError.

        Si el gateway rechaza la firma (401), relee el génesis: si cambió (la cadena se reinició), repite el pedido
        firmado para la nueva. Si era el mismo, el problema es la clave o el reloj: ClaveRechazada.
        """
        if self.cadena is None:
            self.leer_cadena()
        for intento in range(2):
            datos = json.dumps(cuerpo).encode() if cuerpo is not None else b""
            encabezados = {"Content-Type": "application/json",
                           **contrato.firmar_pedido(self.privada, metodo, ruta, datos, cadena=self.cadena)}
            pedido = urllib.request.Request(self.url + ruta, data=datos or None, method=metodo, headers=encabezados)
            try:
                with urllib.request.urlopen(pedido, timeout=self.timeout) as respuesta:
                    texto = respuesta.read()
                    return respuesta.status, json.loads(texto) if texto else None
            except urllib.error.HTTPError as error:
                if error.code != 401:
                    return error.code, None
                motivo, anterior = _motivo(error), self.cadena
                if intento == 0 and self.leer_cadena() != anterior:
                    continue
                raise ClaveRechazada(f"el gateway rechazó la firma: {motivo}. ¿El reloj está en hora?") from None


def _motivo(error):
    try:
        return json.loads(error.read()).get("error", "")
    except (ValueError, AttributeError, OSError):
        return ""


class Minero:
    """El cliente del gateway. Lo usa también el minero GPU, con su propio buscador y tipo «gpu»."""

    def __init__(self, gateway, alias, procesos=None, espera=2, latido=5, buscador=None, tipo="cpu", placa=None,
                 espera_aprobacion=ESPERA_APROBACION):
        self.gateway, self.alias, self.cuenta = gateway, alias, gateway.cuenta
        self.espera, self.latido, self.espera_aprobacion = espera, latido, espera_aprobacion
        self.buscador = buscador or Buscador(procesos)
        self.tipo, self.placa = tipo, placa or modelo_cpu()
        self.rango = None          # el rango en curso, para el keep-alive
        self._lock = threading.Lock()  # el rango y sus shares, entre el hilo que mina y el del keep-alive
        self.detenido = threading.Event()
        # El gateway no lo conoce (403) o perdió su nodo (`presentarse` en el keep-alive): pasa si la cadena
        # se reinicia con el minero andando. Entre un rango y el siguiente repite el /hello y el bench (BC-71).
        self.presentarse_de_nuevo = threading.Event()
        # BC-82: los shares que el gateway dijo aceptar en cada época, para compararlos con lo que se publica.
        self.aceptados, self.contados_de_menos = {}, []

    def _sumar_aceptados(self, rango, respuesta):
        if rango and "altura" in rango and respuesta and isinstance(respuesta.get("shares"), int):
            epoca = contrato.epoca(rango["altura"])
            with self._lock:
                self.aceptados[epoca] = self.aceptados.get(epoca, 0) + respuesta["shares"]

    def revisar_epocas(self, epoca_actual):
        """Las épocas anteriores a `epoca_actual` ya se pagaron: compara lo aceptado con lo publicado (BC-82)."""
        with self._lock:
            cerradas = sorted(e for e in self.aceptados if e < epoca_actual)
        for epoca in cerradas:
            try:
                codigo, publicada = self.gateway.pedir("GET", f"/epocas/{epoca}")
            except (urllib.error.URLError, OSError, ClaveRechazada) as error:
                # Es un control, no el trabajo: si el gateway no contesta, se pregunta con el próximo rango.
                log.debug("No pude preguntar por la época %d: %r", epoca, error)
                return
            if codigo != 200 or not publicada or not publicada.get("publicada"):
                continue
            with self._lock:
                aceptados = self.aceptados.pop(epoca, 0)
            if publicada["shares"] < aceptados:
                self.contados_de_menos.append((epoca, aceptados, publicada["shares"]))
                log.warning("Época %d: el gateway me aceptó %d shares y en la lista publicada figuran %d",
                            epoca, aceptados, publicada["shares"])
            else:
                log.info("Época %d: %d shares aceptados, %d publicados", epoca, aceptados, publicada["shares"])

    # --- Al arrancar -----------------------------------------------------------------------
    def presentarse(self):
        """POST /hello; si el gateway devuelve la tarea modelo, la resuelve y manda POST /bench."""
        _, respuesta = self._insistir("POST", "/hello", {"minero": self.cuenta, "tipo": self.tipo, "placa": self.placa,
                                                           "alias": self.alias})
        if not respuesta or "bloque" not in respuesta:
            log.info("Registrado en el gateway")
            self.presentarse_de_nuevo.clear()
            return
        log.info("Minero nuevo: resuelvo la tarea modelo (%d nonces)", respuesta["hasta"] - respuesta["desde"] + 1)
        segundos, vueltas = self._tarea_modelo(respuesta), 1
        if segundos < MEDIR_AL_MENOS:
            fin = time.monotonic() + MEDIR_AL_MENOS
            while time.monotonic() < fin:
                self._tarea_modelo(respuesta)
            segundos, vueltas = 0.0, 0
            while segundos < MEDIR_AL_MENOS:
                segundos, vueltas = segundos + self._tarea_modelo(respuesta), vueltas + 1
        hps = round(vueltas * (respuesta["hasta"] - respuesta["desde"] + 1) / segundos)
        codigo, _ = self._insistir("POST", "/bench", {"minero": self.cuenta, "segundos": round(segundos, 3),
                                                      "hashes_por_segundo": hps})
        log.info("Bench: %.2f s, %d hashes/s", segundos, hps)
        if codigo < 400:
            # Al final: un keep-alive de mientras medía todavía veía el nodo sin bench.
            self.presentarse_de_nuevo.clear()

    def _tarea_modelo(self, tarea):
        """Los segundos que tarda una vuelta de la tarea modelo."""
        inicio = time.monotonic()
        self.buscador.buscar(tarea["bloque"], tarea["desde"], tarea["hasta"], tarea["prefijo"])
        return time.monotonic() - inicio

    # --- Un ciclo: pedir, minar, devolver --------------------------------------------------
    def un_rango(self):
        """Pide un rango y lo resuelve. Devuelve False si no había trabajo."""
        # El gateway presta el rango a nombre de quien firma el pedido: no hace falta decir quién es.
        codigo, rango = self._insistir("GET", "/tasks/next")
        if codigo == 204 or not rango:
            return False
        with self._lock:
            self.rango = rango
        if "altura" in rango:
            self.revisar_epocas(contrato.epoca(rango["altura"]))
        log.info("Tarea %s rango %s: [%d, %d] prefijo %s", rango["tarea"], rango["rango"],
                 rango["desde"], rango["hasta"], rango["prefijo"])
        nonce, cortado = self.buscador.buscar(rango["bloque"], rango["desde"], rango["hasta"], rango["prefijo"])
        with self._lock:
            self.rango = None
            shares = self.buscador.shares.tomar()
        if cortado:
            log.info("Tarea %s ya confirmada: dejo el rango %s", rango["tarea"], rango["rango"])
            return True
        resultado = {"tarea": rango["tarea"], "rango": rango["rango"], "minero": self.cuenta, "shares": shares}
        if nonce is None:
            resultado["sin_solucion"] = True
            log.info("Rango %s sin solución (%d shares)", rango["rango"], len(shares))
        else:
            resultado.update(nonce=nonce, hash=contrato.hash_bloque(rango["bloque"], nonce))
            log.info("Encontré nonce %d: %s (%d shares)", nonce, resultado["hash"], len(shares))
        _, respuesta = self._insistir("POST", "/results", resultado)
        self._sumar_aceptados(rango, respuesta)
        return True

    def trabajar(self):
        threading.Thread(target=self._latir, name="keepalive", daemon=True).start()
        while not self.detenido.is_set():
            if self.presentarse_de_nuevo.is_set():
                log.info("El gateway no tiene mis datos (¿se reinició la cadena?): me vuelvo a presentar")
                self.presentarse()
                continue
            if not self.un_rango():
                self.detenido.wait(self.espera)

    # --- Keep-alive ------------------------------------------------------------------------
    def _latir(self):
        """Cada `latido` segundos avisa que está vivo y manda los shares que juntó; si el gateway
        contesta «cortá», abandona el rango."""
        while not self.detenido.wait(self.latido):
            with self._lock:
                rango = self.rango
                shares = self.buscador.shares.tomar() if rango else []
            aviso = {"minero": self.cuenta, "hashes_por_segundo": self.buscador.hashes_por_segundo(),
                     "rango": {"tarea": rango["tarea"], "rango": rango["rango"]} if rango else None}
            if rango:
                aviso["shares"] = shares
            try:
                codigo, respuesta = self.gateway.pedir("POST", "/keepalive", aviso)
            except (urllib.error.URLError, OSError) as error:
                log.warning("Keep-alive sin respuesta: %r", error)
                continue
            except ClaveRechazada:
                continue  # lo informa el hilo principal
            self._sumar_aceptados(rango, respuesta)
            if codigo == 403 or (respuesta and respuesta.get("presentarse")):
                self.presentarse_de_nuevo.set()
            if respuesta and respuesta.get("cortar") and rango is self.rango and rango is not None:
                self.buscador.cortar()

    def _insistir(self, metodo, ruta, cuerpo=None):
        """Reintenta mientras el gateway no conteste (espera creciente, hasta 30 s).

        Con 403 (cuenta sin aprobar) reintenta sólo el /hello, cada `espera_aprobacion` segundos: es el que
        lleva el alias para el panel del organizador. Cualquier otro pedido devuelve el 403 y deja anotado
        que hay que presentarse de nuevo.
        """
        espera = 1
        while True:
            try:
                codigo, respuesta = self.gateway.pedir(metodo, ruta, cuerpo)
                if codigo == 403 and ruta != "/hello":
                    if not self.presentarse_de_nuevo.is_set():
                        log.warning("El gateway rechazó %s %s: la cuenta %s no está aprobada", metodo, ruta, self.cuenta)
                    self.presentarse_de_nuevo.set()
                    return codigo, None
                if codigo == 403 and not self.detenido.is_set():
                    log.warning("La cuenta %s no está aprobada como minero: el organizador la tiene que aprobar. "
                                "Reintento en %d s", self.cuenta, self.espera_aprobacion)
                    self.detenido.wait(self.espera_aprobacion)
                    continue
                if codigo >= 400:
                    log.warning("El gateway contestó %d a %s %s", codigo, metodo, ruta)
                return codigo, respuesta
            except (urllib.error.URLError, OSError) as error:
                if self.detenido.is_set():
                    raise
                log.warning("Gateway sin respuesta en %s %s (%r); reintento en %d s", metodo, ruta, error, espera)
                time.sleep(espera)
                espera = min(espera * 2, 30)

    def detener(self):
        self.detenido.set()
        self.buscador.cortar()
        self.buscador.cerrar()


def clave_del_entorno(alias):
    return cargar_clave(alias, privada=config.texto("CLAVE_PRIVADA"), semilla=config.texto("CLAVE_SEMILLA"),
                        archivo=config.texto("CLAVE_ARCHIVO", "minero.clave"))


def alias_del_entorno():
    alias = config.texto("MINERO_ID", socket.gethostname())
    if not contrato.nombre_valido(alias):
        sys.exit(f"MINERO_ID {alias!r} no sirve: de 1 a 32 letras, números, «_», «.» o «-»")
    return alias


def main():
    log_nivel = config.texto("LOG_NIVEL", "INFO").upper()
    registro.configurar("minero_cpu", log_nivel, config.texto("LOGS_DIR", "logs"))
    alias = alias_del_entorno()
    gateway = Gateway(config.texto("GATEWAY_URL", "http://localhost:8080"), clave_del_entorno(alias))
    procesos = config.entero("PROCESOS", os.cpu_count() or 1)
    opciones = dict(espera=config.entero("ESPERA_SIN_TRABAJO", 2), latido=config.entero("KEEPALIVE_SEGUNDOS", 5))
    trampa = config.texto("TRAMPA")
    if trampa:  # BC-76: sólo para las pruebas de los ataques; apagado por defecto
        from .trampas import MineroTramposo
        minero = MineroTramposo(gateway, alias, procesos, trampa=trampa, **opciones)
    else:
        minero = Minero(gateway, alias, procesos, **opciones)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("Minero %s (cuenta %s): %d procesos, gateway %s", alias, gateway.cuenta, procesos, gateway.url)
    try:
        minero.presentarse()
        minero.trabajar()
    except ClaveRechazada as error:
        sys.exit(str(error))
    finally:
        minero.detener()
