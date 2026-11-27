"""NCT: el nodo coordinador de tareas (P4, BC-23).

Valida las transacciones (BC-24, y desde BC-56 la firma y el nonce de cada cuenta), las agrupa en
cajas y publica las tareas de minado (BC-25), y verifica, guarda y avisa cada bloque (BC-26) con la
coinbase de su ganador adentro (BC-62). Al empezar cada época paga la participación por shares de
la anterior (BC-63). Las 2 réplicas corren lo mismo: se coordinan sólo por Redis, con scripts Lua
(atómicos) y SET NX.

Contrato v3 (BC-79, BC-80, BC-82): las altas, bajas y permisos son transacciones firmadas que entran en los
bloques; cada réplica sigue la cadena en memoria (comun/estado.py) y, antes de armar un bloque, saca de la caja lo
que no vale contra la cadena y pone la raíz de estado. `saldos`, `cuentas`, `nonces:confirmados` y `permisos` son
caché: al arrancar y cada tanto se comparan con la cadena, y si difieren gana la cadena.

A los scripts se les pasa todo como texto armado en Python: el Lua de Redis guarda los números
como double y escribiría 10^12 como «1e+12».
"""

import json
import logging
import threading
import time
import uuid

from prometheus_client import Counter, Gauge, Histogram
from redis import exceptions as redis_errores

from comun import Rabbit, Servicio, config, contrato, estado as estado_cadena

log = logging.getLogger("nct")

# Sin saldos: en la red la plata entra por cargas del organizador (BC-58). Los tests y el k3s
# pasan SALDOS_INICIALES con las cuentas de prueba.
SALDOS_INICIALES = {}

# Carga el génesis sólo si la cadena está vacía: con 2 réplicas arrancando juntas, entra uno. El
# organizador y las cuentas del génesis quedan en `cuentas` (el caché de lo que dice el bloque 0).
# KEYS: cadena, saldos, bloque:<hash>, cuentas.
# ARGV: block_hash, n campos, campo, valor…, n saldos, (cuenta, saldo)…, (cuenta, datos de la cuenta)…
LUA_GENESIS = """
if redis.call('LLEN', KEYS[1]) > 0 then return 0 end
local n = tonumber(ARGV[2])
for i = 3, 2 + 2 * n, 2 do redis.call('HSET', KEYS[3], ARGV[i], ARGV[i + 1]) end
local s = tonumber(ARGV[3 + 2 * n])
local primera = 4 + 2 * n
for i = primera, primera + 2 * s - 1, 2 do redis.call('HSET', KEYS[2], ARGV[i], ARGV[i + 1]) end
for i = primera + 2 * s, #ARGV, 2 do redis.call('HSET', KEYS[4], ARGV[i], ARGV[i + 1]) end
redis.call('RPUSH', KEYS[1], ARGV[1])
return 1
"""

# Valida una transacción «recibida» (BC-24 y BC-56). Atómico: dos réplicas no pueden gastar dos
# veces el mismo saldo ni aceptar dos veces el mismo nonce. Los datos se leen de tx:<id> (lo que
# guardó el backend), no del mensaje. Si ya no está «recibida» (mensaje reentregado), no cambia nada.
#   - La firma la verifica Python antes: si falló, ARGV[2] trae el motivo y queda rechazada.
#   - Una transferencia necesita la cuenta origen aprobada (`cuentas`); una carga o un acta del
#     organizador (origen «emision») no mira saldo ni cuenta, sólo que su número siga al último. Un acta
#     delegada (ARGV[4] = 1) tampoco mira la cuenta: el permiso lo verificó Python antes.
#   - El nonce tiene que ser el último aceptado + 1. Uno menor o igual es un reenvío («nonce
#     repetido»). Uno mayor llegó antes que el anterior (dos réplicas consumen en paralelo): queda en
#     espera:<cuenta> hasta que entre el que falta; el ticker rechaza las que esperan de más.
#   - Al aceptar una, se destraban las que esperaban el nonce siguiente.
#   - BC-93: un alta de una cuenta que ya está en `cuentas` o con otra alta en camino se rechaza; si no, queda en
#     `altas:en_camino` en el mismo paso (dos altas seguidas, en dos réplicas o destrabadas de la espera, no pasan).
# KEYS: tx:<id>, saldos, comprometido, tx:pendientes, nonces, cuentas, tx:en_espera, altas:en_camino.
# ARGV: id, motivo si la firma no sirve (o vacío), ahora, 1 si es un acta, motivo de un alta repetida.
# Devuelve {estado de la tx, (id, estado, motivo) de cada una que cambió…}.
LUA_VALIDAR = """
local cambios = {}
local function anotar(id, estado, motivo)
  cambios[#cambios + 1] = id; cambios[#cambios + 1] = estado; cambios[#cambios + 1] = motivo
end
local function rechazar(id, motivo)
  redis.call('HSET', 'tx:' .. id, 'estado', 'rechazada', 'motivo', motivo)
  anotar(id, 'rechazada', motivo)
end
local function aceptar(id)
  local clave = 'tx:' .. id
  local origen, monto, nonce = unpack(redis.call('HMGET', clave, 'origen', 'monto', 'nonce'))
  if string.sub(id, 1, 5) == 'alta-' then
    local destino, datos = unpack(redis.call('HMGET', clave, 'destino', 'datos'))
    if redis.call('HEXISTS', KEYS[6], destino) == 1 or redis.call('HEXISTS', KEYS[8], destino) == 1 then
      rechazar(id, ARGV[5])
      return false
    end
    local corte = string.find(datos or '', ':', 1, true)
    local tipo = corte and string.sub(datos, 1, corte - 1) or (datos or '')
    local nombre = corte and string.sub(datos, corte + 1) or ''
    redis.call('HSET', KEYS[8], destino, cjson.encode({tipo = tipo, nombre = nombre, tx = id}))
  end
  if origen ~= 'emision' and tonumber(monto) > 0 then
    local saldo = tonumber(redis.call('HGET', KEYS[2], origen) or '0')
    local comprometido = tonumber(redis.call('HGET', KEYS[3], origen) or '0')
    if saldo - comprometido < tonumber(monto) then
      rechazar(id, 'saldo insuficiente')
      return false
    end
    redis.call('HINCRBY', KEYS[3], origen, monto)
  end
  redis.call('HSET', clave, 'estado', 'pendiente')
  redis.call('HSET', KEYS[5], origen, nonce)
  redis.call('RPUSH', KEYS[4], id)
  anotar(id, 'pendiente', '')
  return true
end

local id = ARGV[1]
local estado = redis.call('HGET', KEYS[1], 'estado')
if estado ~= 'recibida' then return {estado or ''} end
if ARGV[2] ~= '' then rechazar(id, ARGV[2]) return {'rechazada', unpack(cambios)} end
local origen, nonce = unpack(redis.call('HMGET', KEYS[1], 'origen', 'nonce'))
nonce = tonumber(nonce)
if origen ~= 'emision' and ARGV[4] ~= '1' and redis.call('HEXISTS', KEYS[6], origen) == 0 then
  rechazar(id, 'cuenta no aprobada')
  return {'rechazada', unpack(cambios)}
end
local ultimo = tonumber(redis.call('HGET', KEYS[5], origen) or '0')
if nonce <= ultimo then rechazar(id, 'nonce repetido') return {'rechazada', unpack(cambios)} end
local espera = 'espera:' .. origen
if nonce > ultimo + 1 then
  redis.call('ZADD', espera, nonce, id)
  redis.call('ZADD', KEYS[7], ARGV[3], id)
  return {'en_espera'}
end
local propio = aceptar(id) and 'pendiente' or 'rechazada'
while true do
  local siguiente = tonumber(redis.call('HGET', KEYS[5], origen) or '0') + 1
  local listos = redis.call('ZRANGEBYSCORE', espera, '-inf', siguiente)
  if #listos == 0 then break end
  local avanzo = false
  for _, otro in ipairs(listos) do
    redis.call('ZREM', espera, otro)
    redis.call('ZREM', KEYS[7], otro)
    if redis.call('HGET', 'tx:' .. otro, 'estado') == 'recibida' then
      if avanzo or tonumber(redis.call('HGET', 'tx:' .. otro, 'nonce')) < siguiente then
        rechazar(otro, 'nonce repetido')
      elseif aceptar(otro) then
        avanzo = true
      end
    end
  end
  if not avanzo then break end
end
return {propio, unpack(cambios)}
"""
SALDO_INSUFICIENTE, FIRMA_INVALIDA, NONCE_SALTEADO = "saldo insuficiente", "firma inválida", "nonce salteado"

