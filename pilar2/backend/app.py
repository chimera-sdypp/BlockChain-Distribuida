"""Backend: la API por la que entran las transferencias y se consulta su estado (BC-20).

Desde BC-56 las transferencias vienen firmadas por su origen (contrato v2): el backend verifica el
formato y la firma, y calcula el id con el contenido, así que la misma transacción reenviada da 409.
También entran las cargas de saldo, firmadas por el organizador (`ORGANIZADOR_CLAVE`).

BC-58: los pedidos de alta (`POST /api/altas`, públicos y firmados por quien pide) y el panel del
organizador (`/api/organizador/*`), donde cada pedido va firmado con su clave (X-Organizador).

Contrato v3 (BC-79, BC-80, BC-82): las altas, bajas y permisos llegan como transacciones firmadas por el organizador
(`POST /api/transacciones`, como las cargas) y quedan aprobadas cuando entran en un bloque (antes, aprobar escribía
`cuentas` sin firma). Los pendientes y los suspendidos se leen firmados por el organizador (BC-88). Se suman
las lecturas para verificar desde afuera: los bloques en orden ascendente, la prueba de Merkle de una cuenta contra
la raíz de estado (`/api/prueba/<cuenta>`) y la lista de shares de cada época (`/api/epocas/<n>`).

Contrato v4 (BC-99): todo lo firmado lleva el hash del génesis de esta cadena; los clientes lo leen de `/api/cadena`.
Lo firmado para otra cadena (una anterior al reinicio) no pasa la validación.
"""

import json
import logging
import threading
import time

import redis as redislib
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from prometheus_client import Counter, Gauge

from comun import Servicio, config, contrato, estado as estado_cadena
from comun.altas import ALTAS_MAXIMAS, Altas

log = logging.getLogger("backend")

# Por transacción, también en los lotes: aceptada, invalida, repetida (un reenvío) o error (no se pudo encolar).
TX_RECIBIDAS = Counter("blockchain_tx_recibidas", "Transacciones recibidas por el backend", ["resultado"])
# BC-86: las firmas de los validadores y la finalidad.
FIRMAS_RECIBIDAS = Counter("blockchain_firmas", "Firmas de validadores recibidas", ["resultado"])  # aceptada, invalida, doble
ALTURA_FINALIZADA = Gauge("blockchain_altura_finalizada", "La última altura finalizada (2/3 de los validadores la firmaron)")
VALIDADORES = Gauge("blockchain_validadores", "Validadores con alta en la cadena")

# Anota la firma de un validador (BC-86). Una por validador y altura: si ya firmó otro bloque a esa altura, deja la
# prueba de la doble firma y no la cuenta. Lleva la última altura que firmó cada uno (firmar un bloque firma los
# anteriores). KEYS: firmas:<altura>, firmas:ultima, firmas:dobles. ARGV: validador, altura, datos (JSON), block_hash.
# Devuelve 'aceptada', 'repetida' o 'doble'.
LUA_FIRMA = """
local previa = redis.call('HGET', KEYS[1], ARGV[1])
if previa then
  if cjson.decode(previa).block_hash == ARGV[4] then return 'repetida' end
  redis.call('RPUSH', KEYS[3], cjson.encode({validador = ARGV[1], altura = tonumber(ARGV[2]), primera = cjson.decode(previa),
                                             segunda = cjson.decode(ARGV[3])}))
  return 'doble'
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
local ultima = tonumber(redis.call('ZSCORE', KEYS[2], ARGV[1]) or '-1')
if tonumber(ARGV[2]) > ultima then redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1]) end
return 'aceptada'
"""

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
CAMPOS_TX = ("origen", "destino", "monto", "nonce", "timestamp", "firma", "datos")
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
    """tx:<id> como lo guardan el backend y el NCT (completada con el bloque si está confirmada), con
    los tipos de verdad."""
    resultado = {"id": id_, **tx}
    for campo in ("monto", "nonce", "timestamp"):
        if campo in resultado:
            resultado[campo] = int(resultado[campo])
    if resultado.get("datos") == "":
        del resultado["datos"]
    return resultado


