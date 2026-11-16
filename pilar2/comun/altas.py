"""Altas de cuentas (BC-58): los pedidos en `altas:pendientes` y lo que hace el organizador con ellos.

Lo usan el backend (POST /api/altas y el panel) y el gateway (un minero firmado y sin aprobar queda
pedido solo). Cada paso es un Lua, así dos réplicas que atienden lo mismo a la vez no se pisan.
"""

import json
import time

from comun import contrato

ALTAS_MAXIMAS = 1000  # pedidos pendientes como mucho: un pedido sólo cuesta generar una clave

# KEYS: cuentas, altas:pendientes  ARGV: cuenta, pedido (JSON), máximo de pendientes
LUA_PEDIR = """
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then return 'aprobada' end
if redis.call('HEXISTS', KEYS[2], ARGV[1]) == 1 then return 'pedida' end
if redis.call('HLEN', KEYS[2]) >= tonumber(ARGV[3]) then return 'lleno' end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
return 'nueva'
"""

# KEYS: cuentas, altas:pendientes  ARGV: cuenta, alta (segundos Unix), nombre ('' = el del pedido)
# Devuelve la cuenta aprobada en JSON, o nada si no había pedido.
LUA_APROBAR = """
local pedido = redis.call('HGET', KEYS[2], ARGV[1])
if not pedido then return false end
pedido = cjson.decode(pedido)
local nombre = ARGV[3]
if nombre == '' then nombre = pedido.nombre or '' end
local datos = cjson.encode({tipo = pedido.tipo, nombre = nombre, alta = tonumber(ARGV[2])})
redis.call('HSET', KEYS[1], ARGV[1], datos)
redis.call('HDEL', KEYS[2], ARGV[1])
return datos
"""

# KEYS: el hash  ARGV: cuenta. Devuelve lo que había (JSON) y lo borra; nada si no estaba.
LUA_SACAR = """
local datos = redis.call('HGET', KEYS[1], ARGV[1])
if not datos then return false end
redis.call('HDEL', KEYS[1], ARGV[1])
return datos
"""


class Altas:
    def __init__(self, redis, maximas=ALTAS_MAXIMAS):
        self.redis, self.maximas = redis, maximas
        self._pedir = redis.register_script(LUA_PEDIR)
        self._aprobar = redis.register_script(LUA_APROBAR)
        self._sacar = redis.register_script(LUA_SACAR)

    def pedir(self, cuenta, tipo, nombre, ahora=None):
        """Anota el pedido. Devuelve «nueva», «pedida» (ya estaba), «aprobada» (ya opera) o «lleno»."""
        pedido = json.dumps({"tipo": tipo, "nombre": nombre, "pedida": int(time.time() if ahora is None else ahora)})
        return self._pedir(keys=[contrato.CUENTAS, contrato.ALTAS_PENDIENTES], args=[cuenta, pedido, self.maximas])

    def aprobar(self, cuenta, nombre="", ahora=None):
        """Pasa el pedido a `cuentas` con la fecha de alta. Devuelve {tipo, nombre, alta}, o None si no había pedido."""
        datos = self._aprobar(keys=[contrato.CUENTAS, contrato.ALTAS_PENDIENTES],
                              args=[cuenta, int(time.time() if ahora is None else ahora), nombre])
        return json.loads(datos) if datos else None

    def rechazar(self, cuenta):
        """Borra el pedido. Devuelve lo que se había pedido, o None."""
        datos = self._sacar(keys=[contrato.ALTAS_PENDIENTES], args=[cuenta])
        return json.loads(datos) if datos else None

    def baja(self, cuenta):
        """Saca la cuenta de `cuentas`: el gateway deja de atenderla y el NCT rechaza sus transferencias.

        El saldo queda donde está (congelado). Devuelve los datos que tenía, o None si no estaba aprobada.
        """
        datos = self._sacar(keys=[contrato.CUENTAS], args=[cuenta])
        return json.loads(datos) if datos else None

    def pendientes(self):
        """[{cuenta, tipo, nombre, pedida}], el pedido más viejo primero."""
        pedidos = [{"cuenta": c, **json.loads(d)} for c, d in self.redis.hgetall(contrato.ALTAS_PENDIENTES).items()]
        return sorted(pedidos, key=lambda p: (p.get("pedida", 0), p["cuenta"]))

    def aprobadas(self):
        """[{cuenta, tipo, nombre, alta, saldo}], la más vieja primero."""
        cuentas = self.redis.hgetall(contrato.CUENTAS)
        ids = sorted(cuentas)
        saldos = self.redis.hmget(contrato.SALDOS, ids) if ids else []
        resultado = [{"cuenta": c, **json.loads(cuentas[c]), "saldo": int(s or 0)} for c, s in zip(ids, saldos)]
        return sorted(resultado, key=lambda c: (c.get("alta", 0), c["cuenta"]))
