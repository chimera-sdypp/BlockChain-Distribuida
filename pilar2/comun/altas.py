"""Altas de cuentas (BC-58): los pedidos en `altas:pendientes` y lo que hace el organizador con ellos.

Lo usan el backend (POST /api/altas y el panel) y el gateway (un minero firmado y sin aprobar queda
pedido solo). Cada paso es un Lua, así dos réplicas que atienden lo mismo a la vez no se pisan.

Contrato v3 (BC-79): acá ya no se aprueba ni se da de baja. `cuentas` es caché de la cadena: una cuenta entra o
sale sólo cuando se confirma el bloque con su `alta-<n>` o su `baja-<n>`, firmadas por el organizador.
"""

import json
import time

from comun import contrato

ALTAS_MAXIMAS = 1000  # pedidos pendientes como mucho: un pedido sólo cuesta generar una clave

# KEYS: cuentas, altas:pendientes  ARGV: cuenta, pedido (JSON), máximo de pendientes, renombrar ('1' o '0')
# Con renombrar, un pedido que ya estaba toma el nombre del nuevo (BC-71: el alias del /hello le gana al
# principio de la cuenta con que lo anotó un keep-alive).
LUA_PEDIR = """
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 or redis.call('HEXISTS', 'altas:en_camino', ARGV[1]) == 1 then
  return 'aprobada'
end
local previo = redis.call('HGET', KEYS[2], ARGV[1])
if previo then
  if ARGV[4] == '1' then
    local pedido, nombre = cjson.decode(previo), cjson.decode(ARGV[2]).nombre
    if pedido.nombre ~= nombre then
      pedido.nombre = nombre
      redis.call('HSET', KEYS[2], ARGV[1], cjson.encode(pedido))
      return 'renombrada'
    end
  end
  return 'pedida'
end
if redis.call('HLEN', KEYS[2]) >= tonumber(ARGV[3]) then return 'lleno' end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
return 'nueva'
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
        self._sacar = redis.register_script(LUA_SACAR)

    def pedir(self, cuenta, tipo, nombre, ahora=None, renombrar=False):
        """Anota el pedido. Devuelve «nueva», «pedida» (ya estaba), «aprobada» (ya opera) o «lleno».

        Con `renombrar`, si ya estaba pedido con otro nombre le pone este y devuelve «renombrada».
        """
        pedido = json.dumps({"tipo": tipo, "nombre": nombre, "pedida": int(time.time() if ahora is None else ahora)})
        return self._pedir(keys=[contrato.CUENTAS, contrato.ALTAS_PENDIENTES],
                           args=[cuenta, pedido, self.maximas, "1" if renombrar else "0"])

    def rechazar(self, cuenta):
        """Borra el pedido. Devuelve lo que se había pedido, o None."""
        datos = self._sacar(keys=[contrato.ALTAS_PENDIENTES], args=[cuenta])
        return json.loads(datos) if datos else None

    # ── BC-75: suspensión ──

    def suspender(self, cuenta, motivo, evidencia, ahora=None):
        """El gateway deja de prestarle rangos hasta que el organizador confirme la baja o la reactive.

        Devuelve True si la suspendió ahora, False si ya estaba suspendida o no es un minero aprobado.
        """
        datos = self.redis.hget(contrato.CUENTAS, cuenta)
        if not datos or json.loads(datos).get("tipo") != "minero":
            return False
        registro = json.dumps({"nombre": json.loads(datos).get("nombre", ""), "motivo": motivo,
                               "evidencia": evidencia, "cuando": int(time.time() if ahora is None else ahora)})
        return bool(self.redis.hsetnx(contrato.SUSPENDIDOS, cuenta, registro))

    def reactivar(self, cuenta):
        """La saca de `suspendidos`. Devuelve lo que tenía, o None si no estaba suspendida."""
        datos = self._sacar(keys=[contrato.SUSPENDIDOS], args=[cuenta])
        return json.loads(datos) if datos else None

    def suspendido(self, cuenta):
        datos = self.redis.hget(contrato.SUSPENDIDOS, cuenta)
        return json.loads(datos) if datos else None

    def suspendidos(self):
        """[{cuenta, nombre, motivo, evidencia, cuando}], la más reciente primero."""
        lista = [{"cuenta": c, **json.loads(d)} for c, d in self.redis.hgetall(contrato.SUSPENDIDOS).items()]
        return sorted(lista, key=lambda s: (-s.get("cuando", 0), s["cuenta"]))

    def pendientes(self):
        """[{cuenta, tipo, nombre, pedida}], el pedido más viejo primero.

        BC-93: sin los que ya tienen un alta validada que todavía no entró en un bloque (el pedido se borra recién al
        confirmarla): si no, el organizador los ve todavía pendientes y los aprueba otra vez."""
        pedidos = self.redis.hgetall(contrato.ALTAS_PENDIENTES)
        if not pedidos:
            return []
        en_camino = set(self.redis.hkeys(contrato.ALTAS_EN_CAMINO))
        activas = dict(zip(pedidos, self.redis.hmget(contrato.CUENTAS, list(pedidos))))
        pedidos = [{"cuenta": c, **json.loads(d)} for c, d in pedidos.items() if c not in en_camino and not activas[c]]
        return sorted(pedidos, key=lambda p: (p.get("pedida", 0), p["cuenta"]))

    def aprobadas(self):
        """[{cuenta, tipo, nombre, alta, saldo}], la más vieja primero."""
        cuentas = self.redis.hgetall(contrato.CUENTAS)
        ids = sorted(cuentas)
        saldos = self.redis.hmget(contrato.SALDOS, ids) if ids else []
        resultado = [{"cuenta": c, **json.loads(cuentas[c]), "saldo": int(s or 0)} for c, s in zip(ids, saldos)]
        return sorted(resultado, key=lambda c: (c.get("alta", 0), c["cuenta"]))