# Rechaza las transacciones que esperan un nonce anterior desde antes de ARGV[1]: el que faltaba no
# llegó nunca (o lo rechazaron). KEYS: tx:en_espera. ARGV: límite, motivo. Devuelve los ids rechazados.
LUA_VENCER_ESPERA = """
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, 1000)
local rechazadas = {}
for _, id in ipairs(ids) do
  redis.call('ZREM', KEYS[1], id)
  local origen = redis.call('HGET', 'tx:' .. id, 'origen')
  if origen then redis.call('ZREM', 'espera:' .. origen, id) end
  if redis.call('HGET', 'tx:' .. id, 'estado') == 'recibida' then
    redis.call('HSET', 'tx:' .. id, 'estado', 'rechazada', 'motivo', ARGV[2])
    rechazadas[#rechazadas + 1] = id
  end
end
return rechazadas
"""

# Cierra una caja (BC-25, BC-89): sólo si la fila `cajas` está vacía, saca las primeras N de tx:pendientes y
# las pone como una lista JSON de ids. Mientras se mina un bloque, lo que llega espera en tx:pendientes; al
# confirmarse, la caja siguiente se lleva todo lo acumulado (hasta N). Hasta BC-89 se cerraban cajas de N cada
# ventana y se minaba una por bloque: con una ráfaga la fila crecía sin techo. Atómico: con dos réplicas,
# ninguna transacción queda en dos cajas ni hay dos cajas a la vez.
# KEYS: tx:pendientes, cajas. ARGV: N. Devuelve cuántas cajas armó (0 o 1).
LUA_CERRAR_CAJAS = """
if redis.call('LLEN', KEYS[2]) > 0 then return 0 end
local ids = redis.call('LRANGE', KEYS[1], 0, tonumber(ARGV[1]) - 1)
if #ids == 0 then return 0 end
redis.call('LTRIM', KEYS[1], #ids, -1)
redis.call('RPUSH', KEYS[2], cjson.encode(ids))
return 1
"""

# Suelta una marca (tarea:actual, el lock del ticker) sólo si sigue siendo la nuestra.
LUA_SOLTAR = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""

# Saca la primera caja de `cajas` sólo si sigue siendo ésa (otra réplica pudo haberla sacado ya).
# KEYS: cajas. ARGV: la caja, tal cual está en la lista.
LUA_SACAR_CAJA = """
if redis.call('LINDEX', KEYS[1], 0) == ARGV[1] then redis.call('LPOP', KEYS[1]) return 1 end
return 0
"""

# El consenso (BC-26, NCT.4): agrega el bloque sólo si la tarea sigue en curso, su previous_hash
# sigue siendo la punta de `cadena` y ninguna de sus transacciones está ya en la cadena (si no,
# sería un doble gasto, o un pago de participación repetido). Todo en una sola escritura atómica:
# con 2 réplicas nunca entran dos bloques sobre la misma punta, y una solución repetida no guarda
# nada dos veces.
# El caché del estado (v3) sale de las transacciones del bloque, según su efecto (lo calcula Python):
#   transferencia: le resta al origen, le suma al destino y baja lo comprometido;
#   emitir: le suma al destino (coinbase, participación o carga);
#   alta / baja: la cuenta entra o sale de `cuentas` (y de los pedidos y las altas en camino);
#   permiso: la clave entra o sale de `permisos`; nada: shares de la época.
# Y en `nonces:confirmados`, el nonce de quien firmó. Se anota el historial (BC-54). Cada tx:<id> queda
# como puntero, {estado: confirmada, bloque} (BC-70): su contenido ya está en el bloque. La coinbase,
# la participación y los shares no tenían tx:<id>: nacen así.
# KEYS: tarea:<id>, cadena, cajas, saldos, comprometido, tarea:actual, bloque:<hash>.
# ARGV: id_tarea, block_hash, n campos, campo, valor…,
#       (id, efecto, origen, destino, monto, nonce_de, nonce, extra)…,
#       los campos que se le sacan a tx:<id> y, último, cuántos son.
# Devuelve {resultado, block_hash, altura}: «confirmada», «repetida» (ya estaba confirmada: con su
# bloque, para repetir el aviso) o «descartada» (la tarea no seguía en curso, cambió la punta o
# alguna transacción ya estaba confirmada en otro bloque).
LUA_CONSENSO = """
local estado = redis.call('HGET', KEYS[1], 'estado')
if estado == 'confirmada' then
  local hash = redis.call('HGET', KEYS[1], 'block_hash')
  return {'repetida', hash, redis.call('HGET', 'bloque:' .. hash, 'altura')}
end
if estado ~= 'en_curso' then return {'descartada'} end
local function descartar()
  redis.call('HSET', KEYS[1], 'estado', 'descartada')
  if redis.call('GET', KEYS[6]) == ARGV[1] then redis.call('DEL', KEYS[6]) end
  return {'descartada'}
end
if redis.call('LINDEX', KEYS[2], -1) ~= redis.call('HGET', KEYS[1], 'previous_hash') then return descartar() end
local n = tonumber(ARGV[3])
local primera = 4 + 2 * n
local k = tonumber(ARGV[#ARGV])  -- los campos que se le sacan a tx:<id>, y su cantidad al final
local ultima = #ARGV - k - 1
local copiados = {unpack(ARGV, ultima + 1, #ARGV - 1)}
for i = primera, ultima, 8 do
  if redis.call('HGET', 'tx:' .. ARGV[i], 'estado') == 'confirmada' then return descartar() end
end
local altura = redis.call('LLEN', KEYS[2])
for i = 4, 3 + 2 * n, 2 do redis.call('HSET', KEYS[7], ARGV[i], ARGV[i + 1]) end
redis.call('HSET', KEYS[7], 'altura', altura)
redis.call('RPUSH', KEYS[2], ARGV[2])
for i = primera, ultima, 8 do
  local id, efecto, origen, destino, monto = ARGV[i], ARGV[i + 1], ARGV[i + 2], ARGV[i + 3], ARGV[i + 4]
  local nonce_de, nonce, extra = ARGV[i + 5], ARGV[i + 6], ARGV[i + 7]
  if efecto == 'transferencia' then
    redis.call('HINCRBY', KEYS[4], destino, monto)
    redis.call('HINCRBY', KEYS[4], origen, -tonumber(monto))
    redis.call('HINCRBY', KEYS[5], origen, -tonumber(monto))
  elseif efecto == 'emitir' then
    redis.call('HINCRBY', KEYS[4], destino, monto)
    if string.sub(id, 1, 3) == 'cb-' then redis.call('HINCRBY', 'historial:' .. destino, 'bloques', 1) end
    if string.sub(id, 1, 6) ~= 'carga-' then redis.call('HINCRBY', 'historial:' .. destino, 'recompensas', monto) end
  elseif efecto == 'alta' then
    redis.call('HSET', 'cuentas', destino, extra)
    redis.call('HDEL', 'altas:pendientes', destino)
    redis.call('HDEL', 'altas:en_camino', destino)
  elseif efecto == 'baja' then
    redis.call('HDEL', 'cuentas', destino)
    redis.call('HDEL', 'suspendidos', destino)
    redis.call('HDEL', 'altas:en_camino', destino)
  elseif efecto == 'permiso' then
    if extra == '' then redis.call('HDEL', 'permisos', destino) else redis.call('HSET', 'permisos', destino, extra) end
  end
  if nonce_de ~= '' then redis.call('HSET', 'nonces:confirmados', nonce_de, nonce) end
  local clave = 'tx:' .. id
  redis.call('HDEL', clave, unpack(copiados))
  redis.call('HSET', clave, 'estado', 'confirmada', 'bloque', ARGV[2])
end
if redis.call('LINDEX', KEYS[3], 0) == redis.call('HGET', KEYS[1], 'caja') then redis.call('LPOP', KEYS[3]) end
redis.call('HSET', KEYS[1], 'estado', 'confirmada', 'block_hash', ARGV[2])
if redis.call('GET', KEYS[6]) == ARGV[1] then redis.call('DEL', KEYS[6]) end
return {'confirmada', ARGV[2], altura}
"""

