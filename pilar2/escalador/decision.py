"""Cuántas VMs con mineros CPU hacen falta (BC-29). Sin E/S: se prueba sin Redis ni OpenTofu."""

import math
from dataclasses import dataclass

from comun import contrato


@dataclass(frozen=True)
class Ajustes:
    prefijo_base: int        # el prefijo del sistema (el mismo PREFIJO_BASE del TrP)
    segundos_objetivo: int   # en cuánto tiene que salir un bloque con ese prefijo
    maximo: int              # tope de VMs (quota y costo)
    hps_vm: float            # hashes/s que se le supone a una VM mientras no haya una viva
    espera_subir: int        # segundos sin GPU y con trabajo antes de crear VMs
    espera_bajar: int        # segundos que un número menor se tiene que sostener antes de borrar VMs
    vivo_segundos: int       # un minero cuenta si avisó hace menos que esto
    prefijo_vm: str          # el alias de nuestras VMs empieza así (es su nombre, que el escalador pone en `cuentas`)
    sin_bloque: int = 300    # BC-74: con trabajo y sin bloque hace tanto, VMs igual, crea quien crea lo que haya (0 = no)


@dataclass(frozen=True)
class Foto:
    """Lo que se ve en un ciclo."""
    gpus: int                # mineros vivos que dicen ser GPU (sólo para el log: decide la potencia)
    ajena: float             # hashes/s de los mineros vivos que no son nuestras VMs (GPU incluidas): la potencia
                             # efectiva de cada uno, min(bench, medido) (BC-74)
    hps_vm: float | None     # bench promedio de nuestras VMs vivas (None si no hay ninguna)
    trabajo: bool            # hay transacciones esperando entrar en un bloque
    sin_bloque: float = 0.0  # segundos desde que se armó la caja que se está minando (0 si no hay)


def sacar_foto(nodos, ahora, trabajo, ajustes, sin_bloque=0.0):
    """`nodos`: {cuenta: hash nodo:<cuenta> como lo devuelve Redis (todo texto)}.

    Las VMs propias se reconocen por el alias, que el gateway saca de `cuentas` (lo escribió el
    escalador al aprobarlas): un minero cualquiera no puede hacerse pasar por una VM.
    Los demás cuentan con su potencia efectiva (BC-74): un minero que dice `tipo: gpu` o un bench alto pero
    no manda shares mide 0 y no le saca VMs a nadie.
    """
    gpus, ajena, benches = 0, 0.0, []
    for id_, nodo in nodos.items():
        try:
            if ahora - float(nodo.get("ultimo_aviso", 0)) >= ajustes.vivo_segundos:
                continue
            bench = float(nodo.get("bench", 0))
        except ValueError:
            continue  # un nodo a medio escribir no cuenta
        if nodo.get("alias", id_).startswith(ajustes.prefijo_vm) and nodo.get("tipo") != "gpu":
            if bench > 0:
                benches.append(bench)
            continue
        if nodo.get("tipo") == "gpu":
            gpus += 1
        ajena += contrato.potencia_efectiva(nodo)
    return Foto(gpus, ajena, sum(benches) / len(benches) if benches else None, trabajo, sin_bloque)


def necesarias(foto, ajustes):
    """VMs para que un bloque con el prefijo base salga en unos `segundos_objetivo`.

    Con p caracteres hacen falta 16^p intentos en promedio: la potencia objetivo es 16^p / T.
    Se descuenta la potencia efectiva de los demás mineros (una GPU de verdad alcanza sola). Sin trabajo, 0.
    Si hay trabajo y hace `sin_bloque` segundos que no sale un bloque, no se descuenta nada: alguien dice tener
    potencia que no aparece (BC-74).
    """
    if not foto.trabajo:
        return 0
    objetivo = 16 ** ajustes.prefijo_base / ajustes.segundos_objetivo
    trabado = ajustes.sin_bloque > 0 and foto.sin_bloque >= ajustes.sin_bloque
    falta = objetivo if trabado else objetivo - foto.ajena
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
        if necesarias(foto, self.ajustes) == 0:
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
