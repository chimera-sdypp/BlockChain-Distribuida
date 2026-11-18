"""Escalador (P5, BC-29): levanta y borra VMs con mineros CPU cuando no hay GPU.

No está siempre prendido: es un Deployment de 0 o 1 pod que prende KEDA cuando se apilan rangos
o transacciones, o mientras queden mineros CPU vivos (ver k8s/escalador/). Mientras corre, cada
ciclo mira `nodo:*` y el trabajo apilado, decide cuántas VMs hacen falta y, si cambió, se lo pide
a OpenTofu (`tofu apply -var cantidad=N`): el apply es declarativo, así que repetirlo no crea de más.

Cada VM mina con su propia clave (BC-57): una VM no puede pedir el alta a mano, así que el escalador,
que es un servicio de confianza, le genera el par, aprueba la pública en `cuentas` y le pasa la
privada por metadata. Al borrar la VM, la da de baja.
"""

import json
import logging
import os
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
CLAVES = "escalador:claves"  # nombre de la VM → su clave privada (el pod siguiente usa las mismas)


class ClavesVMs:
    """La clave de cada VM minero-cpu-<n>: se crea al pedirla, se aprueba como minero y se da de baja al borrarla."""

    def __init__(self, redis, prefijo="minero-cpu-"):
        self.redis, self.prefijo = redis, prefijo

    def preparar(self, cantidad, ahora=None):
        """{nombre: privada} de las VMs 0 a cantidad − 1, aprobadas en `cuentas` (las que faltan, nuevas)."""
        guardadas = self.redis.hgetall(CLAVES)
        claves = {f"{self.prefijo}{n}": guardadas.get(f"{self.prefijo}{n}") or contrato.nueva_clave()[0]
                  for n in range(cantidad)}
        nuevas = {nombre: privada for nombre, privada in claves.items() if guardadas.get(nombre) != privada}
        if nuevas:
            self.redis.hset(CLAVES, mapping=nuevas)
        alta = int(time.time() if ahora is None else ahora)
        for nombre, privada in claves.items():
            datos = json.dumps({"tipo": "minero", "nombre": nombre, "alta": alta})
            self.redis.hsetnx(contrato.CUENTAS, contrato.clave_publica(privada), datos)
        for nombre in nuevas:
            log.info("Clave nueva para %s: %s, aprobada como minero", nombre, contrato.clave_publica(claves[nombre]))
        return claves

    def retirar(self, cantidad):
        """Da de baja las claves de las VMs desde `cantidad` en adelante (las que se borraron)."""
        sobran = {nombre: privada for nombre, privada in self.redis.hgetall(CLAVES).items()
                  if nombre.startswith(self.prefijo) and nombre[len(self.prefijo):].isdigit()
                  and int(nombre[len(self.prefijo):]) >= cantidad}
        for nombre, privada in sorted(sobran.items()):
            self.redis.hdel(contrato.CUENTAS, contrato.clave_publica(privada))
            self.redis.hdel(CLAVES, nombre)
            log.info("%s se borró: su clave queda dada de baja", nombre)
        return sorted(sobran)


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
    """Las VMs de verdad: el módulo de BC-35, con las variables `cantidad` y `claves`, y el output `cantidad`.

    El state y su lock están en el bucket (el backend lo define el módulo), así que un apply a
    medias de un pod anterior no se pisa con éste. Las claves van por entorno (TF_VAR_claves), no en
    los argumentos: así no aparecen en la lista de procesos.
    """

    def __init__(self, directorio, claves=None, binario="tofu", ejecutar=subprocess.run, timeout=900):
        self.directorio, self.claves, self.binario = directorio, claves, binario
        self.ejecutar, self.timeout = ejecutar, timeout
        self._tofu("init", "-input=false")

    def _tofu(self, *argumentos, entorno=None):
        return self.ejecutar([self.binario, f"-chdir={self.directorio}", *argumentos], check=True,
                             capture_output=True, text=True, timeout=self.timeout, env=entorno)

    def actual(self):
        try:
            salida = self._tofu("output", "-no-color", "-raw", "cantidad").stdout.strip()
        except subprocess.CalledProcessError:
            return 0  # sin state todavía: nunca se aplicó
        # Con el state vacío tofu no falla: avisa «No outputs found» por stdout.
        return int(salida) if salida.isdigit() else 0

    def aplicar(self, cantidad):
        """Primero las claves (una VM nueva arranca aprobada), después el apply y al final las bajas."""
        claves = self.claves.preparar(cantidad) if self.claves else {}
        entorno = {**os.environ, "TF_VAR_claves": json.dumps(claves)}
        salida = self._tofu("apply", "-auto-approve", "-input=false", "-var", f"cantidad={cantidad}", entorno=entorno)
        log.info("tofu apply cantidad=%d: %s", cantidad, (salida.stdout.strip().splitlines() or [""])[-1])
        if self.claves:
            self.claves.retirar(cantidad)


def cargar_ajustes():
    ajustes = Ajustes(
        prefijo_base=config.entero("PREFIJO_BASE", 6),
        segundos_objetivo=config.entero("SEGUNDOS_POR_BLOQUE", 60),
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
        aplicador = Tofu(config.texto("TOFU_DIR", "/tofu"), claves=ClavesVMs(servicio.redis, ajustes.prefijo_vm))
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
