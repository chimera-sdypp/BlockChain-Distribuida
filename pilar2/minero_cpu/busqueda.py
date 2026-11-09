"""Búsqueda del nonce en CPU, repartida entre varios procesos (en Python, por el GIL, hilos no alcanzan).

El rango se corta en trozos que los procesos toman en orden. Los resultados se leen en el mismo
orden, así que el primer trozo con solución da el menor nonce del rango, igual que el minero GPU
del Hit 7.
"""

import hashlib
import multiprocessing
import time

TROZO = 2**16  # nonces por trozo: ~50 ms en un núcleo; cada cuánto se mira si hay que cortar

_parar = None  # Event compartido: lo levanta el minero para abandonar el rango


def _iniciar(parar):
    global _parar
    _parar = parar


def _probar(trozo):
    """Prueba un trozo [desde, hasta]; devuelve el primer nonce que cumple, o None."""
    bloque, prefijo, desde, hasta = trozo
    if _parar.is_set():
        return None
    # El bloque es el mismo para todos los nonces: se hashea una vez y se copia el estado.
    base = hashlib.md5(bloque.encode())
    for nonce in range(desde, hasta + 1):
        h = base.copy()
        h.update(str(nonce).encode())
        if h.hexdigest().startswith(prefijo):
            return nonce
    return None


class Buscador:
    def __init__(self, procesos):
        self.procesos = procesos
        self.parar = multiprocessing.Event()
        self._pool = multiprocessing.Pool(procesos, initializer=_iniciar, initargs=(self.parar,))
        self.probados = 0          # nonces probados en el rango actual (lo lee el keep-alive)
        self.inicio = time.monotonic()

    def hashes_por_segundo(self):
        segundos = time.monotonic() - self.inicio
        return round(self.probados / segundos) if segundos > 0 else 0

    def buscar(self, bloque, desde, hasta, prefijo):
        """Recorre [desde, hasta]. Devuelve (nonce o None, cortado).

        cortado es True si alguien llamó a cortar() antes de terminar: el resultado no sirve.
        """
        self.parar.clear()
        self.probados, self.inicio = 0, time.monotonic()
        siguiente = desde
        while siguiente <= hasta:
            # De a lotes, para no armar millones de trozos si el rango es enorme.
            lote = []
            while siguiente <= hasta and len(lote) < 4 * self.procesos:
                fin = min(siguiente + TROZO - 1, hasta)
                lote.append((bloque, prefijo, siguiente, fin))
                siguiente = fin + 1
            resultados = self._pool.imap(_probar, lote)
            for (_, _, d, h), nonce in zip(lote, resultados):
                if self.parar.is_set():
                    break
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
