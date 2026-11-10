"""Minero CPU: pide rangos al gateway por HTTP, los recorre y devuelve el resultado.

Corre fuera del cluster (VMs o cualquier máquina): sólo necesita la URL del gateway y el MINER_TOKEN.
"""

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
import urllib.parse
import urllib.request

from comun import config, contrato, registro

from .busqueda import Buscador

log = logging.getLogger("minero_cpu")


class TokenInvalido(Exception):
    pass


def modelo_cpu():
    try:
        with open("/proc/cpuinfo") as cpuinfo:
            for linea in cpuinfo:
                if linea.startswith("model name"):
                    return linea.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "CPU"


class Gateway:
    """Cliente HTTP del gateway (API en CONTRATO.md)."""

    def __init__(self, url, token, timeout=10):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def pedir(self, metodo, ruta, cuerpo=None):
        """Devuelve (código, JSON o None). Los errores de red los levanta como URLError."""
        datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
        pedido = urllib.request.Request(self.url + ruta, data=datos, method=metodo, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as respuesta:
                texto = respuesta.read()
                return respuesta.status, json.loads(texto) if texto else None
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise TokenInvalido("el gateway rechazó el MINER_TOKEN") from None
            return error.code, None


class Minero:
    """El cliente del gateway. Lo usa también el minero GPU, con su propio buscador y tipo «gpu»."""

    def __init__(self, gateway, minero_id, procesos=None, espera=2, latido=5, buscador=None, tipo="cpu", placa=None):
        self.gateway, self.id = gateway, minero_id
        self.espera, self.latido = espera, latido
        self.buscador = buscador or Buscador(procesos)
        self.tipo, self.placa = tipo, placa or modelo_cpu()
        self.rango = None          # el rango en curso, para el keep-alive
        self.detenido = threading.Event()

    # --- Al arrancar -----------------------------------------------------------------------
    def presentarse(self):
        """POST /hello; si el gateway devuelve la tarea modelo, la resuelve y manda POST /bench."""
        _, respuesta = self._insistir("POST", "/hello", {"minero": self.id, "tipo": self.tipo, "placa": self.placa})
        if not respuesta or "bloque" not in respuesta:
            log.info("Registrado en el gateway")
            return
        log.info("Minero nuevo: resuelvo la tarea modelo (%d nonces)", respuesta["hasta"] - respuesta["desde"] + 1)
        inicio = time.monotonic()
        self.buscador.buscar(respuesta["bloque"], respuesta["desde"], respuesta["hasta"], respuesta["prefijo"])
        segundos = time.monotonic() - inicio
        hps = round((respuesta["hasta"] - respuesta["desde"] + 1) / segundos)
        self._insistir("POST", "/bench", {"minero": self.id, "segundos": round(segundos, 3), "hashes_por_segundo": hps})
        log.info("Bench: %.2f s, %d hashes/s", segundos, hps)

    # --- Un ciclo: pedir, minar, devolver --------------------------------------------------
    def un_rango(self):
        """Pide un rango y lo resuelve. Devuelve False si no había trabajo."""
        # El id va en la query: el gateway presta el rango a nombre de este minero.
        codigo, rango = self._insistir("GET", "/tasks/next?" + urllib.parse.urlencode({"minero": self.id}))
        if codigo == 204 or not rango:
            return False
        self.rango = rango
        log.info("Tarea %s rango %s: [%d, %d] prefijo %s", rango["tarea"], rango["rango"],
                 rango["desde"], rango["hasta"], rango["prefijo"])
        nonce, cortado = self.buscador.buscar(rango["bloque"], rango["desde"], rango["hasta"], rango["prefijo"])
        self.rango = None
        if cortado:
            log.info("Tarea %s ya confirmada: dejo el rango %s", rango["tarea"], rango["rango"])
            return True
        resultado = {"tarea": rango["tarea"], "rango": rango["rango"], "minero": self.id}
        if nonce is None:
            resultado["sin_solucion"] = True
            log.info("Rango %s sin solución", rango["rango"])
        else:
            resultado.update(nonce=nonce, hash=contrato.hash_bloque(rango["bloque"], nonce))
            log.info("Encontré nonce %d: %s", nonce, resultado["hash"])
        self._insistir("POST", "/results", resultado)
        return True

    def trabajar(self):
        threading.Thread(target=self._latir, name="keepalive", daemon=True).start()
        while not self.detenido.is_set():
            if not self.un_rango():
                self.detenido.wait(self.espera)

    # --- Keep-alive ------------------------------------------------------------------------
    def _latir(self):
        """Cada `latido` segundos avisa que está vivo; si el gateway contesta «cortá», abandona el rango."""
        while not self.detenido.wait(self.latido):
            rango = self.rango
            aviso = {"minero": self.id, "hashes_por_segundo": self.buscador.hashes_por_segundo(),
                     "rango": {"tarea": rango["tarea"], "rango": rango["rango"]} if rango else None}
            try:
                _, respuesta = self.gateway.pedir("POST", "/keepalive", aviso)
            except (urllib.error.URLError, OSError) as error:
                log.warning("Keep-alive sin respuesta: %r", error)
                continue
            except TokenInvalido:
                continue  # lo informa el hilo principal
            if respuesta and respuesta.get("cortar") and rango is self.rango and rango is not None:
                self.buscador.cortar()

    def _insistir(self, metodo, ruta, cuerpo=None):
        """Reintenta mientras el gateway no conteste (espera creciente, hasta 30 s)."""
        espera = 1
        while True:
            try:
                codigo, respuesta = self.gateway.pedir(metodo, ruta, cuerpo)
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


def main():
    log_nivel = config.texto("LOG_NIVEL", "INFO").upper()
    registro.configurar("minero_cpu", log_nivel, config.texto("LOGS_DIR", "logs"))
    minero_id = config.texto("MINERO_ID", socket.gethostname())
    if not contrato.usuario_valido(minero_id):
        sys.exit(f"MINERO_ID {minero_id!r} no sirve: de 1 a 32 letras, números, «_» o «-»")
    token = config.texto("MINER_TOKEN")
    if not token:
        sys.exit("Falta MINER_TOKEN")
    gateway = Gateway(config.texto("GATEWAY_URL", "http://localhost:8080"), token)
    procesos = config.entero("PROCESOS", os.cpu_count() or 1)
    minero = Minero(gateway, minero_id, procesos, espera=config.entero("ESPERA_SIN_TRABAJO", 2),
                    latido=config.entero("KEEPALIVE_SEGUNDOS", 5))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("Minero %s: %d procesos, gateway %s", minero_id, procesos, gateway.url)
    try:
        minero.presentarse()
        minero.trabajar()
    except TokenInvalido as error:
        sys.exit(str(error))
    finally:
        minero.detener()
