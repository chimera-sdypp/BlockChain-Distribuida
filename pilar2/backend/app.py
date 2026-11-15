"""Backend: la API por la que entran las transferencias y se consulta su estado (BC-20).

Desde BC-56 las transferencias vienen firmadas por su origen (contrato v2): el backend verifica el
formato y la firma, y calcula el id con el contenido, así que la misma transacción reenviada da 409.
También entran las cargas de saldo, firmadas por el organizador (`ORGANIZADOR_CLAVE`).
"""

import json
import logging
import threading
import time

import redis as redislib
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from prometheus_client import Counter

from comun import Servicio, config, contrato

log = logging.getLogger("backend")

# Por transacción, también en los lotes: aceptada, invalida, repetida (un reenvío) o error (no se pudo encolar).
TX_RECIBIDAS = Counter("blockchain_tx_recibidas", "Transacciones recibidas por el backend", ["resultado"])

# Crea los tx:<id> «recibida» sólo si no existía ninguno: todo o nada, también en un lote. Atómico:
# dos backends que reciben la misma transacción a la vez no la encolan dos veces.
# KEYS: tx:<id>…  ARGV: n campos por transacción y después campo, valor… de cada una, en el orden de KEYS.
# Devuelve los índices (desde 1) de las que ya existían; vacío si las creó.
LUA_CREAR = """
local repetidas = {}
for i, clave in ipairs(KEYS) do
  if redis.call('EXISTS', clave) == 1 then repetidas[#repetidas + 1] = i end
end
if #repetidas > 0 then return repetidas end
local n = tonumber(ARGV[1])
for i, clave in ipairs(KEYS) do
  local base = 1 + (i - 1) * 2 * n
  for j = base + 1, base + 2 * n, 2 do redis.call('HSET', clave, ARGV[j], ARGV[j + 1]) end
end
return repetidas
"""
CAMPOS_TX = ("origen", "destino", "monto", "nonce", "timestamp", "firma")
REENVIO = "ya existe: es un reenvío"


class Repetidas(Exception):
    """Alguna de las transacciones ya estaba en Redis: es un reenvío y no se encola."""

    def __init__(self, indices):
        super().__init__(indices)
        self.indices = indices


class NoDisponible(Exception):
    """RabbitMQ o Redis no respondieron: el cliente tiene que reintentar las que no se encolaron."""

    def __init__(self, motivo, encoladas):
        super().__init__(motivo)
        self.motivo, self.encoladas = motivo, encoladas


async def leer_json(request):
    """El cuerpo como JSON, o None si no se puede parsear."""
    try:
        return json.loads(await request.body())
    except ValueError:
        return None


def invalido(errores):
    return JSONResponse({"errores": errores}, status_code=400)


LOTE_MAXIMO = 1000
LIMITE_BLOQUES, LIMITE_MAXIMO_BLOQUES = 20, 100
PENDIENTES_VISIBLES = 50
MINERO_VIVO_SEGUNDOS = 15  # el keep-alive es cada 5 s: tres avisos perdidos y se lo da por caído
CAMPOS_ENTEROS_BLOQUE = ("nonce", "timestamp", "altura")


def _entero(texto):
    try:
        return int(texto)
    except (TypeError, ValueError):
        return None


def _bloque(guardado):
    """bloque:<hash> como lo guarda el NCT (con su altura), con los tipos de verdad.

    El minero y la recompensa no se guardan aparte: salen de la coinbase, la primera transacción.
    """
    bloque = dict(guardado, transactions=json.loads(guardado.get("transactions", "[]")))
    for campo in CAMPOS_ENTEROS_BLOQUE:
        if campo in bloque:
            bloque[campo] = int(bloque[campo])
    primera = bloque["transactions"][0] if bloque["transactions"] else {}
    if contrato.es_coinbase(primera):
        bloque["minero"], bloque["recompensa"] = primera["destino"], primera["monto"]
    else:
        bloque["minero"], bloque["recompensa"] = "", 0
    return bloque


def _transaccion(id_, tx):
    """tx:<id> como lo guardan el backend y el NCT, con los tipos de verdad."""
    resultado = {"id": id_, **tx}
    for campo in ("monto", "nonce", "timestamp"):
        if campo in resultado:
            resultado[campo] = int(resultado[campo])
    return resultado


