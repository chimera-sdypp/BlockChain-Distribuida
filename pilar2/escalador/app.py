"""Escalador (P5, BC-29): levanta y borra VMs con mineros CPU cuando no hay GPU.

No está siempre prendido: es un Deployment de 0 o 1 pod que prende KEDA cuando se apilan rangos
o transacciones, o mientras queden mineros CPU vivos (ver k8s/escalador/). Mientras corre, cada
ciclo mira `nodo:*` y el trabajo apilado, decide cuántas VMs hacen falta y, si cambió, se lo pide
a OpenTofu (`tofu apply -var cantidad=N`): el apply es declarativo, así que repetirlo no crea de más.

Cada VM mina con su propia clave (BC-57). Desde el contrato v3 (BC-79) el escalador no escribe `cuentas`: tiene
su propia clave y un **permiso** del organizador (una transacción `permiso-<n>` en la cadena) para dar de alta y de
baja sólo mineros `minero-cpu-*`. Cada alta es una transacción firmada por esa clave que manda al backend; el NCT la
valida en el momento y el gateway deja minar a la VM con el alta «en camino», así puede minar el bloque que la
confirma. No puede aprobar usuarios ni cargar saldo.

Contrato v4 (BC-95): ninguna privada pasa por Redis ni por el state de OpenTofu.
- La del escalador viene de un Secret (`ESCALADOR_CLAVE`); sin ella, con VMs de verdad, no arranca.
- Cada VM genera la suya al arrancar (pilar3/vms/arrancar.sh) y publica sólo la pública en sus guest attributes
  (`minero/publica`), que sólo la VM puede escribir. El escalador la lee con la API de Compute y firma el alta.
- Una baja del organizador dura: si la clave de una VM está vetada, no se vuelve a pedir su alta (se avisa una vez).
"""

import json
import logging
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
# Hasta la v3 acá estaban las privadas de las VMs y la del escalador. Al arrancar se borran (BC-95).
VIEJAS = ("escalador:claves", "escalador:clave")
ATRIBUTO = "minero/publica"  # el guest attribute donde cada VM deja su clave pública


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

    def cadena(self):
        """El hash del génesis (v4): va adelante de lo que firma el escalador."""
        codigo, datos = self._pedir("GET", "/api/cadena")
        if codigo != 200 or not contrato.cadena_valida((datos or {}).get("genesis")):
            raise RuntimeError(f"el backend no dio el génesis: {codigo} {datos}")
        return datos["genesis"]

    def mandar(self, tx):
        return self._pedir("POST", "/api/transacciones", tx)


class ClavesVMs:
    """Las altas y bajas de las VMs minero-cpu-<n> en la cadena, firmadas por la clave del escalador (con el permiso
    del organizador). Las privadas no pasan por acá: cada VM tiene la suya y publica la pública (`publicas(nombre)`
    la lee, o da None si la VM todavía no la publicó)."""

    def __init__(self, redis, backend, publicas, privada, prefijo="minero-cpu-"):
        if not privada:
            raise ValueError("falta ESCALADOR_CLAVE: la clave del escalador viene de un Secret (BC-95), no se genera")
        self.redis, self.backend, self.publicas, self.prefijo = redis, backend, publicas, prefijo
        self.privada = privada
        self.cuenta = contrato.clave_publica(privada)
        self._avisado = False  # el aviso de que falta el permiso sale una vez, no en cada ciclo
        self._vetadas_avisadas = set()  # lo mismo con cada clave que el organizador dio de baja

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

    def _numero(self, nombre):
        resto = nombre[len(self.prefijo):] if nombre.startswith(self.prefijo) else ""
        return int(resto) if resto.isdigit() else None

    def mineros(self):
        """[(nombre, cuenta)] de los mineros de las VMs que hay en la cadena (el caché `cuentas`), por nombre."""
        propios = []
        for cuenta, datos in self.redis.hgetall(contrato.CUENTAS).items():
            datos = json.loads(datos or "{}")
            if datos.get("tipo") == "minero" and self._numero(str(datos.get("nombre", ""))) is not None:
                propios.append((datos["nombre"], cuenta))
        return sorted(propios)

    def _vetada(self, nombre, cuenta):
        if not self.redis.hexists(contrato.VETADAS, cuenta):
            return False
        if cuenta not in self._vetadas_avisadas:
            self._vetadas_avisadas.add(cuenta)
            log.warning("La clave de %s (%s) tiene una baja del organizador: no se vuelve a pedir su alta. La VM no "
                        "mina hasta que el organizador la reactive o la VM se recree (con una clave nueva)",
                        nombre, cuenta)
        return True

    def preparar(self, cantidad, ahora=None):
        """Las VMs 0 a cantidad − 1 que ya publicaron su clave y no están en la cadena (ni en camino) se dan de alta.

        Si con el nombre de una VM hay otra clave en la cadena (la VM spot se recreó con disco nuevo), esa se da de
        baja. Devuelve {nombre: pública} de las que ya publicaron.
        """
        en_cadena = {}
        for nombre, cuenta in self.mineros():
            en_cadena.setdefault(nombre, []).append(cuenta)
        publicas, actas = {}, []
        for n in range(cantidad):
            nombre = f"{self.prefijo}{n}"
            publica = self.publicas(nombre)
            if publica is None:
                continue  # todavía no arrancó
            if not contrato.cuenta_valida(publica):
                log.error("%s publicó algo que no es una clave pública: %r", nombre, publica)
                continue
            publicas[nombre] = publica
            actas += [("baja", vieja, "la VM se recreó con otra clave") for vieja in en_cadena.get(nombre, [])
                      if vieja != publica]
            if self.redis.hexists(contrato.CUENTAS, publica) or self.redis.hexists(contrato.ALTAS_EN_CAMINO, publica):
                continue
            if not self._vetada(nombre, publica):
                actas.append(("alta", publica, f"minero:{nombre}"))
        if actas and self.tiene_permiso():
            self._mandar(actas, ahora)
        return publicas

    def retirar(self, cantidad):
        """Da de baja (en la cadena) las cuentas de las VMs desde `cantidad` en adelante (las que se borraron)."""
        sobran = [(nombre, cuenta) for nombre, cuenta in self.mineros() if self._numero(nombre) >= cantidad]
        if sobran and self.tiene_permiso():
            self._mandar([("baja", cuenta, "la VM se borró") for _, cuenta in sobran])
        for nombre, _ in sobran:
            log.info("%s se borró: su clave se da de baja", nombre)
        return [nombre for nombre, _ in sobran]

    def _mandar(self, actas, ahora=None):
        """Firma y manda las actas con los nonces que siguen al último aceptado de la clave del escalador."""
        nonce, cadena = self.backend.nonce(self.cuenta), self.backend.cadena()
        timestamp = int(time.time() if ahora is None else ahora)
        for tipo, cuenta, datos in actas:
            tx = contrato.acta(self.privada, tipo, nonce + 1, cuenta, datos, timestamp, delegada=True, cadena=cadena)
            codigo, respuesta = self.backend.mandar(tx)
            if codigo == 202:
                nonce += 1
                log.info("%s de %s (%s) enviada: %s", tipo.capitalize(), datos, cuenta, tx["id"])
            else:
                log.error("No entró el %s de %s: %s %s", tipo, cuenta, codigo, respuesta)


