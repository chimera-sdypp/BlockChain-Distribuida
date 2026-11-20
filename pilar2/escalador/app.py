"""Escalador (P5, BC-29): levanta y borra VMs con mineros CPU cuando no hay GPU.

No está siempre prendido: es un Deployment de 0 o 1 pod que prende KEDA cuando se apilan rangos
o transacciones, o mientras queden mineros CPU vivos (ver k8s/escalador/). Mientras corre, cada
ciclo mira `nodo:*` y el trabajo apilado, decide cuántas VMs hacen falta y, si cambió, se lo pide
a OpenTofu (`tofu apply -var cantidad=N`): el apply es declarativo, así que repetirlo no crea de más.

Cada VM mina con su propia clave (BC-57): una VM no puede pedir el alta a mano, así que el escalador le
genera el par y le pasa la privada por metadata. Desde el contrato v3 (BC-79) el escalador no escribe
`cuentas`: tiene su propia clave y un **permiso** del organizador (una transacción `permiso-<n>` en la cadena)
para dar de alta y de baja sólo mineros `minero-cpu-*`. Cada alta es una transacción firmada por esa clave que
manda al backend; el NCT la valida en el momento y el gateway deja minar a la VM con el alta «en camino», así
puede minar el bloque que la confirma. No puede aprobar usuarios ni cargar saldo.
"""

import json
import logging
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

from prometheus_client import Counter, Gauge

from comun import Servicio, config, contrato

from .decision import Ajustes, Decisor, sacar_foto

log = logging.getLogger("escalador")

VMS = Gauge("blockchain_vms_cpu", "VMs con mineros CPU que pidió el escalador")
APLICADOS = Counter("blockchain_escalador_aplicados", "Cambios de cantidad de VMs aplicados")
FALLIDOS = Counter("blockchain_escalador_fallidos", "Cambios de cantidad de VMs que fallaron (se reintentan)")

SIMULADAS = "escalador:vms"  # la cantidad, cuando las VMs están apagadas por configuración
CLAVES = "escalador:claves"  # nombre de la VM → su clave privada (el pod siguiente usa las mismas)
CLAVE_PROPIA = "escalador:clave"  # la privada del escalador, si no viene en ESCALADOR_CLAVE (la genera la primera vez)


