"""Gateway de los mineros (BC-43): la única puerta de los mineros al sistema.

Los mineros están fuera del cluster y RabbitMQ y Redis no se publican: el gateway habla HTTP con
ellos y por adentro con RabbitMQ y Redis. Sin estado propio: los préstamos de los rangos están en
Redis, así que cualquier pod atiende a cualquier minero (los textos que guarda en memoria salen de
Redis y no cambian).

Contrato v2: cada pedido va firmado con la clave del minero (BC-57) y sólo atiende a los mineros
aprobados en `cuentas`. A cada uno le arma su propio texto, con su coinbase en la raíz (BC-62), y le
cuenta los shares que manda (BC-63).
"""

import json
import logging
import random
import threading
import time
from collections import OrderedDict

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
import redis as redislib
from prometheus_client import REGISTRY, Counter
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from comun import Rabbit, Servicio, config, contrato
from comun.altas import Altas

log = logging.getLogger("gateway")

ENTREGADAS = Counter("blockchain_tareas_entregadas", "Rangos entregados a los mineros")
VENCIDOS = Counter("blockchain_prestamos_vencidos", "Rangos que volvieron a la cola: su minero dejó de avisar "
                   "(sin_aviso) o se le pasó el plazo (plazo, BC-74)", ["motivo"])
SHARES = Counter("blockchain_shares", "Shares que mandaron los mineros", ["resultado"])  # aceptado o descartado
RECHAZOS = Counter("blockchain_pedidos_rechazados", "Pedidos de mineros rechazados", ["motivo"])  # firma, no_aprobado o suspendido
SUSPENSIONES = Counter("blockchain_suspensiones", "Mineros suspendidos por contestar sin_solucion sin buscar (BC-75)",
                       ["motivo"])  # estadistica o reverificacion
REVERIFICACIONES = Counter("blockchain_reverificaciones", "Rangos sin solución reverificados con otro minero (BC-75)",
                           ["resultado"])  # pedida, mentira, sin_solucion, descartada
POCOS_SHARES = Counter("blockchain_pocos_shares", "Préstamos seguidos con muchos menos shares que los esperados (aviso, BC-75)")
TEXTOS_EN_MEMORIA, TAREAS_EN_MEMORIA = 512, 16

TIPOS = ("gpu", "cpu")
MINERO_VIVO = 15  # segundos: el keep-alive es cada 5 s; tres perdidos y se lo da por caído
# BC-74: el plazo de un préstamo es PLAZO_FACTOR veces lo que debería tardar el rango según la potencia del
# minero, más PLAZO_MARGEN por la red. Sin medición, como mucho PLAZO_SIN_MEDICION; nunca menos de PLAZO_MINIMO.
PLAZO_FACTOR, PLAZO_MARGEN, PLAZO_SIN_MEDICION, PLAZO_MINIMO = 3, 10, 120, 30


PRESTAMO = "prestamo:{}"  # prestamo:<tarea>:<rango> → {mensaje, minero, desde, plazo, shares}


class YaTienePrestamo(Exception):
    """Otro pedido del mismo minero se llevó un rango en el medio: éste vuelve a la cola (BC-74)."""


# Presta un rango, uno por minero (BC-74): si el minero ya tiene uno (prestamo_de:<minero>), no hace nada y
# devuelve 0; el rango vuelve a la cola. Si no, anota el préstamo, su vencimiento, el rango en el historial
# (BC-59) y el índice del minero. KEYS: prestamo:<miembro>, prestamos, historial:<minero>, prestamo_de:<minero>.
# ARGV: miembro, mensaje, minero, vence, desde, plazo, texto_de ('' o la cuenta del minero reverificado, BC-75).
LUA_PRESTAR = """
if not redis.call('SET', KEYS[4], ARGV[1], 'NX') then return 0 end
redis.call('HSET', KEYS[1], 'mensaje', ARGV[2], 'minero', ARGV[3], 'desde', ARGV[5], 'plazo', ARGV[6])
if ARGV[7] ~= '' then redis.call('HSET', KEYS[1], 'texto_de', ARGV[7]) end
redis.call('ZADD', KEYS[2], ARGV[4], ARGV[1])
redis.call('HINCRBY', KEYS[3], 'rangos', 1)
return 1
"""

# Libera un préstamo y, si el índice del minero apunta a él, también el índice. KEYS: prestamos,
# prestamo:<miembro>. ARGV: miembro.
LUA_LIBERAR = """
local minero = redis.call('HGET', KEYS[2], 'minero')
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('DEL', KEYS[2])
if minero and redis.call('GET', 'prestamo_de:' .. minero) == ARGV[1] then redis.call('DEL', 'prestamo_de:' .. minero) end
return 1
"""

# Reclama un préstamo vencido: si sigue vencido, le corre el vencimiento y devuelve 1. Así lo
# vuelve a publicar un solo pod, y si ese pod muere antes de publicarlo, vuelve a vencer y se
# reintenta (con un ZREM, el rango se perdería). KEYS: prestamos. ARGV: miembro, ahora, nuevo.
LUA_RECLAMAR = """
local vence = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not vence or tonumber(vence) > tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
return 1
"""

# Suma los shares de un rango prestado (BC-63). Atómico: sólo si la tarea sigue en curso (cuando el
# NCT paga la época, ya no entra ninguno) y si el préstamo es de este minero. Descarta los repetidos
# y no pasa de `tope` por préstamo.
# KEYS: tarea:<id>, tarea:<id>:shares, shares:<época>, historial:<minero>, prestamo:<tarea>:<rango>.
# ARGV: minero, tope, nonce… Devuelve cuántos sumó.
LUA_SHARES = """
if redis.call('HGET', KEYS[1], 'estado') ~= 'en_curso' then return 0 end
if redis.call('HGET', KEYS[5], 'minero') ~= ARGV[1] then return 0 end
local libres = tonumber(ARGV[2]) - tonumber(redis.call('HGET', KEYS[5], 'shares') or '0')
local nuevos = 0
for i = 3, #ARGV do
  if nuevos >= libres then break end
  if redis.call('SADD', KEYS[2], ARGV[1] .. ':' .. ARGV[i]) == 1 then nuevos = nuevos + 1 end
end
if nuevos > 0 then
  redis.call('EXPIRE', KEYS[2], 86400)
  redis.call('HINCRBY', KEYS[5], 'shares', nuevos)
  redis.call('ZINCRBY', KEYS[3], nuevos, ARGV[1])
  redis.call('HINCRBY', KEYS[4], 'shares', nuevos)
end
return nuevos
"""

