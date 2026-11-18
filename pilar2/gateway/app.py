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
VENCIDOS = Counter("blockchain_prestamos_vencidos", "Rangos que volvieron a la cola porque su minero dejó de avisar")
SHARES = Counter("blockchain_shares", "Shares que mandaron los mineros", ["resultado"])  # aceptado o descartado
RECHAZOS = Counter("blockchain_pedidos_rechazados", "Pedidos de mineros rechazados", ["motivo"])  # firma o no_aprobado
TEXTOS_EN_MEMORIA, TAREAS_EN_MEMORIA = 512, 16

TIPOS = ("gpu", "cpu")
MINERO_VIVO = 15  # segundos: el keep-alive es cada 5 s; tres perdidos y se lo da por caído


PRESTAMO = "prestamo:{}"  # prestamo:<tarea>:<rango> → {mensaje, minero}

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
                 lease=30, minero_vivo=MINERO_VIVO):
        self.redis, self.rabbit = redis, rabbit
        self.cola_rangos, self.cola_resultados = cola_rangos, cola_resultados
        self.lease, self.minero_vivo = lease, minero_vivo
        self._renovar = redis.register_script(LUA_RENOVAR)
        self._reclamar = redis.register_script(LUA_RECLAMAR)
        self._sumar_shares = redis.register_script(LUA_SHARES)
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
        return minero, None

    def pedir_alta(self, minero, cuerpo):
        """Un minero firmado y sin aprobar queda en `altas:pendientes` (BC-58): la firma ya demuestra que
        tiene la privada. El alias sale del /hello (`alias`); si no viene, el principio de la cuenta."""
        try:
            alias = json.loads(cuerpo).get("alias") if cuerpo else None
        except (ValueError, AttributeError):
            alias = None
        alias = alias if contrato.nombre_valido(alias) else minero[:12]
        if self.altas.pedir(minero, "minero", alias) == "nueva":
            log.info("Pedido de alta de %s (minero): %s", alias, minero)

    # ── Registro ──

    def hello(self, minero, datos):
        """Devuelve (código, cuerpo). Un minero sin bench recibe la tarea modelo."""
        if not isinstance(datos, dict) or datos.get("minero") != minero \
                or datos.get("tipo") not in TIPOS or not isinstance(datos.get("placa"), str):
            return 400, {"error": "hace falta {minero (el de X-Minero), tipo: gpu | cpu, placa}"}
        nodo = contrato.NODO.format(minero)
        alias = json.loads(self.redis.hget(contrato.CUENTAS, minero) or "{}").get("nombre") or minero[:12]
        self.redis.hset(nodo, mapping={"tipo": datos["tipo"], "placa": datos["placa"][:100], "alias": alias,
                                       "ultimo_aviso": int(time.time())})
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

        Primero se anota el préstamo en Redis y recién después se hace el ack: un rango nunca
        queda sin dueño ni en la cola. Los de tareas cerradas se descartan (ack sin entregar).
        """
        try:
            while True:
                rango = self.rabbit.tomar(self.cola_rangos, lambda mensaje: self._prestar(mensaje, minero))
                if rango is None:
                    return 204, None
                if rango is not False:
                    ENTREGADAS.inc()
                    return 200, rango
        except redislib.RedisError as error:
            log.error("Redis no respondió al prestar un rango (vuelve a la cola): %r", error)
            return 503, {"error": "Redis no disponible"}

    def _prestar(self, mensaje, minero):
        """Dentro de `tomar`: si devuelve, se hace el ack; si lanza (Redis caído), el rango vuelve."""
        if not self.tarea_en_curso(mensaje["tarea"]):
            log.info("Descarto el rango %s de la tarea %s, que ya se cerró", mensaje["rango"], mensaje["tarea"])
            return False
        texto = self.texto(mensaje["tarea"], minero)
        if texto is None:
            log.error("La tarea %s no tiene todo lo que hace falta para armar el texto: descarto el rango %s",
                      mensaje["tarea"], mensaje["rango"])
            return False
        miembro = f"{mensaje['tarea']}:{mensaje['rango']}"
        tuberia = self.redis.pipeline()
        tuberia.hset(PRESTAMO.format(miembro), mapping={"mensaje": json.dumps(mensaje), "minero": minero})
        tuberia.zadd(contrato.PRESTAMOS, {miembro: time.time() + self.lease})
        tuberia.hincrby(contrato.HISTORIAL.format(minero), "rangos", 1)  # el reparto por minero (BC-59)
        tuberia.execute()
        log.info("Rango %s prestado a %s", miembro, minero)
        return {**mensaje, "bloque": texto}

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
        corto, texto = contrato.prefijo_share(mensaje["prefijo"]), self.texto(tarea, minero)
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
        mensaje = {k: v for k, v in datos.items() if k != "shares"}
        try:
            self.rabbit.publicar(self.cola_resultados, mensaje)
        except Exception as error:
            log.error("No se pudo publicar el resultado de %s:%s: %r", datos["tarea"], datos["rango"], error)
            return 503, {"error": "RabbitMQ no disponible"}
        self._liberar(f"{datos['tarea']}:{datos['rango']}")
        return 202, {"shares": shares}

    def _liberar(self, miembro):
        try:
            tuberia = self.redis.pipeline()
            tuberia.zrem(contrato.PRESTAMOS, miembro)
            tuberia.delete(PRESTAMO.format(miembro))
            tuberia.execute()
        except redislib.RedisError:
            log.exception("No se pudo liberar el préstamo %s (vencerá y el TrP tolera el repetido)", miembro)

    # ── Keep-alive ──

    def keepalive(self, minero, datos):
        """POST /keepalive: el minero sigue vivo; suma los shares que trae, renueva su préstamo o le dice que corte."""
        rango = datos.get("rango") if isinstance(datos, dict) else None
        if not isinstance(datos, dict) or datos.get("minero") != minero \
                or not es_numero(datos.get("hashes_por_segundo")) or not shares_validos(datos.get("shares")) \
                or not (rango is None or (isinstance(rango, dict) and isinstance(rango.get("tarea"), str)
                                          and es_entero(rango.get("rango")))):
            return 400, {"error": "hace falta {minero, hashes_por_segundo, rango: {tarea, rango} | null, shares?}"}
        self.redis.hset(contrato.NODO.format(minero), mapping={
            "hashes_por_segundo": int(datos["hashes_por_segundo"]), "ultimo_aviso": int(time.time())})
        if rango is None:
            return 200, {"cortar": False}
        respuesta = {}
        if "shares" in datos:
            respuesta["shares"] = self.aceptar_shares(minero, rango["tarea"], rango["rango"], datos["shares"])
        miembro = f"{rango['tarea']}:{rango['rango']}"
        if not self.tarea_en_curso(rango["tarea"]):
            # El minero deja el rango sin mandar resultado: el préstamo se borra acá.
            self._liberar(miembro)
            log.info("A %s: cortá el rango %s, su tarea ya se cerró", minero, miembro)
            return 200, {"cortar": True, **respuesta}
        self._renovar(keys=[contrato.PRESTAMOS, PRESTAMO.format(miembro)],
                      args=[miembro, minero, time.time() + self.lease])
        return 200, {"cortar": False, **respuesta}

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
                    VENCIDOS.inc()
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
                totales[tipo][1] += float(hps or bench or 0)
        return totales

    def rangos_por_minero(self):
        """[(alias, tipo, rangos prestados)] de cada nodo:*; sin alias, el principio de la cuenta (BC-59)."""
        claves = list(self.redis.scan_iter(match=contrato.NODOS, count=contrato.NODOS_POR_SCAN))
        cuentas = [clave.split(":", 1)[1] for clave in claves]
        tuberia = self.redis.pipeline(transaction=False)
        for clave, cuenta in zip(claves, cuentas):
            tuberia.hmget(clave, "alias", "tipo")
            tuberia.hget(contrato.HISTORIAL.format(cuenta), "rangos")
        respuestas = tuberia.execute()
        return [(alias or cuenta[:8], tipo or "", int(rangos or 0))
                for cuenta, (alias, tipo), rangos in zip(cuentas, respuestas[::2], respuestas[1::2])]


class ColectorMineros:
    """Mineros vivos y hashes/s por tipo, y rangos prestados a cada minero, leídos de Redis al momento del scrape.

    Las réplicas del gateway leen el mismo Redis y exponen lo mismo: en Grafana, `max`, no `sum`.
    """

    def __init__(self):
        self.gateway = None

    def collect(self):
        mineros = GaugeMetricFamily("blockchain_mineros", "Mineros vivos por tipo", labels=["tipo"])
        hashes = GaugeMetricFamily("blockchain_hashes_por_segundo", "Potencia viva por tipo", labels=["tipo"])
        rangos = CounterMetricFamily("blockchain_minero_rangos", "Rangos prestados a cada minero",
                                     labels=["minero", "tipo"])
        if self.gateway is not None:
            try:
                for tipo, (cantidad, hps) in self.gateway.mineros_vivos().items():
                    mineros.add_metric([tipo], cantidad)
                    hashes.add_metric([tipo], hps)
                for alias, tipo, cantidad in self.gateway.rangos_por_minero():
                    rangos.add_metric([alias, tipo], cantidad)
            except Exception:
                log.exception("No se pudieron leer los mineros para /metrics")
        yield mineros
        yield hashes
        yield rangos


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


def revisar_siempre(gateway, cada):
    while True:
        try:
            gateway.revisar_vencidos()
        except Exception:
            log.exception("Falló la revisión de préstamos vencidos")
        time.sleep(cada)


def main():
    servicio = Servicio("gateway")
    c = servicio.config
    rabbit = Rabbit(c, [config.texto("COLA_RANGOS", "rangos"), config.texto("COLA_RESULTADOS", "resultados")])
    gateway = Gateway(servicio.redis, rabbit,
                      cola_rangos=config.texto("COLA_RANGOS", "rangos"),
                      cola_resultados=config.texto("COLA_RESULTADOS", "resultados"),
                      lease=config.entero("TASK_LEASE_SECONDS", 30),
                      minero_vivo=config.entero("MINERO_VIVO_SEGUNDOS", MINERO_VIVO))
    agregar_rutas(servicio.app, gateway)
    threading.Thread(target=revisar_siempre, args=(gateway, config.entero("REVISAR_PRESTAMOS_SEGUNDOS", 5)),
                     name="prestamos", daemon=True).start()
    for conexion in (rabbit, servicio.rabbit):  # la de los pedidos y la de /health
        threading.Thread(target=conexion.conectar, name="rabbit-conectar", daemon=True).start()
    servicio.servir_http(en_hilo=False)