class Backend:
    """Lo poco del backend que usa el escalador: el último nonce de su clave y mandar transacciones."""

    def __init__(self, url, timeout=10):
        self.url, self.timeout = url.rstrip("/"), timeout

    def _pedir(self, metodo, ruta, cuerpo=None):
        datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
        pedido = urllib.request.Request(self.url + ruta, data=datos, method=metodo,
                                        headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as respuesta:
                return respuesta.status, json.loads(respuesta.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    def nonce(self, cuenta):
        codigo, datos = self._pedir("GET", f"/api/cuentas/{cuenta}")
        if codigo != 200:
            raise RuntimeError(f"el backend no dio la cuenta {cuenta}: {codigo} {datos}")
        return datos["nonce"]

    def mandar(self, tx):
        return self._pedir("POST", "/api/transacciones", tx)


class ClavesVMs:
    """La clave de cada VM minero-cpu-<n>: se crea al pedirla y se da de alta en la cadena con un alta delegada,
    firmada por la clave del escalador (que tiene el permiso del organizador). Al borrar la VM, baja delegada."""

    def __init__(self, redis, backend, prefijo="minero-cpu-", privada=None):
        self.redis, self.backend, self.prefijo = redis, backend, prefijo
        self.privada = privada or self._clave_propia()
        self.cuenta = contrato.clave_publica(self.privada)
        self._avisado = False  # el aviso de que falta el permiso sale una vez, no en cada ciclo

    def _clave_propia(self):
        privada = self.redis.get(CLAVE_PROPIA)
        if not privada:
            privada = contrato.nueva_clave()[0]
            if not self.redis.set(CLAVE_PROPIA, privada, nx=True):
                privada = self.redis.get(CLAVE_PROPIA)
        return privada

    def tiene_permiso(self):
        prefijo = self.redis.hget(contrato.PERMISOS, self.cuenta)
        if prefijo is None or not self.prefijo.startswith(prefijo):
            if self._avisado:
                return False
            self._avisado = True
            log.error("El escalador (%s) no tiene permiso en la cadena para dar de alta %s*: el organizador tiene que "
                      "correr `python -m claves permiso <su clave> --clave %s --prefijo %s --url <backend>`",
                      self.cuenta, self.prefijo, self.cuenta, self.prefijo)
            return False
        if self._avisado:
            log.info("El escalador ya tiene permiso para %s*", self.prefijo)
            self._avisado = False
        return True

    def preparar(self, cantidad, ahora=None):
        """{nombre: privada} de las VMs 0 a cantidad − 1. Las que no están en la cadena (ni en camino) se dan de alta."""
        guardadas = self.redis.hgetall(CLAVES)
        claves = {f"{self.prefijo}{n}": guardadas.get(f"{self.prefijo}{n}") or contrato.nueva_clave()[0]
                  for n in range(cantidad)}
        nuevas = {nombre: privada for nombre, privada in claves.items() if guardadas.get(nombre) != privada}
        if nuevas:
            self.redis.hset(CLAVES, mapping=nuevas)
        faltan = [(nombre, contrato.clave_publica(privada)) for nombre, privada in claves.items()]
        faltan = [(nombre, cuenta) for nombre, cuenta in faltan
                  if not self.redis.hexists(contrato.CUENTAS, cuenta) and not self.redis.hexists(contrato.ALTAS_EN_CAMINO, cuenta)]
        if faltan and self.tiene_permiso():
            self._mandar([("alta", cuenta, f"minero:{nombre}") for nombre, cuenta in faltan], ahora)
        return claves

    def retirar(self, cantidad):
        """Da de baja (en la cadena) las claves de las VMs desde `cantidad` en adelante (las que se borraron)."""
        sobran = {nombre: privada for nombre, privada in self.redis.hgetall(CLAVES).items()
                  if nombre.startswith(self.prefijo) and nombre[len(self.prefijo):].isdigit()
                  and int(nombre[len(self.prefijo):]) >= cantidad}
        bajas = [("baja", contrato.clave_publica(privada), "la VM se borró") for _, privada in sorted(sobran.items())
                 if self.redis.hexists(contrato.CUENTAS, contrato.clave_publica(privada))]
        if bajas and self.tiene_permiso():
            self._mandar(bajas)
        for nombre in sorted(sobran):
            self.redis.hdel(CLAVES, nombre)
            log.info("%s se borró: su clave se da de baja", nombre)
        return sorted(sobran)

    def _mandar(self, actas, ahora=None):
        """Firma y manda las actas con los nonces que siguen al último aceptado de la clave del escalador."""
        nonce = self.backend.nonce(self.cuenta)
        timestamp = int(time.time() if ahora is None else ahora)
        for tipo, cuenta, datos in actas:
            tx = contrato.acta(self.privada, tipo, nonce + 1, cuenta, datos, timestamp, delegada=True)
            codigo, respuesta = self.backend.mandar(tx)
            if codigo == 202:
                nonce += 1
                log.info("%s de %s (%s) enviada: %s", tipo.capitalize(), datos, cuenta, tx["id"])
            else:
                log.error("No entró el %s de %s: %s %s", tipo, cuenta, codigo, respuesta)


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

    def revisar(self, cantidad):
        """Sin cambio de cantidad: las VMs que siguen sin alta (porque el permiso llegó después) la piden ahora."""
        if self.claves and cantidad:
            self.claves.preparar(cantidad)


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
        sin_bloque=config.entero("ESCALAR_SIN_BLOQUE_SEGUNDOS", 300),
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


def segundos_sin_bloque(redis, ahora):
    """Segundos desde que se armó la caja que se está minando (`caja:armada`, BC-73); 0 si no hay ninguna.

    Si la primera caja todavía no se armó (la anterior ya salió), es 0: no hay nada trabado.
    """
    tuberia = redis.pipeline(transaction=False)
    tuberia.lindex(contrato.CAJAS, 0)
    tuberia.hmget(contrato.CAJA_ARMADA, "caja", "desde")
    primera, (caja, desde) = tuberia.execute()
    if primera is None or caja != primera or not desde:
        return 0.0
    return max(0.0, ahora - float(desde))


def leer_nodos(redis):
    claves = list(redis.scan_iter(match=contrato.NODOS, count=contrato.NODOS_POR_SCAN))
    tuberia = redis.pipeline(transaction=False)
    for clave in claves:
        tuberia.hgetall(clave)
    return {clave.split(":", 1)[1]: nodo for clave, nodo in zip(claves, tuberia.execute())}


def ciclo(redis, decisor, aplicador, ahora=None):
    """Un ciclo: foto, decisión y, si cambió la cantidad, apply. Devuelve la cantidad pedida."""
    ahora = time.time() if ahora is None else ahora
    foto = sacar_foto(leer_nodos(redis), ahora, hay_trabajo(redis), decisor.ajustes, segundos_sin_bloque(redis, ahora))
    antes = decisor.actual
    cantidad = decisor.decidir(foto, ahora)
    if cantidad != antes:
        log.info("VMs: %d → %d (GPU vivas %d, potencia ajena %.0f h/s, trabajo %s, %.0f s sin bloque)",
                 antes, cantidad, foto.gpus, foto.ajena, foto.trabajo, foto.sin_bloque)
        try:
            aplicador.aplicar(cantidad)
        except Exception:
            decisor.actual = antes  # se reintenta en el próximo ciclo
            FALLIDOS.inc()
            raise
        APLICADOS.inc()
    elif cantidad and hasattr(aplicador, "revisar"):
        aplicador.revisar(cantidad)
    VMS.set(cantidad)
    return cantidad


def main():
    servicio = Servicio("escalador", usa_rabbit=False)
    ajustes = cargar_ajustes()
    servicio.log.info("Ajustes: %s", ajustes)
    if config.entero("VMS_ACTIVAS", 0) == 1:
        claves = ClavesVMs(servicio.redis, Backend(config.texto("BACKEND_URL", "http://backend:8080")),
                           ajustes.prefijo_vm, privada=config.texto("ESCALADOR_CLAVE") or None)
        servicio.log.info("Clave del escalador: %s (necesita un permiso del organizador para %s*)", claves.cuenta,
                          ajustes.prefijo_vm)
        aplicador = Tofu(config.texto("TOFU_DIR", "/tofu"), claves=claves)
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
