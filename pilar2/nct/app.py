"""NCT: el nodo coordinador de tareas (P4, BC-23).

Valida las transacciones (BC-24), las agrupa en cajas y publica las tareas de minado (BC-25),
y verifica, guarda y avisa cada bloque (BC-26). Las 2 réplicas corren lo mismo: se coordinan
sólo por Redis, con scripts Lua (atómicos) y SET NX.

A los scripts se les pasa todo como texto armado en Python: el Lua de Redis guarda los números
como double y escribiría 10^12 como «1e+12».
"""

import json
import logging
import threading
import time
import uuid

from prometheus_client import Gauge

from comun import Rabbit, Servicio, config, contrato

log = logging.getLogger("nct")

SALDOS_INICIALES = {"alice": 1000, "bob": 1000, "carol": 1000}

# Carga el génesis sólo si la cadena está vacía: con 2 réplicas arrancando juntas, entra uno.
# KEYS: cadena, saldos, bloque:<hash>. ARGV: block_hash, n campos, campo, valor…, usuario, saldo…
LUA_GENESIS = """
if redis.call('LLEN', KEYS[1]) > 0 then return 0 end
local n = tonumber(ARGV[2])
for i = 3, 2 + 2 * n, 2 do redis.call('HSET', KEYS[3], ARGV[i], ARGV[i + 1]) end
for i = 3 + 2 * n, #ARGV, 2 do redis.call('HSET', KEYS[2], ARGV[i], ARGV[i + 1]) end
redis.call('RPUSH', KEYS[1], ARGV[1])
return 1
"""

# Valida una transacción «recibida» (BC-24): pendiente si al origen le alcanza el saldo confirmado
# menos lo que ya tiene comprometido; si no, rechazada. Atómico: dos réplicas no pueden gastar dos
# veces el mismo saldo. Si ya no está «recibida» (mensaje reentregado), no cambia nada y devuelve
# el estado que tiene. Los montos se leen de tx:<id> (lo que guardó el backend), no del mensaje.
# KEYS: tx:<id>, saldos, comprometido, tx:pendientes. ARGV: id, motivo.
# Devuelve el estado (nil si la tx no existe).
LUA_VALIDAR = """
local actual = redis.call('HGET', KEYS[1], 'estado')
if actual ~= 'recibida' then return actual end
local origen = redis.call('HGET', KEYS[1], 'origen')
local monto = redis.call('HGET', KEYS[1], 'monto')
local saldo = tonumber(redis.call('HGET', KEYS[2], origen) or '0')
local comprometido = tonumber(redis.call('HGET', KEYS[3], origen) or '0')
if saldo - comprometido >= tonumber(monto) then
  redis.call('HSET', KEYS[1], 'estado', 'pendiente')
  redis.call('HINCRBY', KEYS[3], origen, monto)
  redis.call('RPUSH', KEYS[4], ARGV[1])
  return 'pendiente'
end
redis.call('HSET', KEYS[1], 'estado', 'rechazada', 'motivo', ARGV[2])
return 'rechazada'
"""
SALDO_INSUFICIENTE = "saldo insuficiente"

# Cierra cajas (BC-25): saca todo tx:pendientes y lo agrega al final de `cajas` en listas JSON de
# ids de como mucho N. Atómico: con dos réplicas, ninguna transacción queda en dos cajas.
# KEYS: tx:pendientes, cajas. ARGV: N. Devuelve cuántas cajas armó.
LUA_CERRAR_CAJAS = """
local ids = redis.call('LRANGE', KEYS[1], 0, -1)
if #ids == 0 then return 0 end
redis.call('DEL', KEYS[1])
local n, cajas = tonumber(ARGV[1]), 0
for i = 1, #ids, n do
  local caja = {}
  for j = i, math.min(i + n - 1, #ids) do caja[#caja + 1] = ids[j] end
  redis.call('RPUSH', KEYS[2], cjson.encode(caja))
  cajas = cajas + 1
end
return cajas
"""

# Suelta una marca (tarea:actual, el lock del ticker) sólo si sigue siendo la nuestra.
LUA_SOLTAR = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""

TAREA_ACTUAL, TICKER = contrato.TAREA_ACTUAL, "ticker"
COLA_TAREAS = contrato.COLAS[1]  # tareas
VENTANA, TX_POR_CAJA, TAREA_VENCE = 30, 100, 600