def validar(datos, organizador, cadena):
    """Los errores de una transferencia firmada, de una carga o de un acta del organizador; vacío si sirve."""
    return contrato.validar_de_entrada(datos, organizador, cadena)


def nueva_transaccion(datos, cadena):
    """La transacción como va a la cola y a tx:<id>: el id sale del contenido (o es carga-<n>, alta-<n>…)."""
    id_ = datos["id"] if contrato.es_emision(datos) or contrato.es_acta(datos) else contrato.id_transaccion(datos, cadena)
    return {"id": id_, **{campo: datos.get(campo, "") for campo in CAMPOS_TX}}


def agregar_rutas(app, redis, publicador, cola=contrato.COLAS[0], lote_maximo=LOTE_MAXIMO,
                  organizador="", altas_maximas=ALTAS_MAXIMAS):
    crear = redis.register_script(LUA_CREAR)
    anotar_firma = redis.register_script(LUA_FIRMA)
    altas = Altas(redis, maximas=altas_maximas)

    def genesis():
        """El hash del bloque 0 (v4): va en todo lo que se firma. None si la cadena todavía no tiene génesis."""
        return redis.lindex(contrato.CADENA, 0)

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
                publicador.publicar(cola, {k: v for k, v in tx.items() if not (k == "datos" and v == "")})
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
        cadena = await run_in_threadpool(genesis)
        errores = ["el cuerpo no es JSON válido"] if datos is None else validar(datos, organizador, cadena)
        if errores:
            TX_RECIBIDAS.labels("invalida").inc()
            log.info("Transacción rechazada por el formato o la firma: %s", "; ".join(errores))
            return invalido(errores)
        return await recibir_una(datos, cadena)

    async def recibir_una(datos, cadena):
        """Encola una transacción ya validada: 202, 409 si es un reenvío o 503."""
        tx = nueva_transaccion(datos, cadena)
        try:
            await encolar([tx])
        except Repetidas:
            log.info("Transacción %s reenviada: no se vuelve a encolar", tx["id"])
            return JSONResponse({"error": REENVIO, "id": tx["id"]}, status_code=409)
        except NoDisponible as error:
            return JSONResponse({"error": error.motivo}, status_code=503)
        if contrato.es_acta(tx):
            log.info("Acta %s (%s): %s %s", tx["id"], "delegada" if contrato.es_delegada(tx) else "del organizador",
                     tx["destino"], tx["datos"])
        elif contrato.es_emision(tx):
            log.info("Carga %s del organizador: %s a %s", tx["id"], tx["monto"], tx["destino"])
        return JSONResponse({"id": tx["id"]}, status_code=202)

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
        cadena = await run_in_threadpool(genesis)
        errores = await run_in_threadpool(
            lambda: {str(i): e for i, e in enumerate(validar(tx, organizador, cadena) for tx in datos) if e})
        if errores:
            TX_RECIBIDAS.labels("invalida").inc(len(datos))
            return invalido(errores)
        txs = [nueva_transaccion(tx, cadena) for tx in datos]
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
        return _transaccion(id_, {**_del_bloque(id_, tx), **tx})

    def _del_bloque(id_, tx):
        """BC-70: una tx confirmada es sólo {estado, bloque}; su contenido se lee del bloque."""
        if "origen" in tx or not tx.get("bloque"):
            return {}
        guardadas = redis.hget(contrato.BLOQUE.format(tx["bloque"]), "transactions")
        for completa in json.loads(guardadas or "[]"):
            if completa.get("id") == id_:
                return {campo: completa[campo] for campo in CAMPOS_TX if campo in completa}
        log.error("La transacción %s apunta al bloque %s, pero no está ahí", id_, tx["bloque"])
        return {}

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

    @app.get("/api/cuentas/{cuenta}")
    def cuenta(cuenta: str):
        """Lo que necesita un cliente para firmar: el saldo y el último nonce aceptado (el próximo es +1).

        `emision` da el número de la última carga. Una cuenta que no existe tiene todo en 0. `alta_en_camino`: el NCT
        ya validó su alta y todavía no entró en un bloque (BC-93).
        """
        if cuenta != contrato.EMISION and not contrato.cuenta_valida(cuenta):
            return invalido(["cuenta: clave pública Ed25519 en hex (64 caracteres en minúscula)"])
        tuberia = redis.pipeline(transaction=False)
        tuberia.hget(contrato.SALDOS, cuenta)
        tuberia.hget(contrato.COMPROMETIDO, cuenta)
        tuberia.hget(contrato.NONCES, cuenta)
        tuberia.hget(contrato.CUENTAS, cuenta)
        tuberia.hexists(contrato.ALTAS_PENDIENTES, cuenta)
        tuberia.hexists(contrato.ALTAS_EN_CAMINO, cuenta)
        saldo_, comprometido, nonce, datos, pedida, en_camino = tuberia.execute()
        datos = json.loads(datos) if datos else {}
        return {"cuenta": cuenta, "saldo": int(saldo_ or 0), "comprometido": int(comprometido or 0),
                "nonce": int(nonce or 0), "aprobada": bool(datos), "tipo": datos.get("tipo"),
                "nombre": datos.get("nombre"), "alta_pedida": bool(pedida), "alta_en_camino": bool(en_camino)}

    @app.get("/api/saldos")
    def saldos():
        return {usuario: int(saldo) for usuario, saldo in redis.hgetall(contrato.SALDOS).items()}

    @app.get("/api/saldos/{usuario}")
    def saldo(usuario: str):
        return {"usuario": usuario, "saldo": int(redis.hget(contrato.SALDOS, usuario) or 0)}

    @app.get("/api/bloques")
    def bloques(limite: str = str(LIMITE_BLOQUES), desde: str | None = None, orden: str = "desc"):
        """Del más nuevo al más viejo, empezando por la altura `desde` (por defecto, la punta). Con `orden=asc`, del
        más viejo al más nuevo desde `desde` (por defecto, el génesis): lo que usan los nodos y la billetera para
        bajar sólo lo nuevo."""
        errores = []
        limite = _entero(limite)
        if limite is None or not 1 <= limite <= LIMITE_MAXIMO_BLOQUES:
            errores.append(f"limite: entero de 1 a {LIMITE_MAXIMO_BLOQUES}")
        if desde is not None:
            desde = _entero(desde)
            if desde is None or desde < 0:
                errores.append("desde: entero mayor o igual que 0")
        if orden not in ("asc", "desc"):
            errores.append("orden: asc | desc")
        if errores:
            return invalido(errores)
        largo = redis.llen(contrato.CADENA)
        if largo == 0:
            return []
        if orden == "asc":
            primera = desde or 0
            if primera >= largo:
                return []
            hashes = redis.lrange(contrato.CADENA, primera, min(primera + limite, largo) - 1)
        else:
            primera = largo - 1 if desde is None else min(desde, largo - 1)
            hashes = redis.lrange(contrato.CADENA, max(primera - limite + 1, 0), primera)[::-1]
        tuberia = redis.pipeline(transaction=False)
        for hash_ in hashes:
            tuberia.hgetall(contrato.BLOQUE.format(hash_))
        return con_firmas([_bloque(guardado) for guardado in tuberia.execute()])

    def con_firmas(bloques_):
        """BC-86: a cada bloque, las firmas de validadores sobre él y si está finalizado. Van aparte del bloque (no
        cambian su hash): quien verifica las comprueba él mismo con las altas de los validadores en la cadena."""
        if not bloques_:
            return bloques_
        tuberia = redis.pipeline(transaction=False)
        for b in bloques_:
            tuberia.hgetall(contrato.FIRMAS.format(b["altura"]))
        tuberia.hget(contrato.FINALIZADA, "altura")
        *firmas, finalizada = tuberia.execute()
        finalizada = int(finalizada) if finalizada is not None else -1
        for b, de_la_altura in zip(bloques_, firmas):
            b["firmas"] = [{"validador": v, "firma": json.loads(d)["firma"]} for v, d in sorted(de_la_altura.items())
                           if json.loads(d)["block_hash"] == b["block_hash"]]
            b["finalizado"] = b["altura"] <= finalizada
        return bloques_

    @app.get("/api/epocas/{numero}")
    def epoca(numero: str):
        """BC-82: la lista de shares con que se pagó la época, para recalcular el pago desde afuera. Su `raiz` es la
        que quedó en la cadena, en la transacción `shares-<n>` del primer bloque de la época siguiente."""
        n = _entero(numero)
        if n is None or n < 0:
            return invalido(["la época es un entero mayor o igual que 0"])
        guardado = redis.hgetall(contrato.EPOCA.format(n))
        if not guardado:
            return JSONResponse({"error": "esa época todavía no se pagó"}, status_code=404)
        detalle = json.loads(guardado.get("detalle") or "{}")
        return {"epoca": n, "fondo": int(guardado.get("fondo") or 0), "raiz": contrato.raiz_shares(detalle),
                "detalle": detalle, "lista": contrato.lista_shares(detalle)}

    pruebas = {}  # (altura, punta) → las hojas del estado: se calculan una vez por bloque y por pod

    @app.get("/api/prueba/{cuenta}")
    def prueba_merkle(cuenta: str):
        """BC-80: la hoja de `cuenta` y su rama de Merkle en el estado al cerrar la punta. Esa raíz es la que va a
        llevar el bloque siguiente (`raiz_estado`). Una billetera la compara con la que calculó ella."""
        if cuenta != contrato.EMISION and not contrato.cuenta_valida(cuenta):
            return invalido(["cuenta: clave pública Ed25519 en hex (64 caracteres en minúscula)"])
        cache = estado_cadena.cache_de_redis(redis)
        clave = (cache["altura"], cache["punta"])
        if clave not in pruebas:
            pruebas.clear()
            pruebas[clave] = estado_cadena.hojas_de_cache(cache)
        return {"cuenta": cuenta, "altura": cache["altura"], "block_hash": cache["punta"],
                **estado_cadena.prueba(pruebas[clave], cuenta)}

    @app.get("/api/cuentas")
    def lista_cuentas():
        """Públicas (v3): las cuentas aprobadas (las que están en la cadena), con su saldo. Los pedidos pendientes y
        los mineros suspendidos no: son datos de operación del organizador y se leen firmados (BC-88)."""
        return {"cuentas": altas.aprobadas()}

    @app.get("/api/cadena")
    def cadena_():
        """v4: lo que un cliente necesita para firmar y verificar: el hash del génesis (va adelante de todo lo firmado),
        el organizador y el piso de prefijo que fijó el génesis."""
        hash_ = genesis()
        guardado = redis.hgetall(contrato.BLOQUE.format(hash_)) if hash_ else None
        if not guardado:
            return JSONResponse({"error": "la cadena todavía no tiene génesis"}, status_code=503)
        bloque = _bloque(guardado)
        organizador_ = next((tx["destino"] for tx in bloque["transactions"] if contrato.tipo_de(tx) == "organizador"), "")
        return {"genesis": hash_, "organizador": organizador_, "prefijo_minimo": contrato.prefijo_minimo_de(bloque),
                "timestamp": bloque["timestamp"]}

    @app.get("/api/bloques/{hash_}")
    def bloque(hash_: str):
        guardado = redis.hgetall(contrato.BLOQUE.format(hash_))
        if not guardado:
            return JSONResponse({"error": "no existe el bloque"}, status_code=404)
        return con_firmas([_bloque(guardado)])[0]

    # ── BC-86: firmas de los validadores y finalidad ──

    def validadores():
        return {c for c, d in redis.hgetall(contrato.CUENTAS).items() if json.loads(d).get("tipo") == "validador"}

    def finalidad():
        """(altura, block_hash, validadores, umbral, últimas firmas): lo finalizado con los validadores de la cadena."""
        vigentes = validadores()
        ultimas = {v: int(a) for v, a in redis.zrange(contrato.FIRMAS_ULTIMA, 0, -1, withscores=True)}
        altura = contrato.altura_finalizada(vigentes, ultimas)
        VALIDADORES.set(len(vigentes))
        return altura, (redis.lindex(contrato.CADENA, altura) if altura >= 0 else None), vigentes, ultimas

    @app.post("/api/firmas")
    async def recibir_firma(request: Request):
        """Un validador firma `firma:<altura>:<block_hash>` del último bloque que verificó (POST, sin túnel). Tiene
        que tener un alta de tipo validador en la cadena y el bloque tiene que ser el de la cadena a esa altura."""
        datos = await leer_json(request)
        errores = ["el cuerpo no es JSON válido"] if datos is None else \
            await run_in_threadpool(contrato.validar_firma_bloque, datos)
        if errores:
            FIRMAS_RECIBIDAS.labels("invalida").inc()
            return invalido(errores)
        return await run_in_threadpool(anotar, datos)

    def anotar(datos):
        validador, altura = datos["validador"], datos["altura"]
        if validador not in validadores():
            FIRMAS_RECIBIDAS.labels("invalida").inc()
            return JSONResponse({"error": "esa clave no es un validador en la cadena"}, status_code=403)
        if redis.lindex(contrato.CADENA, altura) != datos["block_hash"]:
            FIRMAS_RECIBIDAS.labels("invalida").inc()
            return JSONResponse({"error": "ese no es el bloque de la cadena a esa altura"}, status_code=409)
        registro = json.dumps({"validador": validador, "altura": altura, "block_hash": datos["block_hash"],
                               "firma": datos["firma"], "cuando": int(time.time())})
        resultado = anotar_firma(keys=[contrato.FIRMAS.format(altura), contrato.FIRMAS_ULTIMA, contrato.FIRMAS_DOBLES],
                                 args=[validador, altura, registro, datos["block_hash"]])
        FIRMAS_RECIBIDAS.labels(resultado).inc()
        if resultado == "doble":
            log.error("DOBLE FIRMA: %s firmó dos bloques distintos a la altura %d (queda la prueba en %s)",
                      validador, altura, contrato.FIRMAS_DOBLES)
            return JSONResponse({"error": "ya firmaste otro bloque a esa altura: queda la prueba"}, status_code=409)
        final, hash_final, _, _ = finalidad()
        anterior = int(redis.hget(contrato.FINALIZADA, "altura") or -1)
        if final > anterior:
            redis.hset(contrato.FINALIZADA, mapping={"altura": final, "block_hash": hash_final})
            ALTURA_FINALIZADA.set(final)
            log.info("Bloque %d finalizado: %s", final, hash_final)
            try:
                publicador.avisar(contrato.EXCHANGE_EVENTOS, contrato.BLOQUE_FINALIZADO,
                                  {"altura": final, "block_hash": hash_final})
            except Exception as error:
                log.warning("No se pudo avisar la finalidad: %r", error)
        return {"resultado": resultado, "finalizada": max(final, anterior)}

    @app.get("/api/finalidad")
    def ver_finalidad():
        """La altura finalizada, los validadores de la cadena, el umbral y la última firma de cada uno."""
        altura, hash_, vigentes, ultimas = finalidad()
        firmas = {}
        for validador, ultima in ultimas.items():
            guardada = redis.hget(contrato.FIRMAS.format(ultima), validador)
            if guardada:
                firmas[validador] = json.loads(guardada)
        return {"altura": altura, "block_hash": hash_, "validadores": sorted(vigentes),
                "umbral": contrato.umbral_finalidad(len(vigentes)), "firmas": firmas,
                "dobles": [json.loads(d) for d in redis.lrange(contrato.FIRMAS_DOBLES, 0, 20)]}

    # ── BC-58: pedidos de alta y panel del organizador ──

    @app.post("/api/altas", status_code=202)
    async def pedir_alta(request: Request):
        """Público: {tipo, clave_publica, nombre, firma}. La firma demuestra que tiene la privada."""
        datos = await leer_json(request)
        cadena = await run_in_threadpool(genesis)
        errores = ["el cuerpo no es JSON válido"] if datos is None else \
            await run_in_threadpool(contrato.validar_pedido_alta, datos, cadena)
        if errores:
            log.info("Pedido de alta rechazado: %s", "; ".join(errores))
            return invalido(errores)
        cuenta_ = datos["clave_publica"]
        resultado = await run_in_threadpool(altas.pedir, cuenta_, datos["tipo"], datos["nombre"])
        if resultado == "aprobada":
            return JSONResponse({"error": "la cuenta ya está aprobada", "cuenta": cuenta_}, status_code=409)
        if resultado == "lleno":
            log.warning("Pedido de alta de %s rechazado: ya hay %d pendientes", cuenta_, altas_maximas)
            return JSONResponse({"error": "hay demasiados pedidos pendientes; probá más tarde"}, status_code=429)
        if resultado == "nueva":
            log.info("Pedido de alta de %s (%s): %s", datos["nombre"], datos["tipo"], cuenta_)
        return JSONResponse({"cuenta": cuenta_, "estado": "pendiente"}, status_code=202)

    def del_organizador(atender):
        """Envuelve una ruta del panel: el pedido tiene que venir firmado por ORGANIZADOR_CLAVE (si no, 403).

        La firma cubre método, ruta (con la query), hora y cuerpo, como los pedidos de los mineros.
        """
        async def pedido(request: Request):
            cuerpo = await request.body()
            if not organizador:
                return JSONResponse({"error": "este backend no tiene ORGANIZADOR_CLAVE: el panel está apagado"},
                                    status_code=403)
            firmada = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            cadena = await run_in_threadpool(genesis)
            quien, motivo = await run_in_threadpool(lambda: contrato.verificar_pedido(
                request.headers, request.method, firmada, cuerpo, None, contrato.ORGANIZADOR, cadena=cadena))
            if not motivo and quien != organizador:
                motivo = f"{contrato.ORGANIZADOR}: no es la cuenta del organizador"
            if motivo:
                log.warning("Pedido %s %s al panel rechazado: %s", request.method, firmada, motivo)
                return JSONResponse({"error": motivo}, status_code=403)
            try:
                datos = json.loads(cuerpo) if cuerpo else {}
            except ValueError:
                return invalido(["el cuerpo no es JSON válido"])
            if not isinstance(datos, dict):
                return invalido(["el cuerpo tiene que ser un objeto JSON"])
            return await atender(datos)
        return pedido

    def cuenta_del_pedido(datos):
        cuenta_ = datos.get("cuenta")
        return cuenta_ if contrato.cuenta_valida(cuenta_) else None

    async def listar_altas(datos):
        return {"pendientes": await run_in_threadpool(altas.pendientes)}

    async def listar_cuentas(datos):
        return {"cuentas": await run_in_threadpool(altas.aprobadas)}

    async def rechazar(datos):
        cuenta_ = cuenta_del_pedido(datos)
        if not cuenta_:
            return invalido(["cuenta: clave pública Ed25519 en hex (64 caracteres en minúscula)"])
        pedido = await run_in_threadpool(altas.rechazar, cuenta_)
        if pedido is None:
            return JSONResponse({"error": "no hay un pedido de alta de esa cuenta"}, status_code=404)
        log.info("El organizador rechazó el alta de %s (%s): %s", pedido.get("nombre"), pedido.get("tipo"), cuenta_)
        return {"cuenta": cuenta_, "rechazada": True}

    async def listar_suspendidos(datos):
        return {"suspendidos": await run_in_threadpool(altas.suspendidos)}

    async def reactivar(datos):
        """BC-75: un minero suspendido vuelve a recibir rangos (el organizador decidió que no mintió)."""
        cuenta_ = cuenta_del_pedido(datos)
        if not cuenta_:
            return invalido(["cuenta: clave pública Ed25519 en hex (64 caracteres en minúscula)"])
        suspension = await run_in_threadpool(altas.reactivar, cuenta_)
        if suspension is None:
            return JSONResponse({"error": "la cuenta no está suspendida"}, status_code=404)
        log.info("El organizador reactivó a %s (suspendido por %s): %s", suspension.get("nombre"),
                 suspension.get("motivo"), cuenta_)
        return {"cuenta": cuenta_, "reactivada": True}

    app.get("/api/organizador/altas")(del_organizador(listar_altas))
    app.get("/api/organizador/cuentas")(del_organizador(listar_cuentas))
    app.post("/api/organizador/rechazar")(del_organizador(rechazar))
    app.get("/api/organizador/suspendidos")(del_organizador(listar_suspendidos))
    app.post("/api/organizador/reactivar")(del_organizador(reactivar))

    @app.exception_handler(redislib.RedisError)
    def redis_caido(request, error):
        log.error("Redis no respondió en %s: %r", request.url.path, error)
        return JSONResponse({"error": "Redis no disponible"}, status_code=503)

    agregar_cors(app)