# Historial de un minero (BC-54), en cada /hello y /keepalive. Suma a `segundos_conectado` el tiempo desde
# el aviso anterior, sólo si fue hace menos de `vivo` (si no, estuvo desconectado y ese hueco no cuenta).
# Anota el aviso, el hashrate y, la primera vez, el alta. Devuelve 1 si el minero se (re)conecta.
# KEYS: historial:<minero>, mineros:conectados. ARGV: minero, ahora, vivo, hashes/s ('' = no cambia), alta ('' = no).
LUA_AVISO = """
local ahora, ult = tonumber(ARGV[2]), tonumber(redis.call('HGET', KEYS[1], 'ultimo_aviso'))
local volvio = redis.call('HGET', KEYS[1], 'conectado') ~= '1' and 1 or 0
if ult and ahora >= ult and ahora - ult <= tonumber(ARGV[3]) then
  if ahora > ult then redis.call('HINCRBY', KEYS[1], 'segundos_conectado', ahora - ult) end
else
  volvio = 1
end
if not ult or ahora > ult then redis.call('HSET', KEYS[1], 'ultimo_aviso', ahora) end
redis.call('HSET', KEYS[1], 'conectado', '1')
redis.call('SADD', KEYS[2], ARGV[1])
if ARGV[4] ~= '' then redis.call('HSET', KEYS[1], 'hashes_por_segundo', ARGV[4]) end
if ARGV[5] ~= '' then redis.call('HSETNX', KEYS[1], 'alta', ARGV[5]) end
return volvio
"""

# Da por desconectado a un minero que dejó de avisar: lo hace un solo pod, y devuelve los segundos que lleva
# conectado en total (o -1 si sigue vivo o ya lo había dado otro pod).
# KEYS: historial:<minero>, mineros:conectados. ARGV: minero, ahora, vivo.
LUA_DESCONECTAR = """
if redis.call('HGET', KEYS[1], 'conectado') ~= '1' then
  redis.call('SREM', KEYS[2], ARGV[1])
  return -1
end
local ult = tonumber(redis.call('HGET', KEYS[1], 'ultimo_aviso') or '0')
if tonumber(ARGV[2]) - ult <= tonumber(ARGV[3]) then return -1 end
redis.call('HSET', KEYS[1], 'conectado', '0')
redis.call('SREM', KEYS[2], ARGV[1])
return tonumber(redis.call('HGET', KEYS[1], 'segundos_conectado') or '0')
"""

# Renueva un préstamo sólo si es de este minero: un minero lento no le estira el préstamo a otro.
# KEYS: prestamos, prestamo:<miembro>. ARGV: miembro, minero, vencimiento nuevo.
LUA_RENOVAR = """
if redis.call('HGET', KEYS[2], 'minero') ~= ARGV[2] then return 0 end
return redis.call('ZADD', KEYS[1], 'XX', 'CH', ARGV[3], ARGV[1])
"""


def es_entero(valor, minimo=0):
    return isinstance(valor, int) and not isinstance(valor, bool) and valor >= minimo


def shares_validos(valor):
    return valor is None or (isinstance(valor, list) and len(valor) <= contrato.SHARES_POR_RANGO
                             and all(es_entero(n) for n in valor))


def resultado_valido(datos):
    if not isinstance(datos, dict) or not isinstance(datos.get("tarea"), str) or not datos["tarea"] \
            or not es_entero(datos.get("rango")) or not contrato.cuenta_valida(datos.get("minero")) \
            or not shares_validos(datos.get("shares")):
        return False
    if datos.get("sin_solucion") is True:
        return True
    return es_entero(datos.get("nonce")) and isinstance(datos.get("hash"), str)


def es_numero(valor, minimo=0):
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and valor >= minimo


