"""Búsqueda del nonce en la GPU: le pasa el rango al binario CUDA (`minar.cu`) de a trozos.

El binario queda abierto (arrancar CUDA cuesta) y atiende un pedido por vez por stdin/stdout.
Los trozos son de TROZO nonces (~1 s en una RTX 4060): entre uno y otro se mira si hay que
cortar, así un «cortá» del gateway no espera a que termine un rango de 400 millones.
Tiene la misma interfaz que el Buscador del minero CPU, así que el resto es el mismo minero.
"""

import subprocess
import threading
import time

TROZO = 2**30


class ErrorGPU(Exception):
    pass


class BuscadorGPU:
    def __init__(self, binario, trozo=TROZO):
        self.trozo = trozo
        self._proceso = subprocess.Popen([binario], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        listo = self._proceso.stdout.readline().decode().strip()
        if not listo.startswith("LISTO"):
            self.cerrar()
            raise ErrorGPU(f"el binario no arrancó: {listo or 'sin respuesta'}")
        self.placa = listo.removeprefix("LISTO").strip()
        self.parar = threading.Event()
        self._lock = threading.Lock()  # un pedido por vez al binario
        self.probados = 0
        self.inicio = time.monotonic()

    def hashes_por_segundo(self):
        segundos = time.monotonic() - self.inicio
        return round(self.probados / segundos) if segundos > 0 else 0

    def _pedir(self, datos, prefijo, desde, hasta):
        with self._lock:
            self._proceso.stdin.write(f"{prefijo} {desde} {hasta} {len(datos)}\n".encode() + datos)
            self._proceso.stdin.flush()
            linea = self._proceso.stdout.readline().decode().strip()
        if not linea:
            raise ErrorGPU(f"el binario terminó (código {self._proceso.poll()})")
        palabra, *campos = linea.split(" ", 1)
        if palabra == "ERROR":
            raise ErrorGPU(linea)
        return palabra, dict(campo.split("=", 1) for campo in (campos[0].split() if campos else []))

    def buscar(self, bloque, desde, hasta, prefijo):
        """Recorre [desde, hasta] en orden. Devuelve (menor nonce o None, cortado)."""
        self.parar.clear()
        self.probados, self.inicio = 0, time.monotonic()
        datos = bloque.encode()
        siguiente = desde
        while siguiente <= hasta:
            if self.parar.is_set():
                return None, True
            fin = min(siguiente + self.trozo - 1, hasta)
            palabra, campos = self._pedir(datos, prefijo, siguiente, fin)
            self.probados += int(campos["evaluados"])
            if palabra == "OK":
                return int(campos["nonce"]), False
            siguiente = fin + 1
        return None, False

    def cortar(self):
        self.parar.set()

    def cerrar(self):
        if self._proceso.stdin:
            try:
                self._proceso.stdin.close()
            except OSError:
                pass
        try:
            self._proceso.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proceso.kill()