TX_PENDIENTES = Gauge("blockchain_tx_pendientes", "Transacciones validadas que esperan caja")
TX_EN_CAJAS = Gauge("blockchain_tx_en_cajas",
                    "Transacciones apiladas en cajas esperando bloque (las dos réplicas reportan lo mismo: usar max)")


def plano(diccionario):
    """{k: v} → [k1, v1, k2, v2, …] como texto, para pasarle a un script Lua."""
    return [str(x) for par in diccionario.items() for x in par]


def campos_bloque(bloque):
    """Un bloque como lo guarda `bloque:<hash>`: `transactions` en JSON."""
    return {**bloque, "transactions": json.dumps(bloque["transactions"])}


class Nct:
    def __init__(self, redis, publicador, saldos_iniciales=None, wait_replicas=1, wait_ms=1000,
                 ventana=VENTANA, tx_por_caja=TX_POR_CAJA, tarea_vence=TAREA_VENCE):
        self.redis, self.publicador = redis, publicador
        self.saldos_iniciales = SALDOS_INICIALES if saldos_iniciales is None else saldos_iniciales
        self.wait_replicas, self.wait_ms = wait_replicas, wait_ms
        self.ventana, self.tx_por_caja, self.tarea_vence = ventana, tx_por_caja, tarea_vence
        self._genesis = redis.register_script(LUA_GENESIS)
        self._validar = redis.register_script(LUA_VALIDAR)
        self._cerrar_cajas = redis.register_script(LUA_CERRAR_CAJAS)
        self._soltar = redis.register_script(LUA_SOLTAR)
        TX_PENDIENTES.set_function(self._contar_pendientes)
        TX_EN_CAJAS.set_function(self._contar_en_cajas)

    def cargar_genesis(self):
        """Si la cadena está vacía, carga el bloque génesis con los saldos iniciales. Devuelve si lo cargó."""
        bloque = contrato.bloque_genesis(self.saldos_iniciales, int(time.time()))
        campos = campos_bloque(bloque)
        cargado = self._genesis(
            keys=[contrato.CADENA, contrato.SALDOS, contrato.BLOQUE.format(bloque["block_hash"])],
            args=[bloque["block_hash"], len(campos), *plano(campos), *plano(self.saldos_iniciales)])
        if cargado:
            log.info("Génesis cargado: %s con %d usuarios", bloque["block_hash"], len(self.saldos_iniciales))
        return bool(cargado)

    def validar(self, mensaje):
        """BC-24: una transacción de `transacciones` pasa a pendiente o rechazada, y se avisa.

        Se avisa después de escribir y antes del ack. Si el pod muere en el medio (o falla el
        aviso), el mensaje vuelve, el script no cambia nada y el aviso se repite: es lo que
        hace que llegue, y repetido no le hace daño a nadie.
        """
        id_ = mensaje["id"]
        estado = self._validar(keys=[contrato.TX.format(id_), contrato.SALDOS, contrato.COMPROMETIDO,
                                     contrato.PENDIENTES], args=[id_, SALDO_INSUFICIENTE])
        if estado not in ("pendiente", "rechazada"):
            log.warning("La transacción %s no se valida: está %s", id_, estado or "ausente de Redis")
            return None
        aviso = {"id": id_, "estado": estado}
        if estado == "rechazada":
            aviso["motivo"] = SALDO_INSUFICIENTE
        self.publicador.avisar(contrato.EXCHANGE_EVENTOS, contrato.TRANSACCION_ESTADO, aviso)
        log.info("Transacción %s %s", id_, estado)
        return estado

    # ── BC-25: cajas y tareas ──

    def tic(self):
        """Lo que hace el ticker en cada ventana, una sola réplica por vez. Devuelve si le tocó."""
        marca = uuid.uuid4().hex
        if not self.redis.set(TICKER, marca, nx=True, ex=max(1, self.ventana - 1)):
            return False
        self.cerrar_cajas()
        self.armar_si_libre()
        return True

    def cerrar_cajas(self):
        cajas = self._cerrar_cajas(keys=[contrato.PENDIENTES, contrato.CAJAS], args=[self.tx_por_caja])
        if cajas:
            log.info("Se cerraron %d cajas", cajas)
        return cajas

    def armar_si_libre(self):
        """Si no hay tarea en curso y hay cajas, arma el bloque candidato con la primera y lo publica.

        La caja queda primera en `cajas` mientras se mina (la saca el consenso al confirmar): si
        el NCT muere, `tarea:actual` vence y la misma caja se vuelve a armar. Devuelve el id o None.
        """
        id_tarea = uuid.uuid4().hex
        if not self.redis.set(TAREA_ACTUAL, id_tarea, nx=True, ex=self.tarea_vence):
            return None
        caja = self.redis.lindex(contrato.CAJAS, 0)
        if caja is None:
            self._soltar(keys=[TAREA_ACTUAL], args=[id_tarea])
            return None
        self._publicar_tarea(id_tarea, json.loads(caja))
        return id_tarea

    def _publicar_tarea(self, id_tarea, caja):
        punta = self.redis.lindex(contrato.CADENA, -1)
        transacciones = self._transacciones(caja)
        timestamp = int(time.time())
        self.redis.hset(contrato.TAREA.format(id_tarea), mapping={
            "previous_hash": punta, "timestamp": timestamp, "transacciones": json.dumps(transacciones),
            "caja": json.dumps(caja), "estado": "en_curso", "publicada": time.time()})
        self.publicador.publicar(COLA_TAREAS, {"tarea": id_tarea, "previous_hash": punta,
                                               "transacciones": transacciones, "timestamp": timestamp})
        log.info("Tarea %s publicada: %d transacciones sobre %s", id_tarea, len(transacciones), punta)

    def _transacciones(self, ids):
        """Las transacciones de una caja, completas y en su orden, como van en el bloque."""
        tuberia = self.redis.pipeline(transaction=False)
        for id_ in ids:
            tuberia.hgetall(contrato.TX.format(id_))
        return [{"id": id_, "origen": tx["origen"], "destino": tx["destino"], "monto": int(tx["monto"]),
                 "timestamp": int(tx["timestamp"])} for id_, tx in zip(ids, tuberia.execute())]

    def _contar_pendientes(self):
        try:
            return self.redis.llen(contrato.PENDIENTES)
        except Exception:
            return float("nan")

    def _contar_en_cajas(self):
        try:
            return sum(len(json.loads(caja)) for caja in self.redis.lrange(contrato.CAJAS, 0, -1))
        except Exception:
            return float("nan")