class Gateway:
    def __init__(self, redis, rabbit, cola_rangos="rangos", cola_resultados="resultados",
                 lease=30, minero_vivo=MINERO_VIVO, umbral=contrato.UMBRAL_SIN_SOLUCION,
                 muestreo=contrato.MUESTREO_SIN_SOLUCION, azar=random.random):
        self.redis, self.rabbit = redis, rabbit
        self.cola_rangos, self.cola_resultados = cola_rangos, cola_resultados
        self.lease, self.minero_vivo = lease, minero_vivo
        self.umbral, self.muestreo, self.azar = umbral, muestreo, azar  # BC-75
        self._renovar = redis.register_script(LUA_RENOVAR)
        self._reclamar = redis.register_script(LUA_RECLAMAR)
        self._sumar_shares = redis.register_script(LUA_SHARES)
        self._aviso = redis.register_script(LUA_AVISO)
        self._desconectar = redis.register_script(LUA_DESCONECTAR)
        self._prestar_lua = redis.register_script(LUA_PRESTAR)
        self._liberar_lua = redis.register_script(LUA_LIBERAR)
        self.altas = Altas(redis)
        self._lock = threading.Lock()
        self._tareas, self._textos = OrderedDict(), OrderedDict()
        COLECTOR.gateway = self

    # ── Quién pide: la firma y la aprobación (BC-57) ──

    def autenticar(self, encabezados, metodo, ruta, cuerpo, ahora=None):
        """(minero, None) si el pedido está firmado y en hora por un minero aprobado; si no, (None, (código, motivo))."""
        minero, motivo = contrato.verificar_pedido(encabezados, metodo, ruta, cuerpo, ahora=ahora)
        if motivo:
            RECHAZOS.labels("firma").inc()
            log.warning("Pedido %s %s rechazado: %s", metodo, ruta, motivo)
            return None, (401, motivo)
        datos = self.redis.hget(contrato.CUENTAS, minero)
        if not datos or json.loads(datos).get("tipo") != "minero":
            RECHAZOS.labels("no_aprobado").inc()
            log.warning("Pedido %s %s de %s, que no es un minero aprobado", metodo, ruta, minero)
            self.pedir_alta(minero, cuerpo)
            return None, (403, f"la cuenta {minero} no es un minero aprobado: el organizador la tiene que aprobar")
        suspension = self.altas.suspendido(minero)
        if suspension:
            RECHAZOS.labels("suspendido").inc()
            return None, (403, f"la cuenta {minero} está suspendida ({suspension['motivo']}): "
                               "el organizador decide si la reactiva o le da de baja")
        return minero, None

    def suspender(self, minero, motivo, evidencia):
        """BC-75: el minero deja de recibir rangos hasta que el organizador decida. Su préstamo vivo vuelve a la cola."""
        if not self.altas.suspender(minero, motivo, evidencia):
            return False
        SUSPENSIONES.labels(motivo).inc()
        alias = self.redis.hget(contrato.NODO.format(minero), "alias") or minero[:12]
        log.warning("Minero suspendido: %s (%s) por %s: %s", alias, minero, motivo, json.dumps(evidencia))
        miembro = self.redis.get(contrato.PRESTAMO_DE.format(minero))
        if miembro:
            prestamo = self.redis.hgetall(PRESTAMO.format(miembro))
            if prestamo.get("mensaje"):
                mensaje = json.loads(prestamo["mensaje"])
                if self.tarea_en_curso(mensaje["tarea"]):
                    self.rabbit.publicar(self.cola_rangos, mensaje)
            self._liberar(miembro)
        return True

    def pedir_alta(self, minero, cuerpo):
        """Un minero firmado y sin aprobar queda en `altas:pendientes` (BC-58): la firma ya demuestra que
        tiene la privada. El alias sale del /hello (`alias`); si no viene, el principio de la cuenta.

        Si el pedido lo anotó antes un keep-alive (sin alias), el /hello le pone el alias (BC-71).
        """
        try:
            alias = json.loads(cuerpo).get("alias") if cuerpo else None
        except (ValueError, AttributeError):
            alias = None
        con_alias = contrato.nombre_valido(alias)
        resultado = self.altas.pedir(minero, "minero", alias if con_alias else minero[:12], renombrar=con_alias)
        if resultado in ("nueva", "renombrada"):
            log.info("Pedido de alta de %s (minero): %s", alias if con_alias else minero[:12], minero)

    # ── Registro ──

    def hello(self, minero, datos):
        """Devuelve (código, cuerpo). Un minero sin bench recibe la tarea modelo."""
        if not isinstance(datos, dict) or datos.get("minero") != minero \
                or datos.get("tipo") not in TIPOS or not isinstance(datos.get("placa"), str):
            return 400, {"error": "hace falta {minero (el de X-Minero), tipo: gpu | cpu, placa}"}
        nodo = contrato.NODO.format(minero)
        cuenta = json.loads(self.redis.hget(contrato.CUENTAS, minero) or "{}")
        alias = cuenta.get("nombre") or minero[:12]
        ahora = int(time.time())
        self.redis.hset(nodo, mapping={"tipo": datos["tipo"], "placa": datos["placa"][:100], "alias": alias,
                                       "ultimo_aviso": ahora})
        self.avisar(minero, alias, ahora, alta=cuenta.get("alta") or ahora)
        if self.redis.hexists(nodo, "bench"):
            return 200, {"registrado": True}
        log.info("Minero nuevo %s (%s, %s, %s): le toca la tarea modelo", alias, datos["tipo"], datos["placa"], minero)
        return 200, contrato.BENCH

    def bench(self, minero, datos):
        if not isinstance(datos, dict) or datos.get("minero") != minero \
                or not es_numero(datos.get("hashes_por_segundo"), minimo=1):
            return 400, {"error": "hace falta {minero, segundos, hashes_por_segundo > 0}"}
        self.redis.hset(contrato.NODO.format(minero), "bench", int(datos["hashes_por_segundo"]))
        log.info("Bench de %s: %d hashes/s", minero, datos["hashes_por_segundo"])
        return 204, None

    # ── El texto de cada minero (BC-62) ──

    def texto(self, tarea, minero):
        """El texto que mina `minero` en `tarea`, con su coinbase en la raíz; None si la tarea no está completa.

        Se guarda en memoria: la tarea no cambia (al rearmarla, el NCT le pone otro id), así que no hace
        falta recalcular la raíz en cada rango ni en cada share.
        """
        with self._lock:
            texto = self._textos.get((tarea, minero))
        if texto is not None:
            return texto
        datos = self._tarea(tarea)
        if datos is None:
            return None
        texto = contrato.texto_para(datos, minero)
        with self._lock:
            self._textos[(tarea, minero)] = texto
            while len(self._textos) > TEXTOS_EN_MEMORIA:
                self._textos.popitem(last=False)
        return texto

    def _tarea(self, tarea):
        with self._lock:
            datos = self._tareas.get(tarea)
        if datos is not None:
            return datos
        try:
            datos = contrato.tarea_guardada(self.redis.hgetall(contrato.TAREA.format(tarea)))
        except (KeyError, ValueError):
            return None  # el NCT o el TrP todavía no la terminaron de escribir
        with self._lock:
            self._tareas[tarea] = datos
            while len(self._tareas) > TAREAS_EN_MEMORIA:
                self._tareas.popitem(last=False)
        return datos

    # ── Rangos: entregar y devolver ──

    def siguiente(self, minero, datos=None):
        """GET /tasks/next: el próximo rango de una tarea en curso, prestado a `minero`, con su texto.

        Uno a la vez por minero (BC-74): si ya tiene un préstamo vivo, recibe ese mismo (por si se reinició).
        Primero se anota el préstamo en Redis y recién después se hace el ack: un rango nunca queda sin dueño
        ni en la cola. Los de tareas cerradas se descartan (ack sin entregar).
        """
        try:
            for _ in range(3):
                vigente = self._vigente(minero)
                if vigente is not None:
                    return 200, vigente
                try:
                    while True:
                        rango = self.rabbit.tomar(self.cola_rangos, lambda mensaje: self._prestar(mensaje, minero))
                        if rango is None:
                            return 204, None
                        if rango is not False:
                            ENTREGADAS.inc()
                            return 200, rango
                except YaTienePrestamo:
                    continue  # otro pedido suyo se llevó un rango en el medio: se le devuelve ése
            return 204, None
        except redislib.RedisError as error:
            log.error("Redis no respondió al prestar un rango (vuelve a la cola): %r", error)
            return 503, {"error": "Redis no disponible"}

    def _vigente(self, minero):
        """El préstamo vivo de `minero`, con su texto, o None. Si su tarea se cerró o se le pasó el plazo, lo
        suelta (y en el segundo caso el rango vuelve a la cola)."""
        miembro = self.redis.get(contrato.PRESTAMO_DE.format(minero))
        if miembro is None:
            return None
        prestamo = self.redis.hgetall(PRESTAMO.format(miembro))
        if prestamo.get("minero") != minero or "mensaje" not in prestamo:
            self._liberar(miembro)  # un índice viejo
            return None
        mensaje = json.loads(prestamo["mensaje"])
        if not self.tarea_en_curso(mensaje["tarea"]):
            self._liberar(miembro)
            return None
        if self._se_paso_el_plazo(prestamo):
            self._cortar_por_plazo(minero, miembro, prestamo, mensaje)
            return None
        texto = self.texto(mensaje["tarea"], prestamo.get("texto_de") or minero)
        limpio = {k: v for k, v in mensaje.items() if k not in ("reverifica", "rango_original")}
        return {**limpio, "bloque": texto} if texto is not None else None

    def plazo(self, minero, mensaje):
        """Segundos que tiene `minero` para recorrer el rango (BC-74): PLAZO_FACTOR veces lo que debería tardar
        con lo que midió el gateway, más el margen. Sin medición, según su bench y como mucho PLAZO_SIN_MEDICION."""
        nodo = self.redis.hgetall(contrato.NODO.format(minero))
        tamano = mensaje["hasta"] - mensaje["desde"] + 1
        estado = nodo.get("medido_estado")
        try:
            potencia = float(nodo.get("medido") or 0) if estado == "medido" else float(nodo.get("bench") or 0)
        except ValueError:
            potencia = 0.0
        if potencia <= 0:
            return PLAZO_MINIMO if estado == "medido" else PLAZO_SIN_MEDICION
        segundos = max(PLAZO_MINIMO, PLAZO_FACTOR * tamano / potencia + PLAZO_MARGEN)
        if estado not in ("medido", "cota"):
            segundos = min(segundos, PLAZO_SIN_MEDICION)
        return segundos

    def _prestar(self, mensaje, minero):
        """Dentro de `tomar`: si devuelve, se hace el ack; si lanza (Redis caído, o el minero ya tiene otro
        préstamo), el rango vuelve a la cola."""
        if not self.tarea_en_curso(mensaje["tarea"]):
            log.info("Descarto el rango %s de la tarea %s, que ya se cerró", mensaje["rango"], mensaje["tarea"])
            if mensaje.get("reverifica"):
                REVERIFICACIONES.labels("descartada").inc()
            return False
        texto_de = mensaje.get("reverifica") or ""
        if texto_de == minero:
            # No se reverifica a sí mismo. Si es el único minero, la reverificación se pierde (se descarta).
            REVERIFICACIONES.labels("descartada").inc()
            return False
        texto = self.texto(mensaje["tarea"], texto_de or minero)
        if texto is None:
            log.error("La tarea %s no tiene todo lo que hace falta para armar el texto: descarto el rango %s",
                      mensaje["tarea"], mensaje["rango"])
            return False
        miembro = f"{mensaje['tarea']}:{mensaje['rango']}"
        ahora = time.time()
        if not self._prestar_lua(
                keys=[PRESTAMO.format(miembro), contrato.PRESTAMOS, contrato.HISTORIAL.format(minero),
                      contrato.PRESTAMO_DE.format(minero)],
                args=[miembro, json.dumps(mensaje), minero, ahora + self.lease, ahora,
                      ahora + self.plazo(minero, mensaje), texto_de]):
            raise YaTienePrestamo(minero)
        log.info("Rango %s prestado a %s%s", miembro, minero, f" (reverifica a {texto_de})" if texto_de else "")
        return {k: v for k, v in mensaje.items() if k not in ("reverifica", "rango_original")} | {"bloque": texto}

    # ── Lo que mide el gateway (BC-74) ──

    @staticmethod
    def _se_paso_el_plazo(prestamo, ahora=None):
        try:
            return float(prestamo["plazo"]) < (time.time() if ahora is None else ahora)
        except (KeyError, ValueError):
            return False

    def _cortar_por_plazo(self, minero, miembro, prestamo, mensaje):
        """Se le pasó el plazo: el rango vuelve a la cola, se mide lo que hizo y se suelta. No es una sanción:
        los shares que mandó quedan."""
        if self.tarea_en_curso(mensaje["tarea"]):
            self.rabbit.publicar(self.cola_rangos, mensaje)
            VENCIDOS.labels("plazo").inc()
        self.medir(minero, prestamo, mensaje)
        self._liberar(miembro)
        log.warning("A %s se le pasó el plazo del rango %s: vuelve a la cola", minero, miembro)

    def medir(self, minero, prestamo, mensaje, ahora=None):
        """Anota un préstamo terminado en la medición de `minero` y recalcula lo que mide (BC-74).

        El trabajo que se puede probar son los shares: cada uno cuesta, en promedio, 16^(p-1) hashes (como
        estiman el hashrate los pools: shares × dificultad / tiempo). Con el tope de 64 por préstamo, un minero
        rápido llega al tope y sólo se sabe que es al menos eso («cota»). Si con lo que dice su bench tendría
        que haber mandado bastantes shares y no los mandó, lo medido manda («medido», aunque dé 0).
        """
        prefijo = mensaje.get("prefijo", "")
        if len(prefijo) < 2 or "desde" not in prestamo:
            return None  # con prefijo de 1 no hay shares
        ahora = time.time() if ahora is None else ahora
        segundos = max(0.05, ahora - float(prestamo["desde"]))
        dificultad = 16 ** (len(prefijo) - 1)
        shares = int(prestamo.get("shares") or 0)
        nodo = contrato.NODO.format(minero)
        bench = float(self.redis.hget(nodo, "bench") or 0)
        registro = {"trabajo": shares * dificultad, "segundos": round(segundos, 3), "shares": shares,
                    "esperados": min(contrato.SHARES_POR_RANGO, segundos * bench / dificultad),
                    "tope": int(shares >= contrato.SHARES_POR_RANGO)}
        clave = contrato.MEDICION.format(minero)
        tuberia = self.redis.pipeline()
        tuberia.rpush(clave, json.dumps(registro))
        tuberia.ltrim(clave, -contrato.MEDICION_PRESTAMOS, -1)
        tuberia.lrange(clave, 0, -1)
        registros = [json.loads(r) for r in tuberia.execute()[-1]]
        trabajo = sum(r["trabajo"] for r in registros)
        total = sum(r["segundos"] for r in registros)
        medido = trabajo / total if total > 0 else 0.0
        if 2 * sum(r["tope"] for r in registros) >= len(registros):
            estado = "cota"
        elif sum(r["esperados"] for r in registros) >= contrato.MEDICION_SHARES_MINIMOS:
            estado = "medido"
        else:
            estado = "sin_datos"
        self.redis.hset(nodo, mapping={"medido": int(medido), "medido_estado": estado})
        ultimos = registros[-3:]
        if len(ultimos) == 3 and all(r["esperados"] >= 8 and r["shares"] < r["esperados"] / 4 for r in ultimos):
            # BC-75: es una señal, no una prueba (un bench que dice de más da lo mismo): sólo un aviso.
            POCOS_SHARES.inc()
            log.warning("%s mandó muchos menos shares que los esperados en sus últimos 3 préstamos (%s de %s)",
                        minero, [r["shares"] for r in ultimos], [round(r["esperados"]) for r in ultimos])
        return medido, estado

    # ── Shares (BC-63) ──

    def aceptar_shares(self, minero, tarea, rango, nonces):
        """Verifica y suma los shares de un rango que `minero` tiene prestado. Devuelve cuántos sumó.

        Cada share es un MD5: un nonce del rango cuyo hash, sobre el texto de este minero, empieza con
        el prefijo menos un carácter. Con prefijo de 1 no hay shares.
        """
        if not nonces:
            return 0
        miembro = f"{tarea}:{rango}"
        prestamo = self.redis.hgetall(PRESTAMO.format(miembro))
        if prestamo.get("minero") != minero:
            SHARES.labels("descartado").inc(len(nonces))
            return 0
        mensaje = json.loads(prestamo["mensaje"])
        corto, texto = contrato.prefijo_share(mensaje["prefijo"]), self.texto(tarea, prestamo.get("texto_de") or minero)
        if corto is None or texto is None:
            SHARES.labels("descartado").inc(len(nonces))
            return 0
        validos = sorted({n for n in nonces if mensaje["desde"] <= n <= mensaje["hasta"]
                          and contrato.cumple(contrato.hash_bloque(texto, n), corto)})
        sumados = 0
        if validos:
            altura = self._tarea(tarea)["altura"]
            sumados = self._sumar_shares(
                keys=[contrato.TAREA.format(tarea), contrato.SHARES_TAREA.format(tarea),
                      contrato.SHARES.format(contrato.epoca(altura)), contrato.HISTORIAL.format(minero),
                      PRESTAMO.format(miembro)],
                args=[minero, contrato.SHARES_POR_RANGO, *validos])
        SHARES.labels("aceptado").inc(sumados)
        SHARES.labels("descartado").inc(len(nonces) - sumados)
        return sumados

    def tarea_en_curso(self, tarea):
        return self.redis.hget(contrato.TAREA.format(tarea), "estado") == "en_curso"

    def resultado(self, minero, datos):
        """POST /results: suma los shares, lo publica en `resultados` y recién después libera el préstamo."""
        if not resultado_valido(datos) or datos["minero"] != minero:
            return 400, {"error": "hace falta {tarea, rango, minero (el de X-Minero)} y {nonce, hash} o "
                                  "{sin_solucion: true}, con shares opcional (lista de nonces)"}
        shares = self.aceptar_shares(minero, datos["tarea"], datos["rango"], datos.get("shares"))
        miembro = f"{datos['tarea']}:{datos['rango']}"
        prestamo = self.redis.hgetall(PRESTAMO.format(miembro))
        propio = prestamo.get("minero") == minero and "mensaje" in prestamo
        if propio and prestamo.get("texto_de"):
            self._reverificado(minero, prestamo, datos)  # BC-75: no va al TrP
            self._terminar(minero, miembro)
            return 202, {"shares": shares}
        if propio and datos.get("sin_solucion") and self._revisar_sin_solucion(minero, prestamo):
            self._terminar(minero, miembro)  # mintió: el rango vuelve a la cola en vez de ir al TrP
            return 202, {"shares": shares}
        mensaje = {k: v for k, v in datos.items() if k != "shares"}
        try:
            self.rabbit.publicar(self.cola_resultados, mensaje)
        except Exception as error:
            log.error("No se pudo publicar el resultado de %s:%s: %r", datos["tarea"], datos["rango"], error)
            return 503, {"error": "RabbitMQ no disponible"}
        self._terminar(minero, miembro)
        return 202, {"shares": shares}

    # ── BC-75: los que contestan `sin_solucion` sin buscar ──

    def _revisar_sin_solucion(self, minero, prestamo):
        """Un `sin_solucion` de un rango propio. Devuelve True si se lo suspendió (y el rango volvió a la cola).

        - Si la probabilidad de que el rango no tenga solución es menor que el umbral, es una mentira: se suspende.
        - Si no, con probabilidad `muestreo` y mientras la tarea siga en curso, se reverifica con otro minero.
        """
        mensaje = json.loads(prestamo["mensaje"])
        tamano = mensaje["hasta"] - mensaje["desde"] + 1
        probabilidad = contrato.prob_sin_solucion(tamano, mensaje["prefijo"])
        if probabilidad < self.umbral:
            evidencia = {"tarea": mensaje["tarea"], "rango": mensaje["rango"], "nonces": tamano,
                         "prefijo": mensaje["prefijo"], "probabilidad": probabilidad}
            self.suspender(minero, "estadistica", evidencia)  # su préstamo (este rango) vuelve a la cola
            return True
        if self.muestreo > 0 and self.azar() < self.muestreo and self.tarea_en_curso(mensaje["tarea"]):
            self.rabbit.publicar(self.cola_rangos, {**mensaje, "rango": mensaje["rango"] + contrato.REVERIFICACION,
                                                    "rango_original": mensaje["rango"], "reverifica": minero})
            REVERIFICACIONES.labels("pedida").inc()
            log.info("Reverifico el rango %s:%s que %s devolvió sin solución (probabilidad %.2g)",
                     mensaje["tarea"], mensaje["rango"], minero, probabilidad)
        return False

    def _reverificado(self, verificador, prestamo, datos):
        """Lo que devolvió quien reverificaba: si encontró un nonce con el texto del otro, el otro mintió.

        El bloque no se usa: tiene la coinbase del que mintió, y si entrara cobraría la recompensa. Si dice
        `sin_solucion`, no se concluye nada (puede que de verdad no haya)."""
        mensaje, original = json.loads(prestamo["mensaje"]), prestamo["texto_de"]
        if datos.get("sin_solucion"):
            REVERIFICACIONES.labels("sin_solucion").inc()
            return
        texto = self.texto(mensaje["tarea"], original)
        nonce = datos.get("nonce")
        if texto is None or not mensaje["desde"] <= nonce <= mensaje["hasta"]:
            return
        hash_ = contrato.hash_bloque(texto, nonce)
        if hash_ != datos.get("hash") or not contrato.cumple(hash_, mensaje["prefijo"]):
            log.warning("La reverificación de %s mandó un nonce que no sirve: se ignora", verificador)
            return
        REVERIFICACIONES.labels("mentira").inc()
        self.suspender(original, "reverificacion", {
            "tarea": mensaje["tarea"], "rango": mensaje.get("rango_original"), "nonce": nonce, "hash": hash_,
            "verificador": verificador})

    def _terminar(self, minero, miembro):
        """Un préstamo que terminó (resultado o «cortá»): se mide y se suelta."""
        prestamo = self.redis.hgetall(PRESTAMO.format(miembro))
        if prestamo.get("minero") == minero and "mensaje" in prestamo:
            try:
                self.medir(minero, prestamo, json.loads(prestamo["mensaje"]))
            except (redislib.RedisError, ValueError, KeyError):
                log.exception("No se pudo medir el préstamo %s de %s", miembro, minero)
        self._liberar(miembro)

    def _liberar(self, miembro):
        try:
            self._liberar_lua(keys=[contrato.PRESTAMOS, PRESTAMO.format(miembro)], args=[miembro])
        except redislib.RedisError:
            log.exception("No se pudo liberar el préstamo %s (vencerá y el TrP tolera el repetido)", miembro)

    # ── Keep-alive ──

    def keepalive(self, minero, datos):
        """POST /keepalive: el minero sigue vivo; suma los shares que trae, renueva su préstamo o le dice que corte.

        Si su `nodo:<id>` no tiene bench (la cadena se reinició, o se cortó antes de mandarlo), la respuesta
        lleva `presentarse: true` para que repita el /hello y el bench (BC-71).
        """
        rango = datos.get("rango") if isinstance(datos, dict) else None
        if not isinstance(datos, dict) or datos.get("minero") != minero \
                or not es_numero(datos.get("hashes_por_segundo")) or not shares_validos(datos.get("shares")) \
                or not (rango is None or (isinstance(rango, dict) and isinstance(rango.get("tarea"), str)
                                          and es_entero(rango.get("rango")))):
            return 400, {"error": "hace falta {minero, hashes_por_segundo, rango: {tarea, rango} | null, shares?}"}
        nodo = contrato.NODO.format(minero)
        ahora, hps = int(time.time()), int(datos["hashes_por_segundo"])
        tuberia = self.redis.pipeline(transaction=False)
        tuberia.hset(nodo, mapping={"hashes_por_segundo": hps, "ultimo_aviso": ahora})
        tuberia.hexists(nodo, "bench")
        tuberia.hget(nodo, "alias")
        _, con_bench, alias = tuberia.execute()
        respuesta = {} if con_bench else {"presentarse": True}
        self.avisar(minero, alias or minero[:12], ahora, hps=hps)
        if rango is None:
            return 200, {"cortar": False, **respuesta}
        if "shares" in datos:
            respuesta["shares"] = self.aceptar_shares(minero, rango["tarea"], rango["rango"], datos["shares"])
        miembro = f"{rango['tarea']}:{rango['rango']}"
        if not self.tarea_en_curso(rango["tarea"]):
            # El minero deja el rango sin mandar resultado: el préstamo se mide y se borra acá.
            self._terminar(minero, miembro)
            log.info("A %s: cortá el rango %s, su tarea ya se cerró", minero, miembro)
            return 200, {"cortar": True, **respuesta}
        prestamo = self.redis.hgetall(PRESTAMO.format(miembro))
        if prestamo.get("minero") == minero and self._se_paso_el_plazo(prestamo):
            self._cortar_por_plazo(minero, miembro, prestamo, json.loads(prestamo["mensaje"]))
            return 200, {"cortar": True, **respuesta}
        self._renovar(keys=[contrato.PRESTAMOS, PRESTAMO.format(miembro)],
                      args=[miembro, minero, time.time() + self.lease])
        return 200, {"cortar": False, **respuesta}

    # ── Historial: conexiones y desconexiones (BC-54) ──

    def avisar(self, minero, alias, ahora, hps="", alta=""):
        """Anota el aviso en `historial:<minero>` (tiempo conectado, hashrate y alta) y deja en el log quién entra."""
        try:
            if self._aviso(keys=[contrato.HISTORIAL.format(minero), contrato.CONECTADOS],
                           args=[minero, ahora, self.minero_vivo, hps, alta]):
                log.info("Minero conectado: %s (%s)", alias, minero)
        except redislib.RedisError:
            log.exception("No se pudo anotar el aviso de %s en su historial", alias)

    def revisar_desconectados(self, ahora=None):
        """Da por desconectados a los mineros que dejaron de avisar y lo deja en el log. Devuelve cuántos."""
        ahora = int(ahora if ahora is not None else time.time())
        caidos = 0
        for minero in self.redis.smembers(contrato.CONECTADOS):
            segundos = self._desconectar(keys=[contrato.HISTORIAL.format(minero), contrato.CONECTADOS],
                                         args=[minero, ahora, self.minero_vivo])
            if segundos >= 0:
                caidos += 1
                alias = self.redis.hget(contrato.NODO.format(minero), "alias") or minero[:12]
                log.warning("Minero desconectado: %s (%s) dejó de avisar; lleva %.1f h conectado en total",
                            alias, minero, segundos / 3600)
        return caidos

    # ── Préstamos vencidos ──

    def revisar_vencidos(self):
        """Vuelve a publicar en `rangos` los préstamos vencidos (los que reclama este pod)."""
        ahora = time.time()
        vencidos = self.redis.zrangebyscore(contrato.PRESTAMOS, "-inf", ahora, start=0, num=100)
        devueltos = 0
        for miembro in vencidos:
            if not self._reclamar(keys=[contrato.PRESTAMOS], args=[miembro, ahora, ahora + self.lease]):
                continue  # lo reclamó otro pod
            try:
                prestamo = self.redis.hgetall(PRESTAMO.format(miembro))
                mensaje = json.loads(prestamo["mensaje"]) if prestamo else None
                if mensaje and self.tarea_en_curso(mensaje["tarea"]):
                    self.rabbit.publicar(self.cola_rangos, mensaje)
                    VENCIDOS.labels("sin_aviso").inc()
                    devueltos += 1
                    log.warning("El préstamo %s de %s venció: el rango vuelve a la cola", miembro, prestamo["minero"])
                self._liberar(miembro)
            except Exception:
                log.exception("No se pudo devolver el préstamo %s: se reintenta cuando vuelva a vencer", miembro)
        return devueltos

    # ── Métricas ──

    def mineros_vivos(self):
        """{tipo: (cantidad, hashes/s)} de los nodo:* con un aviso de hace menos de minero_vivo."""
        claves = list(self.redis.scan_iter(match=contrato.NODOS, count=contrato.NODOS_POR_SCAN))
        tuberia = self.redis.pipeline(transaction=False)
        for clave in claves:
            tuberia.hmget(clave, "tipo", "ultimo_aviso", "hashes_por_segundo", "bench")
        ahora, totales = time.time(), {tipo: [0, 0.0] for tipo in TIPOS}
        for tipo, aviso, hps, bench in tuberia.execute():
            if tipo in totales and aviso and ahora - float(aviso) < self.minero_vivo:
                totales[tipo][0] += 1
                totales[tipo][1] += float(hps or 0) or float(bench or 0)  # sin minar todavía manda 0: su bench
        return totales

    def lista_mineros(self):
        """Cada nodo:<cuenta> con si está vivo, su saldo y su historial (BC-54), del más viejo al más nuevo.

        Es lo que muestra GET /mineros (el listado salió de la API de la app) y lo que exponen las métricas.
        """
        claves = sorted(self.redis.scan_iter(match=contrato.NODOS, count=contrato.NODOS_POR_SCAN))
        cuentas = [clave.split(":", 1)[1] for clave in claves]
        tuberia = self.redis.pipeline(transaction=False)
        for clave, cuenta in zip(claves, cuentas):
            tuberia.hgetall(clave)
            tuberia.hgetall(contrato.HISTORIAL.format(cuenta))
        tuberia.llen(contrato.CADENA)
        respuestas = tuberia.execute()
        epoca = contrato.epoca(respuestas[-1])
        saldos = self.redis.hmget(contrato.SALDOS, cuentas) if cuentas else []
        shares = self.redis.zrange(contrato.SHARES.format(epoca), 0, -1, withscores=True)
        shares_epoca = {minero: int(cantidad) for minero, cantidad in shares}
        ahora, resultado = time.time(), []
        for cuenta, nodo, historial, saldo in zip(cuentas, respuestas[:-1:2], respuestas[1:-1:2], saldos):
            aviso = _numero(nodo.get("ultimo_aviso"))
            vivo = aviso is not None and ahora - aviso < self.minero_vivo
            resultado.append({
                "id": cuenta, "alias": nodo.get("alias") or cuenta[:8], "tipo": nodo.get("tipo") or "",
                "placa": nodo.get("placa"), "bench": _numero(nodo.get("bench")),
                "medido": _numero(nodo.get("medido")), "medido_estado": nodo.get("medido_estado") or "sin_datos",
                "potencia": contrato.potencia_efectiva(nodo),
                "hashes_por_segundo": _numero(nodo.get("hashes_por_segundo")), "ultimo_aviso": aviso, "vivo": vivo,
                "saldo": int(saldo or 0), "alta": _numero(historial.get("alta")),
                **{campo: _numero(historial.get(campo)) or 0
                   for campo in ("bloques", "recompensas", "shares", "rangos", "segundos_conectado")},
                "shares_epoca": shares_epoca.get(cuenta, 0), "epoca": epoca,
            })
        return resultado