# Una transacción validada («pendiente») que al armar el bloque no vale contra la cadena (v3): queda
# rechazada, se le devuelve lo comprometido y el nonce de quien firmó vuelve atrás, así puede firmar
# otra con ese número. Si era un alta, deja de estar en camino.
# KEYS: tx:<id>, comprometido, nonces, altas:en_camino. ARGV: motivo, id. Devuelve 1 si la rechazó.
LUA_RECHAZAR_PENDIENTE = """
if redis.call('HGET', KEYS[1], 'estado') ~= 'pendiente' then return 0 end
local origen, destino, monto, nonce = unpack(redis.call('HMGET', KEYS[1], 'origen', 'destino', 'monto', 'nonce'))
redis.call('HSET', KEYS[1], 'estado', 'rechazada', 'motivo', ARGV[1])
if origen ~= 'emision' and tonumber(monto) > 0 then redis.call('HINCRBY', KEYS[2], origen, -tonumber(monto)) end
local ultimo = tonumber(redis.call('HGET', KEYS[3], origen) or '0')
if ultimo >= tonumber(nonce) then redis.call('HSET', KEYS[3], origen, tonumber(nonce) - 1) end
if string.sub(ARGV[2], 1, 5) == 'alta-' then redis.call('HDEL', KEYS[4], destino) end
return 1
"""

# Recalcula `comprometido` desde las transacciones pendientes (en tx:pendientes y en las cajas).
# KEYS: tx:pendientes, cajas, comprometido. Devuelve cuántas cuentas cambiaron.
LUA_COMPROMETIDO = """
local suma = {}
local function contar(id)
  local estado, origen, monto = unpack(redis.call('HMGET', 'tx:' .. id, 'estado', 'origen', 'monto'))
  if estado == 'pendiente' and origen ~= 'emision' and tonumber(monto) > 0 then
    suma[origen] = (suma[origen] or 0) + tonumber(monto)
  end
end
for _, id in ipairs(redis.call('LRANGE', KEYS[1], 0, -1)) do contar(id) end
for _, caja in ipairs(redis.call('LRANGE', KEYS[2], 0, -1)) do
  for _, id in ipairs(cjson.decode(caja)) do contar(id) end
end
local actual, cambios = {}, 0
local guardado = redis.call('HGETALL', KEYS[3])
for i = 1, #guardado, 2 do actual[guardado[i]] = tonumber(guardado[i + 1]) end
for cuenta, monto in pairs(actual) do if (suma[cuenta] or 0) ~= monto then cambios = cambios + 1 end end
for cuenta, _ in pairs(suma) do if actual[cuenta] == nil then cambios = cambios + 1 end end
if cambios > 0 then
  redis.call('DEL', KEYS[3])
  for cuenta, monto in pairs(suma) do redis.call('HSET', KEYS[3], cuenta, string.format('%d', monto)) end
end
return cambios
"""

# Cierra una tarea en curso que no terminó en bloque («sin_solucion» o «rechazada») y le pasa la
# marca de tarea actual a la nueva, que va a minar la misma caja. Si la tarea ya no estaba en curso
# (un aviso tardío) o la marca es de otra tarea, no hace nada.
# KEYS: tarea:<vieja>, tarea:actual. ARGV: vieja, nueva, estado, vencimiento. Devuelve 1 si rearma.
LUA_REARMAR = """
if redis.call('HGET', KEYS[1], 'estado') ~= 'en_curso' then return 0 end
local actual = redis.call('GET', KEYS[2])
if actual and actual ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[1], 'estado', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[4])
return 1
"""

# Releva a la tarea anterior: la nueva pasa a ser la última y, si la anterior sigue en curso (venció
# tarea:actual sin bloque), queda «vencida». Así el gateway deja de prestar sus rangos y les dice
# «cortá» a los mineros que la tienen, y si después llega su solución el consenso la descarta.
# KEYS: tarea:ultima. ARGV: nueva. Devuelve el id de la que venció, o nada.
LUA_RELEVAR = """
local vieja = redis.call('GET', KEYS[1])
redis.call('SET', KEYS[1], ARGV[1])
if vieja and vieja ~= ARGV[1] and redis.call('HGET', 'tarea:' .. vieja, 'estado') == 'en_curso' then
  redis.call('HSET', 'tarea:' .. vieja, 'estado', 'vencida')
  return vieja
end
return false
"""