class AtributosGCE:
    """La clave pública que dejó cada VM en sus guest attributes (`minero/publica`), con la API de Compute.

    Entra con la cuenta de GCP del pod (Workload Identity: el token sale del servidor de metadata de GKE). Sólo la
    propia VM puede escribir sus guest attributes, así que lo que se lee ahí es la clave de esa VM y no otra.
    """

    METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
    API = "https://compute.googleapis.com/compute/v1/projects/{proyecto}/zones/{zona}/instances/{nombre}/getGuestAttributes"

    def __init__(self, proyecto, zona, abrir=urllib.request.urlopen, timeout=10):
        self.proyecto, self.zona, self.abrir, self.timeout = proyecto, zona, abrir, timeout
        self._token, self._vence = None, 0.0

    def _leer(self, pedido):
        with self.abrir(pedido, timeout=self.timeout) as respuesta:
            return json.loads(respuesta.read())

    def token(self):
        if self._token is None or time.time() > self._vence:
            datos = self._leer(urllib.request.Request(self.METADATA, headers={"Metadata-Flavor": "Google"}))
            self._token, self._vence = datos["access_token"], time.time() + int(datos.get("expires_in", 300)) - 60
        return self._token

    def __call__(self, nombre):
        url = self.API.format(proyecto=self.proyecto, zona=self.zona, nombre=nombre) + f"?variableKey={ATRIBUTO}"
        pedido = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token()}"})
        try:
            return self._leer(pedido).get("variableValue", "").strip() or None
        except urllib.error.HTTPError as error:
            if error.code != 404:  # 404: la VM no existe o todavía no publicó su clave
                log.warning("No se pudo leer la clave de %s: %s %s", nombre, error.code, error.reason)
            return None
        except (urllib.error.URLError, OSError, ValueError) as error:
            log.warning("No se pudo leer la clave de %s: %r", nombre, error)
            return None


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
    """Las VMs de verdad: el módulo de BC-35, con la variable `cantidad` y el output `cantidad`.

    El state y su lock están en el bucket (el backend lo define el módulo), así que un apply a
    medias de un pod anterior no se pisa con éste. Desde BC-95 el state no tiene claves: cada VM genera la suya.
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
        """El apply y después las bajas de las que sobran. Las altas salen en los ciclos siguientes (`revisar`), cuando
        cada VM nueva arrancó y publicó su clave."""
        salida = self._tofu("apply", "-auto-approve", "-input=false", "-var", f"cantidad={cantidad}")
        log.info("tofu apply cantidad=%d: %s", cantidad, (salida.stdout.strip().splitlines() or [""])[-1])
        if self.claves:
            self.claves.retirar(cantidad)

    def revisar(self, cantidad):
        """Sin cambio de cantidad: las VMs que publicaron su clave y siguen sin alta la piden ahora."""
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
    viejas = servicio.redis.delete(*VIEJAS)
    if viejas:
        servicio.log.warning("Se borraron de Redis las claves privadas que guardaba el escalador hasta la v3 (BC-95)")
    if config.entero("VMS_ACTIVAS", 0) == 1:
        if not config.texto("TF_VAR_proyecto"):
            raise ValueError("falta TF_VAR_proyecto: el proyecto de GCP de las VMs (gcp.env)")
        publicas = AtributosGCE(config.texto("TF_VAR_proyecto"), config.texto("TF_VAR_zona", "us-central1-a"))
        claves = ClavesVMs(servicio.redis, Backend(config.texto("BACKEND_URL", "http://backend:8080")), publicas,
                           config.texto("ESCALADOR_CLAVE"), ajustes.prefijo_vm)
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