def leer_saldos_iniciales(texto):
    """SALDOS_INICIALES: un JSON {usuario: saldo}; vacío, los de por defecto."""
    if not texto:
        return SALDOS_INICIALES
    saldos = json.loads(texto)
    if not isinstance(saldos, dict) or not all(contrato.usuario_valido(u) and isinstance(s, int) and s >= 0
                                              for u, s in saldos.items()):
        raise ValueError("SALDOS_INICIALES tiene que ser un JSON {usuario: saldo entero >= 0}")
    return saldos


def main():
    servicio = Servicio("nct", colas=contrato.COLAS)
    c = servicio.config
    publicador = Rabbit(c, contrato.COLAS)  # para publicar desde cualquier hilo (es segura entre hilos)
    nct = Nct(servicio.redis, publicador,
              saldos_iniciales=leer_saldos_iniciales(config.texto("SALDOS_INICIALES")),
              wait_replicas=config.entero("WAIT_REPLICAS", 1), wait_ms=config.entero("WAIT_MS", 1000),
              ventana=config.entero("VENTANA_SEGUNDOS", VENTANA), tx_por_caja=config.entero("TX_POR_CAJA", TX_POR_CAJA),
              tarea_vence=config.entero("TAREA_VENCE_SEGUNDOS", TAREA_VENCE))
    nct.cargar_genesis()
    servicio.servir_http(en_hilo=True)
    publicador.conectar()
    servicio.rabbit.conectar()  # la de /health
    consumir(c, contrato.COLAS[0], nct.validar)  # transacciones
    threading.Thread(target=tictac, args=(nct,), name="ticker", daemon=True).start()
    threading.Event().wait()


def tictac(nct):
    """El ticker: cada ventana intenta el tic (lo hace una sola réplica). Si Redis falla, sigue."""
    while True:
        try:
            nct.tic()
        except Exception:
            log.exception("Falló el tic; se reintenta en la próxima ventana")
        time.sleep(nct.ventana)


def consumir(c, cola, manejador):
    """Un hilo por cola, cada uno con su propia conexión (así lo pide comun.Rabbit)."""
    threading.Thread(target=Rabbit(c, contrato.COLAS).consumir, args=(cola, manejador),
                     name=f"consumir-{cola}", daemon=True).start()