# Cuándo se armó por primera vez la caja que se está minando (BC-73). Si la caja es la misma de la vez
# anterior (se rearmó: sin solución, rechazada, vencida o cambió la punta), devuelve esa hora; si es otra,
# anota ésta. Así la `duracion` del bloque cuenta todos los intentos, y el TrP sabe hace cuánto que no
# sale un bloque (la bajada de emergencia).
# KEYS: caja:armada. ARGV: la caja (tal cual está en `cajas`), ahora. Devuelve la hora del primer armado.
LUA_ARMADA = """
if redis.call('HGET', KEYS[1], 'caja') == ARGV[1] then return redis.call('HGET', KEYS[1], 'desde') end
redis.call('HSET', KEYS[1], 'caja', ARGV[1], 'desde', ARGV[2])
return ARGV[2]
"""

TAREA_ACTUAL, TICKER = contrato.TAREA_ACTUAL, "ticker"
COLA_TAREAS = contrato.COLAS[1]  # tareas
VENTANA, TX_POR_CAJA, TAREA_VENCE, ESPERA_NONCE = 30, 1000, 600, 60

BLOQUES = Counter("blockchain_bloques", "Bloques confirmados por esta réplica del NCT")
TIEMPO_BLOQUE = Histogram("blockchain_tiempo_bloque_segundos", "Desde que se arma la caja hasta que su bloque se confirma",
                          buckets=(1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600))
SIN_REPLICA = Counter("blockchain_bloques_sin_replica",
                      "Bloques avisados sin que una réplica de Redis confirmara tenerlos a tiempo")
TX_PENDIENTES = Gauge("blockchain_tx_pendientes", "Transacciones validadas que esperan caja")
TX_EN_CAJAS = Gauge("blockchain_tx_en_cajas",
                    "Transacciones apiladas en cajas esperando bloque (las dos réplicas reportan lo mismo: usar max)")
TX_POR_BLOQUE = Gauge("blockchain_tx_por_bloque_maximo", "Transacciones por bloque como mucho (TX_POR_CAJA): dividido "
                      "el tiempo esperado por bloque, la capacidad del sistema en tx/s")
TX_RECHAZADAS = Counter("blockchain_tx_rechazadas", "Transacciones rechazadas por el NCT", ["motivo"])
# v3: el caché contra la cadena.
DIVERGENCIAS = Counter("blockchain_estado_divergencias", "Valores del caché de Redis que no coincidían con la cadena "
                       "(y se corrigieron)", ["campo"])
CADENA_PROBLEMAS = Counter("blockchain_cadena_problemas", "Bloques de la cadena que no cumplen las reglas del "
                           "contrato (alguien los tocó en Redis)")
SACADAS = Counter("blockchain_tx_sacadas_de_caja", "Transacciones validadas que no valían contra la cadena al "
                  "armar el bloque (el caché estaba mal o la cuenta se dio de baja)")


def plano(diccionario):
    """{k: v} → [k1, v1, k2, v2, …] como texto, para pasarle a un script Lua."""
    return [str(x) for par in diccionario.items() for x in par]


def movimiento(tx):
    """Los 8 valores de una transacción del bloque para LUA_CONSENSO: id, efecto, origen, destino, monto,
    de quién es el nonce (o vacío), el nonce y el extra (los datos de la cuenta en un alta, el prefijo en un permiso)."""
    tipo = contrato.tipo_de(tx)
    firmante = contrato.EMISION if tx["origen"] == contrato.EMISION else tx["origen"]
    efecto, nonce_de, extra = "nada", "", ""
    if tipo == "transferencia":
        efecto, nonce_de = "transferencia", tx["origen"]
    elif tipo in ("coinbase", "participacion"):
        efecto = "emitir"
    elif tipo == "carga":
        efecto, nonce_de = "emitir", contrato.EMISION
    elif tipo == "alta":
        tipo_cuenta, nombre = contrato.datos_alta(tx)
        efecto, nonce_de = "alta", firmante
        extra = json.dumps({"tipo": tipo_cuenta, "nombre": nombre, "alta": tx["timestamp"]})
    elif tipo == "baja":
        efecto, nonce_de = "baja", firmante
    elif tipo == "permiso":
        efecto, nonce_de, extra = "permiso", firmante, tx.get("datos", "")
    return [str(x) for x in (tx["id"], efecto, tx["origen"], tx["destino"], tx["monto"], nonce_de, tx["nonce"], extra)]


def campos_bloque(bloque):
    """Un bloque como lo guarda `bloque:<hash>`: `transactions` en JSON."""
    return {**bloque, "transactions": json.dumps(bloque["transactions"])}


def datos_tarea(tarea, transacciones):
    """tarea:<id> como lo devuelve Redis → lo que necesita contrato.texto_para."""
    return {"previous_hash": tarea["previous_hash"], "timestamp": int(tarea["timestamp"]),
            "altura": int(tarea["altura"]), "prefijo": tarea["prefijo"], "transacciones": transacciones,
            "raiz_estado": tarea["raiz_estado"]}


