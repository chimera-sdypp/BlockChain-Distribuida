"""Búsqueda del nonce en CPU, repartida entre varios procesos (en Python, por el GIL, hilos no alcanzan).

El rango se corta en trozos que los procesos toman en orden. Los resultados se leen en el mismo
orden, así que el primer trozo con solución da el menor nonce del rango, igual que el minero GPU
del Hit 7. De paso junta los shares (BC-63): los nonces cuyo hash cumple el prefijo menos un
carácter, hasta SHARES_POR_RANGO por rango.
"""

import hashlib
import multiprocessing
import threading
import time

from comun import contrato

TROZO = 2**16  # nonces por trozo: ~50 ms en un núcleo; cada cuánto se mira si hay que cortar

_parar = None  # Event compartido: lo levanta el minero para abandonar el rango


def _iniciar(parar):
    global _parar
    _parar = parar


def _probar(trozo):
    """Prueba un trozo [desde, hasta]: (el primer nonce que cumple o None, los shares hasta ahí)."""
    bloque, prefijo, desde, hasta = trozo
    if _parar.is_set():
        return None, []
    corto, shares = contrato.prefijo_share(prefijo), []
    # El bloque es el mismo para todos los nonces: se hashea una vez y se copia el estado.
    base = hashlib.md5(bloque.encode())
    for nonce in range(desde, hasta + 1):
        h = base.copy()
        h.update(str(nonce).encode())
        digest = h.hexdigest()
        if corto is not None and digest.startswith(corto):
            if len(shares) < contrato.SHARES_POR_RANGO:
                shares.append(nonce)
            if digest.startswith(prefijo):
                return nonce, shares
        elif corto is None and digest.startswith(prefijo):
            return nonce, shares
    return None, shares


class Shares:
    """Los shares de un rango: el buscador los agrega y el minero se los lleva (en el keep-alive y en
    el resultado). Seguro entre hilos; no pasa de SHARES_POR_RANGO por rango."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pendientes, self._juntados = [], 0

    def empezar(self):
        with self._lock:
            self._pendientes, self._juntados = [], 0

    def agregar(self, nonces):
        with self._lock:
            libres = contrato.SHARES_POR_RANGO - self._juntados
            nuevos = list(nonces)[:max(libres, 0)]
            self._pendientes += nuevos
            self._juntados += len(nuevos)

    def tomar(self):
        with self._lock:
            pendientes, self._pendientes = self._pendientes, []
            return pendientes


class Ritmo:
    """Hashes/s que manda el keep-alive: los probados sobre el tiempo que se buscó, en el rango en curso o, entre
    rangos, en el último. Contra el tiempo desde que empezó el rango, con el minero parado bajaba como 1/t."""

    probados, inicio, fin = 0, 0.0, None

    def hashes_por_segundo(self):
        segundos = (self.fin or time.monotonic()) - self.inicio
        return round(self.probados / segundos) if self.probados and segundos > 0 else 0

    def buscar(self, bloque, desde, hasta, prefijo):
        """Recorre [desde, hasta]. Devuelve (nonce o None, cortado); los shares quedan en `self.shares`.

        cortado es True si alguien llamó a cortar() antes de terminar: el resultado no sirve.
        """
        self.parar.clear()
        self.probados, self.inicio, self.fin = 0, time.monotonic(), None
        self.shares.empezar()
        try:
            return self._recorrer(bloque, desde, hasta, prefijo)
        finally:
            self.fin = time.monotonic()


class Buscador(Ritmo):
    def __init__(self, procesos):
        self.procesos = procesos
        self.parar = multiprocessing.Event()
        self._pool = multiprocessing.Pool(procesos, initializer=_iniciar, initargs=(self.parar,))
        self.shares = Shares()

    def _recorrer(self, bloque, desde, hasta, prefijo):
        siguiente = desde
        while siguiente <= hasta:
            # De a lotes, para no armar millones de trozos si el rango es enorme.
            lote = []
            while siguiente <= hasta and len(lote) < 4 * self.procesos:
                fin = min(siguiente + TROZO - 1, hasta)
                lote.append((bloque, prefijo, siguiente, fin))
                siguiente = fin + 1
            resultados = self._pool.imap(_probar, lote)
            for (_, _, d, h), (nonce, shares) in zip(lote, resultados):
                if self.parar.is_set():
                    break
                self.shares.agregar(shares)
                if nonce is not None:
                    self.probados += nonce - d + 1
                    self.parar.set()
                    break
                self.probados += h - d + 1
            if self.parar.is_set():
                for _ in resultados:  # esperar los que quedaron del lote (salen sin calcular)
                    pass
                return (nonce, False) if nonce is not None else (None, True)
        return None, False

    def cortar(self):
        self.parar.set()

    def cerrar(self):
        self._pool.terminate()