def _numero(texto):
    """Lo que Redis guarda como texto, como entero si se puede y si no como decimal (None si no hay)."""
    try:
        return int(texto)
    except (TypeError, ValueError):
        try:
            return float(texto)
        except (TypeError, ValueError):
            return None


class ColectorMineros:
    """Mineros vivos y hashes/s por tipo, y el historial de cada minero (BC-54), leídos de Redis en el scrape.

    Las réplicas del gateway leen el mismo Redis y exponen lo mismo: en Grafana, `max`, no `sum`.
    """

    # (métrica, campo de lista_mineros, ayuda): contadores del historial, con etiquetas minero (alias) y tipo
    CONTADORES = (
        ("blockchain_minero_rangos", "rangos", "Rangos prestados a cada minero"),
        ("blockchain_minero_bloques", "bloques", "Bloques que ganó cada minero"),
        ("blockchain_minero_recompensas", "recompensas", "Lo que cobró cada minero: coinbase y participación"),
        ("blockchain_minero_shares", "shares", "Shares aceptados de cada minero"),
        ("blockchain_minero_segundos_conectado", "segundos_conectado", "Segundos que estuvo conectado cada minero"),
    )
    MEDIDAS = (
        ("blockchain_minero_hashes_por_segundo", "Hashes/s de cada minero en su último aviso (0 si no está vivo)"),
        ("blockchain_minero_vivo", "1 si el minero avisó hace menos de MINERO_VIVO_SEGUNDOS"),
        ("blockchain_minero_bench", "Hashes/s de cada minero en la tarea modelo"),
        ("blockchain_minero_medido", "Hashes/s que midió el gateway con los shares de cada minero (BC-74)"),
        ("blockchain_minero_potencia", "Hashes/s con que cuenta el sistema: min(bench, medido), o el bench si no hay medición"),
        ("blockchain_minero_alta", "Fecha de alta de cada minero (segundos Unix)"),
        ("blockchain_minero_shares_epoca", "Shares de cada minero en la época en curso"),
    )

    def __init__(self):
        self.gateway = None

    def collect(self):
        mineros = GaugeMetricFamily("blockchain_mineros", "Mineros vivos por tipo", labels=["tipo"])
        hashes = GaugeMetricFamily("blockchain_hashes_por_segundo", "Potencia viva por tipo", labels=["tipo"])
        epoca = GaugeMetricFamily("blockchain_epoca", "Época en curso (cada 10 bloques)")
        suspendidos = GaugeMetricFamily("blockchain_mineros_suspendidos",
                                        "Mineros suspendidos que esperan la decisión del organizador (BC-75)")
        contadores = [CounterMetricFamily(nombre, ayuda, labels=["minero", "tipo"])
                      for nombre, _, ayuda in self.CONTADORES]
        medidas = {nombre: GaugeMetricFamily(nombre, ayuda, labels=["minero", "tipo"]) for nombre, ayuda in self.MEDIDAS}
        if self.gateway is not None:
            try:
                for tipo, (cantidad, hps) in self.gateway.mineros_vivos().items():
                    mineros.add_metric([tipo], cantidad)
                    hashes.add_metric([tipo], hps)
                lista = self.gateway.lista_mineros()
                for m in lista:
                    etiquetas = [m["alias"], m["tipo"]]
                    for familia, (_, campo, _) in zip(contadores, self.CONTADORES):
                        familia.add_metric(etiquetas, m[campo])
                    valores = {"blockchain_minero_hashes_por_segundo": m["hashes_por_segundo"] if m["vivo"] else 0,
                               "blockchain_minero_vivo": int(m["vivo"]),
                               "blockchain_minero_bench": m["bench"],
                               "blockchain_minero_medido": m["medido"],
                               "blockchain_minero_potencia": m["potencia"],
                               "blockchain_minero_alta": m["alta"],
                               "blockchain_minero_shares_epoca": m["shares_epoca"]}
                    for nombre, valor in valores.items():
                        if valor is not None:
                            medidas[nombre].add_metric(etiquetas, valor or 0)
                if lista:
                    epoca.add_metric([], lista[0]["epoca"])
                suspendidos.add_metric([], self.gateway.redis.hlen(contrato.SUSPENDIDOS))
            except Exception:
                log.exception("No se pudieron leer los mineros para /metrics")
        yield mineros
        yield hashes
        yield epoca
        yield suspendidos
        yield from contadores
        yield from medidas.values()