class Nct:
    def __init__(self, redis, publicador, saldos_iniciales=None, organizador="", wait_replicas=1, wait_ms=1000,
                 ventana=VENTANA, tx_por_caja=TX_POR_CAJA, tarea_vence=TAREA_VENCE, espera_nonce=ESPERA_NONCE,
                 fondo_epoca=contrato.FONDO_EPOCA, tarea_guarda=contrato.TAREA_GUARDA,
                 emergencia=contrato.EMERGENCIA_SEGUNDOS, revisar_cada=60, snapshot_cada=100):
        self.redis, self.publicador = redis, publicador
        self.saldos_iniciales = SALDOS_INICIALES if saldos_iniciales is None else saldos_iniciales
        self.organizador = organizador
        self.wait_replicas, self.wait_ms = wait_replicas, wait_ms
        self.ventana, self.tx_por_caja, self.tarea_vence = ventana, tx_por_caja, tarea_vence
        self.espera_nonce, self.fondo_epoca, self.tarea_guarda = espera_nonce, fondo_epoca, tarea_guarda
        # BC-73: una tarea sin bloque vence a los `emergencia` segundos (0 = sólo a los `tarea_vence`) y se
        # rearma la misma caja; el TrP le baja un carácter por cada `emergencia` desde que se armó la caja.
        self.emergencia = emergencia
        # v3: la cadena en memoria (verificando firmas sólo al arrancar) y cada cuánto se compara el caché con ella.
        self.revisar_cada, self._ultima_revision = revisar_cada, 0.0
        self.reglas = estado_cadena.Reglas(organizador=organizador, fondo_epoca=fondo_epoca,
                                           shares=estado_cadena.shares_de_redis(redis), verificar_firmas=False)
        self.seguidor = estado_cadena.Seguidor(redis, self.reglas, snapshot_cada=snapshot_cada,
                                               al_problema=self._problema_en_la_cadena)
        self._rechazar_pendiente = redis.register_script(LUA_RECHAZAR_PENDIENTE)
        self._comprometido = redis.register_script(LUA_COMPROMETIDO)
        self._armada = redis.register_script(LUA_ARMADA)
        self._genesis = redis.register_script(LUA_GENESIS)
        self._validar = redis.register_script(LUA_VALIDAR)
        self._vencer_espera = redis.register_script(LUA_VENCER_ESPERA)
        self._cerrar_cajas = redis.register_script(LUA_CERRAR_CAJAS)
        self._soltar = redis.register_script(LUA_SOLTAR)
        self._sacar_caja = redis.register_script(LUA_SACAR_CAJA)
        self._consenso = redis.register_script(LUA_CONSENSO)
        self._rearmar = redis.register_script(LUA_REARMAR)
        self._relevar = redis.register_script(LUA_RELEVAR)
        TX_PENDIENTES.set_function(self._contar_pendientes)
        TX_EN_CAJAS.set_function(self._contar_en_cajas)
        TX_POR_BLOQUE.set(tx_por_caja)

    def cargar_genesis(self):
        """Si la cadena está vacía, carga el génesis con los saldos iniciales y aprueba esas cuentas.

        `saldos_iniciales`: {cuenta: saldo} o {cuenta: {"saldo": n, "nombre": alias}}. Devuelve si lo cargó.
        """
        timestamp = int(time.time())
        saldos = {cuenta: d["saldo"] if isinstance(d, dict) else d for cuenta, d in self.saldos_iniciales.items()}
        nombres = {cuenta: d.get("nombre", "") for cuenta, d in self.saldos_iniciales.items() if isinstance(d, dict)}
        bloque = contrato.bloque_genesis(saldos, timestamp, organizador=self.organizador, nombres=nombres)
        campos = campos_bloque(bloque)
        cuentas = {cuenta: {"tipo": "usuario", "nombre": nombres.get(cuenta, ""), "alta": timestamp}
                   for cuenta in saldos}
        if self.organizador:
            cuentas[self.organizador] = {"tipo": "organizador", "nombre": "organizador", "alta": timestamp}
        cargado = self._genesis(
            keys=[contrato.CADENA, contrato.SALDOS, contrato.BLOQUE.format(bloque["block_hash"]), contrato.CUENTAS],
            args=[bloque["block_hash"], len(campos), *plano(campos), len(saldos), *plano(dict(sorted(saldos.items()))),
                  *plano({c: json.dumps(d) for c, d in sorted(cuentas.items())})])
        if cargado:
            log.info("Génesis cargado: %s con %d cuentas", bloque["block_hash"], len(saldos))
        return bool(cargado)

    # ── BC-24 y BC-56: validar ──

    def validar(self, mensaje):
        """Una transacción de `transacciones` pasa a pendiente o rechazada (o espera su turno), y se avisa.

        Se avisa después de escribir y antes del ack. Si el pod muere en el medio (o falla el
        aviso), el mensaje vuelve, el script no cambia nada y el aviso se repite: es lo que
        hace que llegue, y repetido no le hace daño a nadie. Devuelve el estado de la transacción.
        """
        id_ = mensaje["id"]
        guardada = self.redis.hgetall(contrato.TX.format(id_))
        if not guardada:
            # El backend la guarda en Redis antes de publicarla: si no está, este NCT le habla a un
            # Redis que no es el primario de verdad (uno que acaba de reiniciar, en un failover).
            # Se sueltan las conexiones, para volver a preguntarle a Sentinel, y el mensaje vuelve a
            # la cola (comun/rabbit.py). Las colas quorum lo descartan tras 20 entregas.
            self.redis.connection_pool.disconnect()
            raise redis_errores.ConnectionError(f"la transacción {id_} no está en Redis")
        problema = "" if guardada.get("estado") != "recibida" else self._problema(id_, guardada)
        acta = "1" if contrato.es_acta({"id": id_, "origen": guardada.get("origen")}) else "0"
        estado, *cambios = self._validar(
            keys=[contrato.TX.format(id_), contrato.SALDOS, contrato.COMPROMETIDO, contrato.PENDIENTES,
                  contrato.NONCES, contrato.CUENTAS, contrato.EN_ESPERA, contrato.ALTAS_EN_CAMINO],
            args=[id_, problema, time.time(), acta, estado_cadena.ALTA_REPETIDA])
        if estado == "en_espera":
            log.info("Transacción %s: su nonce llegó antes que el anterior, espera su turno", id_)
            return estado
        if not cambios and estado in ("pendiente", "rechazada"):
            # Un mensaje reentregado: no cambió nada, pero se repite el aviso por si se perdió.
            aviso = {"id": id_, "estado": estado}
            if estado == "rechazada":
                aviso["motivo"] = guardada.get("motivo", "")
            cambios = [id_, estado, aviso.get("motivo", "")]
        elif not cambios:
            log.warning("La transacción %s no se valida: está %s", id_, estado or "sin estado")
            return None
        for i in range(0, len(cambios), 3):
            self._avisar_estado(*cambios[i:i + 3])
        return estado

    def _problema(self, id_, guardada):
        """El motivo por el que no vale (firma, o las reglas de un acta contra el caché), o vacío."""
        try:
            tx = contrato.transaccion_guardada(id_, guardada)
        except (KeyError, ValueError):
            return FIRMA_INVALIDA  # le falta un campo o no es un número: no puede tener una firma que sirva
        if contrato.es_acta(tx):
            if contrato.validar_acta(tx, self.organizador):
                return FIRMA_INVALIDA
            return estado_cadena.problema_de_acta(tx, self._cuenta_en_cache, self._permiso_en_cache,
                                                  self.organizador) or ""
        if contrato.es_emision(tx):
            return "" if not contrato.validar_carga(tx, self.organizador) else FIRMA_INVALIDA
        return "" if not contrato.validar_transaccion_firmada(tx) else FIRMA_INVALIDA

    def _cuenta_en_cache(self, cuenta):
        datos = self.redis.hget(contrato.CUENTAS, cuenta)
        return json.loads(datos) if datos else None

    def _permiso_en_cache(self, clave):
        return self.redis.hget(contrato.PERMISOS, clave)

    def _avisar_estado(self, id_, estado, motivo=""):
        aviso = {"id": id_, "estado": estado}
        if estado == "rechazada":
            aviso["motivo"] = motivo
            TX_RECHAZADAS.labels(motivo).inc()
            log.info("Transacción %s rechazada: %s", id_, motivo)
        else:
            log.info("Transacción %s %s", id_, estado)
        self.publicador.avisar(contrato.EXCHANGE_EVENTOS, contrato.TRANSACCION_ESTADO, aviso)

    def vencer_espera(self, ahora=None):
        """Rechaza las que esperan un nonce anterior desde hace más de `espera_nonce` segundos."""
        limite = (time.time() if ahora is None else ahora) - self.espera_nonce
        rechazadas = self._vencer_espera(keys=[contrato.EN_ESPERA], args=[limite, NONCE_SALTEADO])
        for id_ in rechazadas:
            self._avisar_estado(id_, "rechazada", NONCE_SALTEADO)
        return rechazadas

    # ── BC-25: cajas y tareas ──

    def tic(self):
        """Lo que hace el ticker en cada ventana, una sola réplica por vez. Devuelve si le tocó."""
        marca = uuid.uuid4().hex
        if not self.redis.set(TICKER, marca, nx=True, ex=max(1, self.ventana - 1)):
            return False
        self.vencer_espera()
        if self.revisar_cada and time.time() - self._ultima_revision >= self.revisar_cada:
            self.revisar_cache()
        self.cerrar_cajas()
        self.armar_si_libre()
        return True

    # ── BC-80: el estado desde la cadena ──

    def arrancar(self):
        """Al arrancar: la cadena en memoria (verificando todas las firmas una vez) y el caché comparado con ella."""
        inicio = time.monotonic()
        self.reglas.verificar_firmas = True
        try:
            desde = self.seguidor.arrancar()
        finally:
            self.reglas.verificar_firmas = False
        log.info("Cadena recorrida desde la altura %d hasta la %d en %.1f s (raíz de estado %s)", desde,
                 self.seguidor.estado.altura, time.monotonic() - inicio, self.seguidor.estado.raiz())
        if self.redis.set(contrato.ESTADO_REVISION, uuid.uuid4().hex, nx=True, ex=120):
            self.revisar_cache()
            cambios = self._comprometido(keys=[contrato.PENDIENTES, contrato.CAJAS, contrato.COMPROMETIDO])
            if cambios:
                DIVERGENCIAS.labels("comprometido").inc(cambios)
                log.warning("`comprometido` no coincidía con las transacciones pendientes en %d cuentas: recalculado",
                            cambios)

    def revisar_cache(self):
        """Compara `saldos`, `nonces:confirmados`, `cuentas` y `permisos` con la cadena y, si difieren, gana la
        cadena. Devuelve {campo: [(clave, cadena, caché)]} de lo que corrigió."""
        self._ultima_revision = time.time()
        for _ in range(5):
            with self.seguidor.lock:
                estado = self.seguidor.ponerse_al_dia()
                cache = estado_cadena.cache_de_redis(self.redis)
                if cache["altura"] != estado.altura:
                    continue  # entró un bloque en el medio
                diferentes = estado_cadena.diferencias(estado, cache)
                if not diferentes:
                    return {}
                if not estado_cadena.reparar(self.redis, estado, diferentes):
                    continue
            for campo, lista in diferentes.items():
                DIVERGENCIAS.labels(campo).inc(len(lista))
                for clave, cadena, guardado in lista[:20]:
                    log.error("Caché distinto de la cadena en %s[%s]: la cadena dice %s y Redis decía %s; gana la cadena",
                              campo, clave, json.dumps(cadena), json.dumps(guardado))
            return diferentes
        log.warning("No se pudo comparar el caché con la cadena: entraban bloques en el medio")
        return {}

    def _problema_en_la_cadena(self, altura, problemas):
        CADENA_PROBLEMAS.inc()
        log.error("El bloque %d de la cadena no cumple el contrato: %s", altura, "; ".join(problemas[:5]))

    def cerrar_cajas(self):
        cajas = self._cerrar_cajas(keys=[contrato.PENDIENTES, contrato.CAJAS], args=[self.tx_por_caja])
        if cajas:
            log.info("Se cerraron %d cajas", cajas)
        return cajas

    def vence(self):
        """Segundos que dura `tarea:actual`: la bajada de emergencia (BC-73) la acorta."""
        return min(self.tarea_vence, self.emergencia) if self.emergencia > 0 else self.tarea_vence

    def armar_si_libre(self):
        """Si no hay tarea en curso y hay cajas, arma el bloque candidato con la primera y lo publica.

        Si la fila está vacía cierra una caja con lo que esperaba (BC-89): así, al confirmarse un bloque, el
        siguiente sale enseguida con todo lo que llegó mientras se minaba, sin esperar el tic.
        La caja queda primera en `cajas` mientras se mina (la saca el consenso al confirmar): si
        el NCT muere, `tarea:actual` vence y la misma caja se vuelve a armar. Devuelve el id o None.
        """
        id_tarea = uuid.uuid4().hex
        if not self.redis.set(TAREA_ACTUAL, id_tarea, nx=True, ex=self.vence()):
            return None
        caja = self.redis.lindex(contrato.CAJAS, 0)
        if caja is None and self.cerrar_cajas():
            caja = self.redis.lindex(contrato.CAJAS, 0)
        if caja is None:
            self._soltar(keys=[TAREA_ACTUAL], args=[id_tarea])
            return None
        if not self._publicar_tarea(id_tarea, caja):
            return self.armar_si_libre()  # la caja sobraba: se sigue con la próxima
        return id_tarea

    def _publicar_tarea(self, id_tarea, caja, despues_de=0):
        """`caja` es el texto tal cual está en `cajas`: el consenso lo compara para sacarla de la fila.

        `despues_de`: al rearmar, el timestamp tiene que ser otro (si no, el hash a buscar es el
        mismo y no hay soluciones nuevas), aunque se rearme en el mismo segundo.

        Las transacciones que ya están en la cadena no van (serían un doble gasto). Si no queda
        ninguna, la caja sale de la fila, se suelta la marca y devuelve False: no publica nada.
        En el primer bloque de cada época van primero los pagos de participación de la anterior.
        """
        with self.seguidor.lock:
            estado = self.seguidor.ponerse_al_dia()
            punta, altura, raiz_estado = estado.punta, estado.altura + 1, estado.raiz()
            transacciones = self._filtrar(estado, self._transacciones(json.loads(caja)))
        if not transacciones:
            self._sacar_caja(keys=[contrato.CAJAS], args=[caja])
            self._soltar(keys=[TAREA_ACTUAL], args=[id_tarea])
            log.warning("Una caja no tenía transacciones que valgan: se saca de la fila")
            return False
        if contrato.es_primer_bloque_de_epoca(altura):
            transacciones = self.participacion(contrato.epoca(altura) - 1, punta) + transacciones
        timestamp = max(int(time.time()), despues_de + 1)
        armada = float(self._armada(keys=[contrato.CAJA_ARMADA], args=[caja, time.time()]))
        vencida = self._relevar(keys=[contrato.TAREA_ULTIMA], args=[id_tarea])
        if vencida:
            ts_vieja = self.redis.hget(contrato.TAREA.format(vencida), "timestamp")
            if ts_vieja:
                timestamp = max(timestamp, int(ts_vieja) + 1)
            log.info("La tarea %s venció sin bloque: se cierra y su caja se vuelve a armar", vencida)
            self._archivar(vencida)
        self.redis.hset(contrato.TAREA.format(id_tarea), mapping={
            "previous_hash": punta, "timestamp": timestamp, "altura": altura,
            "transacciones": json.dumps(transacciones), "caja": caja, "estado": "en_curso", "publicada": time.time(),
            "armada": armada, "raiz_estado": raiz_estado})
        self.publicador.publicar(COLA_TAREAS, {"tarea": id_tarea, "previous_hash": punta, "altura": altura,
                                               "transacciones": transacciones, "timestamp": timestamp,
                                               "armada": armada, "raiz_estado": raiz_estado})
        log.info("Tarea %s publicada: bloque %d con %d transacciones sobre %s",
                 id_tarea, altura, len(transacciones), punta)
        return True

    def _filtrar(self, estado, transacciones):
        """Las de la caja que valen contra la cadena (v3). Las que no (el caché estaba mal, o la cuenta se dio de baja
        mientras esperaban) quedan rechazadas con el motivo: así el NCT nunca arma un bloque que los nodos rechacen."""
        validas, rechazadas = estado_cadena.filtrar(estado, transacciones)
        for tx, motivo in rechazadas:
            if self._rechazar_pendiente(keys=[contrato.TX.format(tx["id"]), contrato.COMPROMETIDO, contrato.NONCES,
                                              contrato.ALTAS_EN_CAMINO], args=[motivo, tx["id"]]):
                SACADAS.inc()
                log.error("La transacción %s no vale contra la cadena y sale de la caja: %s", tx["id"], motivo)
                self._avisar_estado(tx["id"], "rechazada", "no vale contra la cadena")
        return validas

    def _transacciones(self, ids):
        """Las transacciones de una caja que todavía no están en la cadena, completas y en su orden,
        como van en el bloque."""
        tuberia = self.redis.pipeline(transaction=False)
        for id_ in ids:
            tuberia.hgetall(contrato.TX.format(id_))
        return [contrato.transaccion_guardada(id_, tx) for id_, tx in zip(ids, tuberia.execute())
                if tx and tx.get("estado") != "confirmada"]

    # ── BC-63: participación por shares ──

    def participacion(self, epoca, punta):
        """Los pagos `part-<época>-<minero>` de una época que ya cerró, y su detalle en epoca:<n>.

        Se reparte `fondo_epoca` según los shares de cada minero en shares:<época> por el bonus de su
        racha (épocas seguidas con shares). El timestamp es el del último bloque de la época (la
        punta), así rearmar la tarea da los mismos pagos.
        """
        anteriores = [e for e in range(epoca - 1, epoca - contrato.RACHA_MAXIMA, -1) if e >= 0]
        tuberia = self.redis.pipeline(transaction=False)
        tuberia.hget(contrato.BLOQUE.format(punta), "timestamp")
        tuberia.zrange(contrato.SHARES.format(epoca), 0, -1, withscores=True)
        for anterior in anteriores:
            tuberia.zrange(contrato.SHARES.format(anterior), 0, -1)
        timestamp, actual, *con_shares = tuberia.execute()
        shares = {minero: int(n) for minero, n in actual}
        con_shares = [set(mineros) for mineros in con_shares]  # de la época anterior hacia atrás
        rachas = {}
        for minero in shares:
            racha = 1
            while racha <= len(con_shares) and minero in con_shares[racha - 1]:
                racha += 1
            rachas[minero] = racha
        montos = contrato.repartir(self.fondo_epoca, shares, rachas)
        detalle = {minero: {"shares": shares[minero], "racha": rachas[minero], "monto": montos.get(minero, 0)}
                   for minero in sorted(shares)}
        raiz = contrato.raiz_shares(detalle)
        self.redis.hset(contrato.EPOCA.format(epoca), mapping={"fondo": self.fondo_epoca, "detalle": json.dumps(detalle),
                                                               "raiz": raiz})
        if montos:
            log.info("Época %d: se reparten %d entre %d mineros", epoca, sum(montos.values()), len(montos))
        # BC-82: la lista de shares queda comprometida en la cadena (shares-<época>), y cualquiera la recalcula.
        pagos = [contrato.shares_epoca(epoca, self.fondo_epoca, raiz, int(timestamp or 0))] if shares else []
        pagos += [contrato.participacion(epoca, minero, monto, int(timestamp or 0)) for minero, monto in montos.items()]
        # Un pago que ya está en la cadena no se vuelve a proponer: el consenso descartaría el bloque.
        tuberia = self.redis.pipeline(transaction=False)
        for pago in pagos:
            tuberia.hget(contrato.TX.format(pago["id"]), "estado")
        return [pago for pago, estado in zip(pagos, tuberia.execute()) if estado != "confirmada"]

    # ── BC-26 y BC-62: verificar, guardar y avisar ──

    def procesar_solucion(self, mensaje):
        """Una solución de `soluciones`: si es válida y extiende la punta, entra el bloque y se avisa."""
        id_tarea = mensaje["tarea"]
        tarea = self.redis.hgetall(contrato.TAREA.format(id_tarea))
        if not tarea:
            log.warning("Solución para la tarea %s, que no existe: se descarta", id_tarea)
            return None
        if mensaje.get("sin_solucion"):
            log.info("La tarea %s volvió sin solución: se rearma su caja con otro timestamp", id_tarea)
            return self.rearmar(id_tarea, tarea, "sin_solucion")
        transacciones = json.loads(tarea["transacciones"])
        problema = self.verificar(tarea, transacciones, mensaje)
        if problema:
            log.warning("La solución de %s para la tarea %s no sirve: %s", mensaje.get("minero"), id_tarea, problema)
            return self.rearmar(id_tarea, tarea, "rechazada")
        return self._guardar(id_tarea, tarea, transacciones, mensaje)

    def rearmar(self, id_tarea, tarea, estado):
        """La misma caja como una tarea nueva (otro id: el TrP ya tiene estado guardado para la vieja)."""
        nueva = uuid.uuid4().hex
        if not self._rearmar(keys=[contrato.TAREA.format(id_tarea), TAREA_ACTUAL],
                             args=[id_tarea, nueva, estado, self.vence()]):
            log.info("La tarea %s ya no estaba en curso: no se rearma", id_tarea)
            return None
        self._archivar(id_tarea)
        if not self._publicar_tarea(nueva, tarea["caja"], despues_de=int(tarea["timestamp"])):
            self.armar_si_libre()
        return estado

    @staticmethod
    def verificar(tarea, transacciones, mensaje):
        """NCT.3: el NCT nunca confía en el minero. Devuelve el problema, o None si sirve.

        El texto se arma acá con la coinbase de quien mandó el nonce y la recompensa que corresponde
        al prefijo: un nonce minado sobre el texto de otro minero, o con otra recompensa, no da el hash.
        """
        if not tarea.get("prefijo"):
            return "la tarea no tiene prefijo (lo escribe el TrP)"
        if not contrato.cuenta_valida(mensaje.get("minero")):
            return "el minero no es una cuenta"
        if not isinstance(mensaje.get("nonce"), int) or isinstance(mensaje.get("nonce"), bool) or mensaje["nonce"] < 0:
            return "el nonce no es un entero positivo"
        texto = contrato.texto_para(datos_tarea(tarea, transacciones), mensaje["minero"])
        hash_ = contrato.hash_bloque(texto, mensaje["nonce"])
        if hash_ != mensaje.get("hash"):
            return f"el hash no coincide con el texto de {mensaje['minero']} (da {hash_})"
        if not contrato.cumple(hash_, tarea["prefijo"]):
            return f"el hash no empieza con {tarea['prefijo']}"
        return None

    def _guardar(self, id_tarea, tarea, transacciones, mensaje):
        prefijo, hash_, minero = tarea["prefijo"], mensaje["hash"], mensaje["minero"]
        # BC-73: desde que se armó la caja por primera vez, con los rearmados adentro (antes de BC-73 no
        # había `armada`: la tarea vieja se mide desde que se publicó).
        duracion = max(0.0, time.time() - float(tarea.get("armada") or tarea["publicada"]))
        del_bloque = contrato.transacciones_del_bloque(datos_tarea(tarea, transacciones), minero)
        bloque = {"previous_hash": tarea["previous_hash"], "timestamp": tarea["timestamp"],
                  "transactions": json.dumps(del_bloque), "nonce": mensaje["nonce"], "block_hash": hash_,
                  "prefijo": prefijo, "duracion": round(duracion, 3), "raiz_estado": tarea["raiz_estado"]}
        movimientos = [x for tx in del_bloque for x in movimiento(tx)]
        resultado, *datos = self._consenso(
            keys=[contrato.TAREA.format(id_tarea), contrato.CADENA, contrato.CAJAS, contrato.SALDOS,
                  contrato.COMPROMETIDO, TAREA_ACTUAL, contrato.BLOQUE.format(hash_)],
            args=[id_tarea, hash_, len(bloque), *plano(bloque), *movimientos,
                  *contrato.CAMPOS_COPIADOS_TX, len(contrato.CAMPOS_COPIADOS_TX)])
        if resultado == "descartada":
            self._archivar(id_tarea)
            # La caja sigue primera en `cajas`: se arma otra vez sobre la punta nueva.
            log.info("La tarea %s ya no extiende la punta, no sigue en curso o repite una transacción "
                     "confirmada: el bloque no entra", id_tarea)
            self.armar_si_libre()
            return resultado
        self._archivar(id_tarea)
        hash_, altura = datos[0], int(datos[1])
        if resultado == "confirmada":
            BLOQUES.inc()
            TIEMPO_BLOQUE.observe(duracion)
            log.info("Bloque %d confirmado: %s (minó %s, cobra %d)", altura, hash_, minero, del_bloque[0]["monto"])
        self._esperar_replica(hash_)
        self.publicador.avisar(contrato.EXCHANGE_EVENTOS, contrato.BLOQUE_CONFIRMADO,
                               {"tarea": id_tarea, "block_hash": hash_, "altura": altura})
        self.armar_si_libre()
        return resultado

    def _archivar(self, id_tarea):
        """BC-70: una tarea terminada vence en `tarea_guarda` segundos, con sus conjuntos de shares y de
        rangos sin solución. Lo que importa ya quedó en el bloque (o la caja se vuelve a armar)."""
        tuberia = self.redis.pipeline(transaction=False)
        for clave in (contrato.TAREA, contrato.SHARES_TAREA, contrato.TAREA_SIN_SOLUCION):
            tuberia.expire(clave.format(id_tarea), self.tarea_guarda)
        tuberia.execute()

    def _esperar_replica(self, hash_):
        """Que una réplica de Redis tenga el bloque antes de anunciarlo (WAIT). Si no llega a tiempo,
        se avisa igual: el bloque ya está en el primario con AOF, y callarlo dejaría a todos esperando."""
        if self.wait_replicas <= 0:
            return
        confirmaron = self.redis.execute_command("WAIT", self.wait_replicas, self.wait_ms)
        if confirmaron < self.wait_replicas:
            SIN_REPLICA.inc()
            log.warning("El bloque %s se avisa con %d de %d réplicas de Redis", hash_, confirmaron, self.wait_replicas)

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
    """SALDOS_INICIALES: un JSON {cuenta: saldo} o {cuenta: {"saldo": n, "nombre": alias}}; vacío, ninguno."""
    if not texto:
        return SALDOS_INICIALES
    saldos = json.loads(texto)

    def valido(cuenta, datos):
        saldo = datos.get("saldo") if isinstance(datos, dict) else datos
        nombre = datos.get("nombre", "x") if isinstance(datos, dict) else "x"
        return (contrato.cuenta_valida(cuenta) and isinstance(saldo, int) and not isinstance(saldo, bool)
                and 0 <= saldo <= contrato.MONTO_MAXIMO * 1000 and (nombre == "" or contrato.nombre_valido(nombre)))

    if not isinstance(saldos, dict) or not all(valido(c, d) for c, d in saldos.items()):
        raise ValueError("SALDOS_INICIALES tiene que ser un JSON {cuenta: saldo} o {cuenta: {saldo, nombre}}, "
                         "con la cuenta en hex (64 caracteres) y el saldo entero >= 0")
    return saldos


