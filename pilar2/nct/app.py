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


def plano(diccionario):
    """{k: v} → [k1, v1, k2, v2, …] como texto, para pasarle a un script Lua."""
    return [str(x) for par in diccionario.items() for x in par]


def campos_bloque(bloque):
    """Un bloque como lo guarda `bloque:<hash>`: `transactions` en JSON."""
    return {**bloque, "transactions": json.dumps(bloque["transactions"])}


class Nct:
    def __init__(self, redis, publicador, saldos_iniciales=None, wait_replicas=1, wait_ms=1000):
        self.redis, self.publicador = redis, publicador
        self.saldos_iniciales = SALDOS_INICIALES if saldos_iniciales is None else saldos_iniciales
        self.wait_replicas, self.wait_ms = wait_replicas, wait_ms
        self._genesis = redis.register_script(LUA_GENESIS)

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
              wait_replicas=config.entero("WAIT_REPLICAS", 1), wait_ms=config.entero("WAIT_MS", 1000))
    nct.cargar_genesis()
    servicio.servir_http(en_hilo=True)
    publicador.conectar()
    servicio.rabbit.conectar()  # la de /health
    threading.Event().wait()
