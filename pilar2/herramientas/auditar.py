"""Auditoría de la cadena (BC-61, y desde el contrato v3, BC-80): la recorre desde el génesis y la verifica entera.

    python -m herramientas.auditar [--organizador <cuenta>] [--reparar]      # REDIS_* como los servicios

Usa el mismo código que el NCT y los nodos verificadores (comun/estado.py): cada hash, la raíz de transacciones y la
de estado, cada firma, el nonce de cada cuenta, las altas, bajas y permisos, y cada transacción de emisión (génesis,
coinbase, carga y la participación contra la lista de shares publicada). Después compara el estado que sale de la
cadena con el caché de Redis (`saldos`, `nonces:confirmados`, `cuentas` y `permisos`) y que `nonces` no esté atrás.
Sale con 0 si está todo bien y con 1 si encontró algo. Con `--reparar`, gana la cadena: pisa el caché.

Para la demo: se cambia un saldo a mano en Redis (`HINCRBY saldos <cuenta> 1000`) y la auditoría lo encuentra,
porque los saldos no se creen: salen de la cadena.
"""

import argparse
import sys
from dataclasses import dataclass, field

from comun import config, contrato, estado as estado_cadena

NOMBRES = {"genesis": "génesis", "participacion": "participación"}


@dataclass
class Informe:
    bloques: int = 0
    tipos: dict = field(default_factory=dict)      # tipo de transacción → cuántas
    saldos: dict = field(default_factory=dict)     # cuenta → saldo recalculado desde la cadena
    problemas: list = field(default_factory=list)  # (altura o None, qué)
    avisos: list = field(default_factory=list)     # lo que no se pudo verificar
    estado: object = None                          # el estado que sale de la cadena
    diferencias: dict = field(default_factory=dict)  # lo que el caché tenía distinto
    reparado: bool = False

    @property
    def ok(self):
        return not self.problemas

    def problema(self, altura, texto):
        self.problemas.append((altura, texto))


def tipo_de(tx):
    tipo = contrato.tipo_de(tx)
    return NOMBRES.get(tipo, tipo)


def auditar(redis, organizador="", reparar=False):
    informe = Informe()
    reglas = estado_cadena.Reglas(organizador=organizador, shares=estado_cadena.shares_de_redis(redis))
    estado = estado_cadena.Estado()
    for hash_, bloque in estado_cadena.bloques_de_redis(redis):
        altura = estado.altura + 1
        if bloque is None:
            informe.problema(altura, f"no está bloque:{hash_}")
            break
        problemas, avisos = estado_cadena.aplicar_bloque(estado, bloque, reglas, estricto=False, hash_esperado=hash_)
        for texto in problemas:
            informe.problema(altura, texto)
        informe.avisos.extend(a for a in avisos if a not in informe.avisos)
        for tx in bloque["transactions"]:
            tipo = tipo_de(tx)
            informe.tipos[tipo] = informe.tipos.get(tipo, 0) + 1
        informe.bloques += 1
    if informe.bloques == 0:
        informe.problema(None, "la cadena está vacía")
        return informe
    if not organizador and estado.organizador:
        informe.avisos.append(f"el organizador sale del génesis ({estado.organizador}); con --organizador se lo compara")
    if not estado.organizador and any(t in informe.tipos for t in ("carga", "alta", "baja", "permiso")):
        informe.avisos.append("las firmas de las cargas y actas no se verificaron: no hay organizador")
    informe.estado, informe.saldos = estado, {c: s for c, s in estado.saldos.items() if s}
    cache = estado_cadena.cache_de_redis(redis)
    if cache["altura"] != estado.altura:
        informe.avisos.append("entró un bloque mientras se auditaba: el caché se compara con la cadena hasta acá")
    else:
        informe.diferencias = estado_cadena.diferencias(estado, cache)
        for campo, lista in informe.diferencias.items():
            for clave, cadena, guardado in lista:
                informe.problema(None, f"{campo} de {clave}: la cadena dice {cadena} y Redis {guardado}")
        if reparar and informe.diferencias:
            informe.reparado = estado_cadena.reparar(redis, estado, informe.diferencias)
    aceptados = {cuenta: int(n) for cuenta, n in redis.hgetall(contrato.NONCES).items()}
    for cuenta, nonce in estado.nonces.items():
        if aceptados.get(cuenta, 0) < nonce:  # Redis puede ir adelante (las pendientes), nunca atrás
            informe.problema(None, f"nonce de {cuenta}: la cadena llega al {nonce} y Redis dice {aceptados.get(cuenta, 0)}")
    return informe


def mostrar(informe, salida=print):
    tipos = " · ".join(f"{n} {tipo}" for tipo, n in sorted(informe.tipos.items()))
    salida(f"{informe.bloques} bloques, {sum(informe.tipos.values())} transacciones ({tipos})")
    salida(f"{len(informe.saldos)} cuentas con saldo, {sum(informe.saldos.values())} en circulación")
    if informe.estado is not None:
        salida(f"raíz de estado en la punta: {informe.estado.raiz()}")
    for aviso in informe.avisos:
        salida(f"aviso: {aviso}")
    if informe.ok:
        salida("Todo igual: cada hash, raíz, firma y nonce se verificó, y el caché de Redis es lo que dice la cadena.")
        return
    salida(f"{len(informe.problemas)} problemas:")
    for altura, texto in informe.problemas:
        salida(f"  {'bloque ' + str(altura) if altura is not None else 'Redis'}: {texto}")
    if informe.reparado:
        salida("Reparado: el caché de Redis quedó como dice la cadena.")


def main():
    parser = argparse.ArgumentParser(prog="python -m herramientas.auditar", description=__doc__.split("\n\n")[0])
    parser.add_argument("--organizador", default=config.texto("ORGANIZADOR_CLAVE"),
                        help="la cuenta del organizador, para compararla con la del génesis (ORGANIZADOR_CLAVE)")
    parser.add_argument("--reparar", action="store_true", help="si el caché difiere, gana la cadena: lo pisa")
    argumentos = parser.parse_args()
    from comun import redisdb
    informe = auditar(redisdb.conectar(config.cargar("auditar")), argumentos.organizador, argumentos.reparar)
    mostrar(informe)
    sys.exit(0 if informe.ok else 1)


if __name__ == "__main__":
    main()