def leer_organizador():
    organizador = config.texto("ORGANIZADOR_CLAVE")
    if organizador and not contrato.cuenta_valida(organizador):
        raise ValueError("ORGANIZADOR_CLAVE tiene que ser una clave pública Ed25519 en hex (64 caracteres)")
    return organizador


def main():
    servicio = Servicio("nct", colas=contrato.COLAS)
    c = servicio.config
    publicador = Rabbit(c, contrato.COLAS)  # para publicar desde cualquier hilo (es segura entre hilos)
    nct = Nct(servicio.redis, publicador,
              saldos_iniciales=leer_saldos_iniciales(config.texto("SALDOS_INICIALES")),
              organizador=leer_organizador(),
              wait_replicas=config.entero("WAIT_REPLICAS", 1), wait_ms=config.entero("WAIT_MS", 1000),
              ventana=config.entero("VENTANA_SEGUNDOS", VENTANA), tx_por_caja=config.entero("TX_POR_CAJA", TX_POR_CAJA),
              tarea_vence=config.entero("TAREA_VENCE_SEGUNDOS", TAREA_VENCE),
              espera_nonce=config.entero("ESPERA_NONCE_SEGUNDOS", ESPERA_NONCE),
              fondo_epoca=config.entero("FONDO_EPOCA", contrato.FONDO_EPOCA),
              tarea_guarda=config.entero("TAREA_GUARDA_SEGUNDOS", contrato.TAREA_GUARDA),
              emergencia=config.entero("EMERGENCIA_SEGUNDOS", contrato.EMERGENCIA_SEGUNDOS),
              revisar_cada=config.entero("REVISAR_ESTADO_SEGUNDOS", 60),
              snapshot_cada=config.entero("SNAPSHOT_CADA", 100))
    nct.cargar_genesis()
    nct.arrancar()
    servicio.servir_http(en_hilo=True)
    publicador.conectar()
    servicio.rabbit.conectar()  # la de /health
    consumir(c, contrato.COLAS[0], nct.validar)  # transacciones
    consumir(c, contrato.COLAS[4], nct.procesar_solucion)  # soluciones
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