# BC-78: la billetera descargada se abre desde el disco (origen «null») y le habla a la API desde otro origen. Sólo
# las rutas públicas: las lecturas y lo que manda un cliente (transacciones y pedidos de alta), y lo que usa su modo
# organizador (BC-87, BC-88): los pendientes y los suspendidos, rechazar y reactivar, todos firmados con X-Organizador
# (la firma es la autorización: no hay cookies). Las firmas de los validadores (programas, no páginas) quedan afuera.
POST_PUBLICOS = ("/api/transacciones", "/api/transacciones/lote", "/api/altas")
DEL_ORGANIZADOR = {("GET", "/api/organizador/altas"), ("GET", "/api/organizador/suspendidos"),
                   ("POST", "/api/organizador/rechazar"), ("POST", "/api/organizador/reactivar")}
ENCABEZADOS_CORS = "content-type, x-organizador, x-tiempo, x-firma"


def es_publica(ruta, metodo):
    if (metodo, ruta) in DEL_ORGANIZADOR:
        return True
    if not ruta.startswith("/api/") or ruta.startswith("/api/organizador"):
        return False
    return metodo in ("GET", "HEAD") or (metodo == "POST" and ruta in POST_PUBLICOS)


def agregar_cors(app):
    @app.middleware("http")
    async def cors(request, siguiente):
        ruta = request.url.path
        if request.method == "OPTIONS":
            pedido = request.headers.get("access-control-request-method", "").upper()
            if es_publica(ruta, pedido):
                return Response(status_code=204, headers={
                    "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST",
                    "Access-Control-Allow-Headers": ENCABEZADOS_CORS, "Access-Control-Max-Age": "600"})
        respuesta = await siguiente(request)
        if es_publica(ruta, request.method):
            respuesta.headers["Access-Control-Allow-Origin"] = "*"
        return respuesta


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
                  organizador=leer_organizador())
    threading.Thread(target=servicio.rabbit.conectar, name="rabbit-conectar", daemon=True).start()
    servicio.servir_http(en_hilo=False)