def validar(datos, organizador):
    """Los errores de una transferencia firmada o de una carga del organizador; vacío si sirve."""
    if isinstance(datos, dict) and datos.get("origen") == contrato.EMISION:
        if not organizador:
            return ["origen: «emision» es para las cargas de saldo, y este backend no tiene ORGANIZADOR_CLAVE"]
        return contrato.validar_carga(datos, organizador)
    return contrato.validar_transaccion_firmada(datos)


def nueva_transaccion(datos):
    """La transacción como va a la cola y a tx:<id>: el id sale del contenido (o es carga-<n>)."""
    id_ = datos["id"] if contrato.es_emision(datos) else contrato.id_transaccion(datos)
    return {"id": id_, **{campo: datos[campo] for campo in CAMPOS_TX}}


def _numero(texto):
    """Lo que Redis guarda como texto, como entero si se puede y si no como decimal."""
    try:
        return int(texto)
    except (TypeError, ValueError):
        try:
            return float(texto)
        except (TypeError, ValueError):
            return None


def agregar_rutas(app, redis, publicador, cola=contrato.COLAS[0], lote_maximo=LOTE_MAXIMO,
                  minero_vivo=MINERO_VIVO_SEGUNDOS, organizador=""):
    crear = redis.register_script(LUA_CREAR)

    def registrar_y_encolar(txs):
        """Primero Redis y después la cola: así el NCT nunca ve una tx que todavía no está en Redis.

        Si alguna ya existía (un reenvío), no se crea ninguna y lanza Repetidas. Si la cola falla a
        mitad, las que ya se publicaron quedan (el NCT las va a procesar) y se borran las demás, para
        que no queden «recibida» para siempre. NoDisponible dice cuáles quedaron.
        """
        claves = [contrato.TX.format(tx["id"]) for tx in txs]
        campos = [str(x) for tx in txs for campo in CAMPOS_TX for x in (campo, tx[campo])]
        try:
            repetidas = crear(keys=claves, args=[len(CAMPOS_TX) + 1, *_con_estado(campos)])
        except redislib.RedisError as error:
            log.error("Redis no respondió al guardar %d transacciones: %r", len(txs), error)
            raise NoDisponible("Redis no disponible", []) from error
        if repetidas:
            raise Repetidas([int(i) - 1 for i in repetidas])
        encoladas = []
        for tx in txs:
            try:
                publicador.publicar(cola, tx)
            except Exception as error:
                log.error("No se pudo publicar %s en %s: %r", tx["id"], cola, error)
                _borrar(*claves[len(encoladas):])
                raise NoDisponible("RabbitMQ no disponible", encoladas) from error
            encoladas.append(tx["id"])

    def _con_estado(campos):
        """Le agrega estado=recibida a los campos de cada transacción (van de a len(CAMPOS_TX) pares)."""
        paso = 2 * len(CAMPOS_TX)
        return [x for i in range(0, len(campos), paso) for x in (*campos[i:i + paso], "estado", "recibida")]

    def _borrar(*claves):
        try:
            redis.delete(*claves)
        except redislib.RedisError:
            log.exception("No se pudieron borrar %d transacciones de Redis", len(claves))

    async def encolar(txs):
        """Registra y publica fuera del event loop (Redis y pika bloquean) y cuenta la métrica."""
        try:
            await run_in_threadpool(registrar_y_encolar, txs)
        except Repetidas:
            TX_RECIBIDAS.labels("repetida").inc(len(txs))
            raise
        except NoDisponible as error:
            TX_RECIBIDAS.labels("aceptada").inc(len(error.encoladas))
            TX_RECIBIDAS.labels("error").inc(len(txs) - len(error.encoladas))
            raise
        TX_RECIBIDAS.labels("aceptada").inc(len(txs))

    @app.post("/api/transacciones", status_code=202)
    async def recibir(request: Request):
        datos = await leer_json(request)
        errores = ["el cuerpo no es JSON válido"] if datos is None else validar(datos, organizador)
        if errores:
            TX_RECIBIDAS.labels("invalida").inc()
            log.info("Transacción rechazada por el formato o la firma: %s", "; ".join(errores))
            return invalido(errores)
        tx = nueva_transaccion(datos)
        try:
            await encolar([tx])
        except Repetidas:
            log.info("Transacción %s reenviada: no se vuelve a encolar", tx["id"])
            return JSONResponse({"error": REENVIO, "id": tx["id"]}, status_code=409)
        except NoDisponible as error:
            return JSONResponse({"error": error.motivo}, status_code=503)
        return {"id": tx["id"]}

    @app.post("/api/transacciones/lote", status_code=202)
    async def recibir_lote(request: Request):
        """Todo o nada: si una es inválida no entra ninguna (400 con los errores por índice), y si una
        ya existía o está dos veces, tampoco (409, también por índice)."""
        datos = await leer_json(request)
        if not isinstance(datos, list) or not datos:
            return invalido(["el cuerpo tiene que ser una lista de transacciones, no vacía"])
        if len(datos) > lote_maximo:
            return JSONResponse({"errores": [f"el lote tiene {len(datos)} transacciones; el máximo es {lote_maximo}"]},
                                status_code=413)
        # Verificar mil firmas lleva unos 100 ms: fuera del event loop, para no frenar los otros pedidos.
        errores = await run_in_threadpool(lambda: {str(i): e for i, e in enumerate(validar(tx, organizador) for tx in datos) if e})
        if errores:
            TX_RECIBIDAS.labels("invalida").inc(len(datos))
            return invalido(errores)
        txs = [nueva_transaccion(tx) for tx in datos]
        vistos, repetidas = set(), []
        for i, tx in enumerate(txs):
            if tx["id"] in vistos:
                repetidas.append(i)
            vistos.add(tx["id"])
        try:
            if repetidas:
                raise Repetidas(repetidas)
            await encolar(txs)
        except Repetidas as error:
            return JSONResponse({"errores": {str(i): [REENVIO] for i in error.indices}}, status_code=409)
        except NoDisponible as error:
            return JSONResponse({"error": error.motivo, "encoladas": error.encoladas}, status_code=503)
        return {"ids": [tx["id"] for tx in txs]}

    # Las consultas son sync: FastAPI las corre en su threadpool, así Redis no frena el event loop.

    @app.get("/api/transacciones/{id_}")
    def transaccion(id_: str):
        tx = redis.hgetall(contrato.TX.format(id_))
        if not tx:
            return JSONResponse({"error": "no existe la transacción"}, status_code=404)
        return _transaccion(id_, tx)

    @app.get("/api/cajas")
    def cajas():
        """Las transacciones validadas que esperan caja y cuántas hay apiladas en cada caja."""
        tuberia = redis.pipeline(transaction=False)
        tuberia.lrange(contrato.PENDIENTES, 0, PENDIENTES_VISIBLES - 1)
        tuberia.llen(contrato.PENDIENTES)
        tuberia.lrange(contrato.CAJAS, 0, -1)
        ids, total, guardadas = tuberia.execute()
        for id_ in ids:
            tuberia.hgetall(contrato.TX.format(id_))
        pendientes = [_transaccion(id_, tx) for id_, tx in zip(ids, tuberia.execute()) if tx]
        return {"pendientes": pendientes, "total_pendientes": total,
                "cajas": [len(json.loads(caja)) for caja in guardadas]}

    @app.get("/api/mineros")
    def mineros():
        """Cada nodo:<id> que escribe el gateway, con si está vivo y su saldo (incluye lo que cobró)."""
        claves = sorted(redis.scan_iter(match=contrato.NODOS, count=100))
        ids = [clave.split(":", 1)[1] for clave in claves]
        tuberia = redis.pipeline(transaction=False)
        for clave in claves:
            tuberia.hgetall(clave)
        nodos = tuberia.execute()
        saldos = redis.hmget(contrato.SALDOS, ids) if ids else []
        ahora = time.time()
        resultado = []
        for id_, nodo, saldo in zip(ids, nodos, saldos):
            minero = {"id": id_, "alias": nodo.get("alias"), "tipo": nodo.get("tipo"), "placa": nodo.get("placa"),
                      **{campo: _numero(nodo.get(campo)) for campo in ("bench", "hashes_por_segundo", "ultimo_aviso")}}
            minero["vivo"] = minero["ultimo_aviso"] is not None and ahora - minero["ultimo_aviso"] < minero_vivo
            minero["saldo"] = int(saldo or 0)
            resultado.append(minero)
        return resultado

    @app.get("/api/cuentas/{cuenta}")
    def cuenta(cuenta: str):
        """Lo que necesita un cliente para firmar: el saldo y el último nonce aceptado (el próximo es +1).

        `emision` da el número de la última carga. Una cuenta que no existe tiene todo en 0.
        """
        if cuenta != contrato.EMISION and not contrato.cuenta_valida(cuenta):
            return invalido(["cuenta: clave pública Ed25519 en hex (64 caracteres en minúscula)"])
        tuberia = redis.pipeline(transaction=False)
        tuberia.hget(contrato.SALDOS, cuenta)
        tuberia.hget(contrato.COMPROMETIDO, cuenta)
        tuberia.hget(contrato.NONCES, cuenta)
        tuberia.hget(contrato.CUENTAS, cuenta)
        saldo_, comprometido, nonce, datos = tuberia.execute()
        datos = json.loads(datos) if datos else {}
        return {"cuenta": cuenta, "saldo": int(saldo_ or 0), "comprometido": int(comprometido or 0),
                "nonce": int(nonce or 0), "aprobada": bool(datos), "tipo": datos.get("tipo"),
                "nombre": datos.get("nombre")}

    @app.get("/api/saldos")
    def saldos():
        return {usuario: int(saldo) for usuario, saldo in redis.hgetall(contrato.SALDOS).items()}

    @app.get("/api/saldos/{usuario}")
    def saldo(usuario: str):
        return {"usuario": usuario, "saldo": int(redis.hget(contrato.SALDOS, usuario) or 0)}

    @app.get("/api/bloques")
    def bloques(limite: str = str(LIMITE_BLOQUES), desde: str | None = None):
        """Del más nuevo al más viejo, empezando por la altura `desde` (por defecto, la punta)."""
        errores = []
        limite = _entero(limite)
        if limite is None or not 1 <= limite <= LIMITE_MAXIMO_BLOQUES:
            errores.append(f"limite: entero de 1 a {LIMITE_MAXIMO_BLOQUES}")
        if desde is not None:
            desde = _entero(desde)
            if desde is None or desde < 0:
                errores.append("desde: entero mayor o igual que 0")
        if errores:
            return invalido(errores)
        largo = redis.llen(contrato.CADENA)
        if largo == 0:
            return []
        primera = largo - 1 if desde is None else min(desde, largo - 1)
        ultima = max(primera - limite + 1, 0)
        hashes = redis.lrange(contrato.CADENA, ultima, primera)
        tuberia = redis.pipeline(transaction=False)
        for hash_ in hashes:
            tuberia.hgetall(contrato.BLOQUE.format(hash_))
        guardados = tuberia.execute()
        return [_bloque(guardado) for guardado in reversed(guardados)]

    @app.get("/api/bloques/{hash_}")
    def bloque(hash_: str):
        guardado = redis.hgetall(contrato.BLOQUE.format(hash_))
        if not guardado:
            return JSONResponse({"error": "no existe el bloque"}, status_code=404)
        return _bloque(guardado)

    @app.exception_handler(redislib.RedisError)
    def redis_caido(request, error):
        log.error("Redis no respondió en %s: %r", request.url.path, error)
        return JSONResponse({"error": "Redis no disponible"}, status_code=503)


def leer_organizador():
    """ORGANIZADOR_CLAVE: la cuenta del organizador, que firma las cargas de saldo. Vacía: no hay cargas."""
    organizador = config.texto("ORGANIZADOR_CLAVE")
    if organizador and not contrato.cuenta_valida(organizador):
        raise ValueError("ORGANIZADOR_CLAVE tiene que ser una clave pública Ed25519 en hex (64 caracteres)")
    return organizador


def main():
    cola = config.texto("COLA_TRANSACCIONES", contrato.COLAS[0])
    servicio = Servicio("backend", colas=[cola])
    # Nadie consume en esta conexión: se usa sólo para publicar desde los hilos HTTP.
    agregar_rutas(servicio.app, servicio.redis, servicio.rabbit, cola=cola,
                  lote_maximo=config.entero("LOTE_MAXIMO", LOTE_MAXIMO),
                  minero_vivo=config.entero("MINERO_VIVO_SEGUNDOS", MINERO_VIVO_SEGUNDOS),
                  organizador=leer_organizador())
    threading.Thread(target=servicio.rabbit.conectar, name="rabbit-conectar", daemon=True).start()
    servicio.servir_http(en_hilo=False)