COLECTOR = ColectorMineros()
REGISTRY.register(COLECTOR)


async def leer_json(request):
    try:
        return json.loads(await request.body())
    except ValueError:
        return None


def responder(codigo, cuerpo):
    if cuerpo is None:
        return Response(status_code=codigo)
    return JSONResponse(cuerpo, status_code=codigo)


def agregar_rutas(app, gateway):
    def ruta(metodo_http, atender):
        """Verifica la firma y la aprobación, lee el JSON y corre el método del gateway fuera del event
        loop (Redis y pika bloquean). La ruta firmada lleva la query, como la manda el minero."""
        async def pedido(request: Request):
            cuerpo = await request.body()
            firmada = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            try:
                minero, error = await run_in_threadpool(gateway.autenticar, request.headers, metodo_http,
                                                        firmada, cuerpo)
            except redislib.RedisError as falla:
                log.error("Redis no respondió al verificar un minero: %r", falla)
                return JSONResponse({"error": "Redis no disponible"}, status_code=503)
            if error:
                return JSONResponse({"error": error[1]}, status_code=error[0])
            try:
                datos = json.loads(cuerpo) if cuerpo else None
            except ValueError:
                datos = None
            return responder(*await run_in_threadpool(atender, minero, datos))
        return pedido

    app.post("/hello")(ruta("POST", gateway.hello))
    app.post("/bench")(ruta("POST", gateway.bench))
    app.post("/results")(ruta("POST", gateway.resultado))
    app.post("/keepalive")(ruta("POST", gateway.keepalive))
    app.get("/tasks/next")(ruta("GET", gateway.siguiente))

    @app.get("/mineros")
    async def mineros():
        """Los mineros con su historial (BC-54), sin firma: lo usan las pruebas y la operación. La página de la app
        no los muestra; en Grafana está la fila «Historial de mineros»."""
        try:
            return await run_in_threadpool(gateway.lista_mineros)
        except redislib.RedisError as falla:
            log.error("Redis no respondió al listar los mineros: %r", falla)
            return JSONResponse({"error": "Redis no disponible"}, status_code=503)


