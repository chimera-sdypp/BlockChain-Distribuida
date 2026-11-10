"""Escalador (P5, BC-29): levanta y borra VMs con mineros CPU cuando no hay GPU.

No está siempre prendido: es un Deployment de 0 o 1 pod que prende KEDA cuando se apilan rangos
o transacciones, o mientras queden mineros CPU vivos (ver k8s/escalador/). Mientras corre, cada
ciclo mira `nodo:*` y el trabajo apilado, decide cuántas VMs hacen falta y, si cambió, se lo pide
a OpenTofu (`tofu apply -var cantidad=N`): el apply es declarativo, así que repetirlo no crea de más.
"""

import logging
import signal
import subprocess
import threading
import time

from prometheus_client import Counter, Gauge

from comun import Servicio, config, contrato

from .decision import Ajustes, Decisor, sacar_foto

log = logging.getLogger("escalador")

VMS = Gauge("blockchain_vms_cpu", "VMs con mineros CPU que pidió el escalador")
APLICADOS = Counter("blockchain_escalador_aplicados", "Cambios de cantidad de VMs aplicados")
FALLIDOS = Counter("blockchain_escalador_fallidos", "Cambios de cantidad de VMs que fallaron (se reintentan)")

SIMULADAS = "escalador:vms"  # la cantidad, cuando las VMs están apagadas por configuración


class Simulado:
    """VMS_ACTIVAS=0: no toca la nube, sólo anota la cantidad en Redis (para probar KEDA en k3s)."""

    def __init__(self, redis):
        self.redis = redis

    def actual(self):
        return int(self.redis.get(SIMULADAS) or 0)

    def aplicar(self, cantidad):
        self.redis.set(SIMULADAS, cantidad)
        log.info("VMs simuladas: %d", cantidad)


class Tofu:
    """Las VMs de verdad: el módulo de BC-35, con la variable y el output `cantidad`.

    El state y su lock están en el bucket (el backend lo define el módulo), así que un apply a
    medias de un pod anterior no se pisa con éste.
    """

    def __init__(self, directorio, binario="tofu", ejecutar=subprocess.run, timeout=900):
        self.directorio, self.binario = directorio, binario
        self.ejecutar, self.timeout = ejecutar, timeout
        self._tofu("init", "-input=false")

    def _tofu(self, *argumentos):
        return self.ejecutar([self.binario, f"-chdir={self.directorio}", *argumentos], check=True,
                             capture_output=True, text=True, timeout=self.timeout)

    def actual(self):
        try:
            return int(self._tofu("output", "-raw", "cantidad").stdout.strip() or 0)
        except subprocess.CalledProcessError:
            return 0  # sin state todavía: nunca se aplicó

    def aplicar(self, cantidad):
        salida = self._tofu("apply", "-auto-approve", "-input=false", "-var", f"cantidad={cantidad}")
        log.info("tofu apply cantidad=%d: %s", cantidad, (salida.stdout.strip().splitlines() or [""])[-1])


def cargar_ajustes():
    ajustes = Ajustes(
        prefijo_base=config.entero("PREFIJO_BASE", 6),
        segundos_objetivo=config.entero("SEGUNDOS_POR_BLOQUE", 30),
        maximo=config.entero("VMS_MAXIMO", 3),
        hps_vm=config.entero("HPS_POR_VM", 1_000_000),
        espera_subir=config.entero("ESPERA_SUBIR_SEGUNDOS", 30),
        espera_bajar=config.entero("ESPERA_BAJAR_SEGUNDOS", 120),
        vivo_segundos=config.entero("VIVO_SEGUNDOS", 15),
        prefijo_vm=config.texto("PREFIJO_VMS", "minero-cpu-"),
    )
    if ajustes.maximo < 0 or ajustes.hps_vm <= 0 or ajustes.segundos_objetivo <= 0:
        raise ValueError("VMS_MAXIMO >= 0, HPS_POR_VM > 0 y SEGUNDOS_POR_BLOQUE > 0")
    return ajustes


def hay_trabajo(redis):
    """Transacciones validadas que esperan caja, o cajas sin bloque (la primera es la que se mina)."""
    tuberia = redis.pipeline(transaction=False)
    tuberia.llen(contrato.PENDIENTES)
    tuberia.llen(contrato.CAJAS)
    return sum(tuberia.execute()) > 0


def leer_nodos(redis):
    claves = list(redis.scan_iter(match=contrato.NODOS, count=100))
    tuberia = redis.pipeline(transaction=False)
    for clave in claves:
        tuberia.hgetall(clave)
    return {clave.split(":", 1)[1]: nodo for clave, nodo in zip(claves, tuberia.execute())}


def ciclo(redis, decisor, aplicador, ahora=None):
    """Un ciclo: foto, decisión y, si cambió la cantidad, apply. Devuelve la cantidad pedida."""
    ahora = time.time() if ahora is None else ahora
    foto = sacar_foto(leer_nodos(redis), ahora, hay_trabajo(redis), decisor.ajustes)
    antes = decisor.actual
    cantidad = decisor.decidir(foto, ahora)
    if cantidad != antes:
        log.info("VMs: %d → %d (GPU vivas %d, CPU ajena %.0f h/s, trabajo %s)",
                 antes, cantidad, foto.gpus, foto.ajena, foto.trabajo)
        try:
            aplicador.aplicar(cantidad)
        except Exception:
            decisor.actual = antes  # se reintenta en el próximo ciclo
            FALLIDOS.inc()
            raise
        APLICADOS.inc()
    VMS.set(cantidad)
    return cantidad


def main():
    servicio = Servicio("escalador", usa_rabbit=False)
    ajustes = cargar_ajustes()
    servicio.log.info("Ajustes: %s", ajustes)
    if config.entero("VMS_ACTIVAS", 0) == 1:
        aplicador = Tofu(config.texto("TOFU_DIR", "/tofu"))
    else:
        servicio.log.warning("VMS_ACTIVAS=0: las VMs se simulan en Redis (%s)", SIMULADAS)
        aplicador = Simulado(servicio.redis)
    decisor = Decisor(ajustes, aplicador.actual())
    VMS.set(decisor.actual)
    servicio.servir_http()
    # KEDA lo apaga con SIGTERM: se termina el ciclo en curso (un apply a medias deja el lock tomado).
    detenido = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: detenido.set())
    cada = config.entero("CICLO_SEGUNDOS", 10)
    while not detenido.is_set():
        try:
            ciclo(servicio.redis, decisor, aplicador)
        except Exception:
            log.exception("Falló el ciclo; se reintenta en %d s", cada)
        detenido.wait(cada)
    log.info("Apagado con %d VMs pedidas", decisor.actual)
