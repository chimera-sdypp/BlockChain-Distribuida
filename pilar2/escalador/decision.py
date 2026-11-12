"""Cuántas VMs con mineros CPU hacen falta (BC-29). Sin E/S: se prueba sin Redis ni OpenTofu."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Ajustes:
    prefijo_base: int        # el prefijo del sistema (el mismo PREFIJO_BASE del TrP)
    segundos_objetivo: int   # en cuánto tiene que salir un bloque con ese prefijo
    maximo: int              # tope de VMs (quota y costo)
    hps_vm: float            # hashes/s que se le supone a una VM mientras no haya una viva
    espera_subir: int        # segundos sin GPU y con trabajo antes de crear VMs
    espera_bajar: int        # segundos que un número menor se tiene que sostener antes de borrar VMs
    vivo_segundos: int       # un minero cuenta si avisó hace menos que esto
    prefijo_vm: str          # el MINERO_ID de nuestras VMs empieza así (es su hostname)


@dataclass(frozen=True)
class Foto:
    """Lo que se ve en un ciclo."""
    gpus: int                # mineros GPU vivos
    ajena: float             # hashes/s de los mineros CPU vivos que no son nuestras VMs
    hps_vm: float | None     # bench promedio de nuestras VMs vivas (None si no hay ninguna)
    trabajo: bool            # hay transacciones esperando entrar en un bloque


def sacar_foto(nodos, ahora, trabajo, ajustes):
    """`nodos`: {id: hash nodo:<id> como lo devuelve Redis (todo texto)}."""
    gpus, ajena, benches = 0, 0.0, []
    for id_, nodo in nodos.items():
        try:
            if ahora - float(nodo.get("ultimo_aviso", 0)) >= ajustes.vivo_segundos:
                continue
            bench = float(nodo.get("bench", 0))
        except ValueError:
            continue  # un nodo a medio escribir no cuenta
        if nodo.get("tipo") == "gpu":
            gpus += 1
        elif id_.startswith(ajustes.prefijo_vm):
            if bench > 0:
                benches.append(bench)
        else:
            ajena += bench
    return Foto(gpus, ajena, sum(benches) / len(benches) if benches else None, trabajo)


def necesarias(foto, ajustes):
    """VMs para que, sin GPU, un bloque con el prefijo base salga en unos `segundos_objetivo`.

    Con p caracteres hacen falta 16^p intentos en promedio: la potencia objetivo es 16^p / T.
    Lo que ya aportan otros mineros CPU se descuenta. Con una GPU viva o sin trabajo, 0.
    """
    if foto.gpus or not foto.trabajo:
        return 0
    falta = 16 ** ajustes.prefijo_base / ajustes.segundos_objetivo - foto.ajena
    if falta <= 0:
        return 0
    return min(ajustes.maximo, math.ceil(falta / (foto.hps_vm or ajustes.hps_vm)))


class Decisor:
    """Lleva la cantidad de VMs sin oscilar.

    - Sube recién después de `espera_subir` segundos seguidos sin GPU y con trabajo: una GPU que
      se reinicia o un keep-alive perdido no levantan VMs.
    - Baja recién cuando el número menor se sostuvo `espera_bajar` segundos: si la GPU vuelve un
      momento y se va, las VMs siguen.
    """

    def __init__(self, ajustes, actual):
        self.ajustes, self.actual = ajustes, actual
        self._sin_gpu_desde = None
        self._menor_desde = None

    def decidir(self, foto, ahora):
        if foto.gpus or not foto.trabajo:
            self._sin_gpu_desde = None
        elif self._sin_gpu_desde is None:
            self._sin_gpu_desde = ahora
        objetivo = necesarias(foto, self.ajustes)
        if objetivo > self.actual:
            self._menor_desde = None
            if ahora - self._sin_gpu_desde >= self.ajustes.espera_subir:
                self.actual = objetivo
        elif objetivo < self.actual:
            if self._menor_desde is None:
                self._menor_desde = ahora
            if ahora - self._menor_desde >= self.ajustes.espera_bajar:
                self.actual, self._menor_desde = objetivo, None
        else:
            self._menor_desde = None
        return self.actual