def revisar_siempre(gateway, cada):
    while True:
        try:
            gateway.revisar_vencidos()
        except Exception:
            log.exception("Falló la revisión de préstamos vencidos")
        try:
            gateway.revisar_desconectados()
        except Exception:
            log.exception("Falló la revisión de mineros desconectados")
        time.sleep(cada)


def main():
    servicio = Servicio("gateway")
    c = servicio.config
    rabbit = Rabbit(c, [config.texto("COLA_RANGOS", "rangos"), config.texto("COLA_RESULTADOS", "resultados")])
    gateway = Gateway(servicio.redis, rabbit,
                      cola_rangos=config.texto("COLA_RANGOS", "rangos"),
                      cola_resultados=config.texto("COLA_RESULTADOS", "resultados"),
                      lease=config.entero("TASK_LEASE_SECONDS", 30),
                      minero_vivo=config.entero("MINERO_VIVO_SEGUNDOS", MINERO_VIVO),
                      umbral=float(config.texto("UMBRAL_SIN_SOLUCION", str(contrato.UMBRAL_SIN_SOLUCION))),
                      muestreo=float(config.texto("MUESTREO_SIN_SOLUCION", str(contrato.MUESTREO_SIN_SOLUCION))))
    agregar_rutas(servicio.app, gateway)
    threading.Thread(target=revisar_siempre, args=(gateway, config.entero("REVISAR_PRESTAMOS_SEGUNDOS", 5)),
                     name="prestamos", daemon=True).start()
    for conexion in (rabbit, servicio.rabbit):  # la de los pedidos y la de /health
        threading.Thread(target=conexion.conectar, name="rabbit-conectar", daemon=True).start()
    servicio.servir_http(en_hilo=False)
