"""Auditoría de la cadena (BC-61): la recorre desde el génesis y la verifica entera, sin confiar en nadie.

    python -m herramientas.auditar [--organizador <cuenta>]      # REDIS_* como los servicios

Verifica cada hash, cada raíz, cada firma, el nonce de cada cuenta y cada transacción de emisión
(génesis, coinbase, carga y participación), recalcula todos los saldos y los compara con `saldos`
de Redis. Sale con 0 si está todo bien y con 1 si encontró algo.

Para la demo: se cambia un saldo a mano en Redis (`HINCRBY saldos <cuenta> 1000`) y la auditoría
lo encuentra, porque los saldos no se creen: salen de la cadena.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field

from comun import config, contrato


@dataclass
class Informe:
    bloques: int = 0
    tipos: dict = field(default_factory=dict)      # tipo de transacción → cuántas
    saldos: dict = field(default_factory=dict)     # cuenta → saldo recalculado desde la cadena
    problemas: list = field(default_factory=list)  # (altura o None, qué)
    avisos: list = field(default_factory=list)     # lo que no se pudo verificar

    @property
    def ok(self):
        return not self.problemas

    def problema(self, altura, texto):
        self.problemas.append((altura, texto))

    def contar(self, tipo):
        self.tipos[tipo] = self.tipos.get(tipo, 0) + 1


def tipo_de(tx):
    if not contrato.es_emision(tx):
        return "transferencia"
    for prefijo, tipo in (("genesis-", "génesis"), ("cb-", "coinbase"), ("carga-", "carga"), ("part-", "participación")):
        if str(tx.get("id", "")).startswith(prefijo):
            return tipo
    return "emisión desconocida"


class Auditor:
    def __init__(self, redis, organizador=""):
        self.redis, self.organizador = redis, organizador
        self.informe = Informe()
        self.nonces, self.vistas = {}, set()
        self._epocas = {}

    def auditar(self):
        hashes = self.redis.lrange(contrato.CADENA, 0, -1)
        if not hashes:
            self.informe.problema(None, "la cadena está vacía")
            return self.informe
        anterior = None
        for altura, hash_ in enumerate(hashes):
            bloque = self._leer(altura, hash_)
            if bloque is None:
                continue
            if altura == 0:
                self._genesis(bloque, hash_)
            else:
                self._bloque(altura, bloque, hash_, hashes[altura - 1], anterior)
            anterior = bloque
            self.informe.bloques += 1
        self._comparar_saldos()
        self._comparar_nonces()
        return self.informe

    def _leer(self, altura, hash_):
        guardado = self.redis.hgetall(contrato.BLOQUE.format(hash_))
        if not guardado:
            self.informe.problema(altura, f"no está bloque:{hash_}")
            return None
        try:
            return {**guardado, "transactions": json.loads(guardado["transactions"]),
                    "timestamp": int(guardado["timestamp"]), "nonce": int(guardado["nonce"]),
                    "altura": int(guardado.get("altura", -1))}
        except (KeyError, ValueError) as error:
            self.informe.problema(altura, f"el bloque está mal guardado: {error!r}")
            return None

    # ── El génesis ──

    def _genesis(self, bloque, hash_):
        if bloque.get("previous_hash") != contrato.CEROS or bloque["nonce"] != 0:
            self.informe.problema(0, "el génesis tiene que tener previous_hash en ceros y nonce 0")
        texto = contrato._texto(contrato.CEROS, bloque["timestamp"], bloque["transactions"])
        if contrato.hash_bloque(texto, 0) != hash_ or bloque.get("block_hash") != hash_:
            self.informe.problema(0, "el hash del génesis no sale de sus transacciones")
        for tx in bloque["transactions"]:
            esperada = contrato._emision(f"genesis-{tx.get('destino')}", tx.get("destino"), tx.get("monto"), 0,
                                         bloque["timestamp"])
            if tx != esperada or not contrato.cuenta_valida(tx.get("destino")) or not isinstance(tx.get("monto"), int):
                self.informe.problema(0, f"transacción del génesis mal formada: {tx.get('id')}")
                continue
            self._aplicar(0, tx)

    # ── Cada bloque minado ──

    def _bloque(self, altura, bloque, hash_, previo, anterior):
        txs, prefijo = bloque["transactions"], bloque.get("prefijo", "")
        if bloque.get("previous_hash") != previo:
            self.informe.problema(altura, "no encadena: su previous_hash no es el bloque anterior")
        if bloque["altura"] != altura:
            self.informe.problema(altura, f"dice tener altura {bloque['altura']}")
        texto = contrato._texto(bloque.get("previous_hash", ""), bloque["timestamp"], txs)
        if contrato.hash_bloque(texto, bloque["nonce"]) != hash_ or bloque.get("block_hash") != hash_:
            self.informe.problema(altura, "el hash no sale del texto (la raíz de sus transacciones) y el nonce")
        if not prefijo or not contrato.cumple(hash_, prefijo):
            self.informe.problema(altura, f"el hash no cumple el prefijo {prefijo!r}")
        if not txs or tipo_de(txs[0]) != "coinbase":
            self.informe.problema(altura, "la primera transacción no es la coinbase")
        else:
            esperada = contrato.coinbase(altura, txs[0].get("destino"), contrato.recompensa(prefijo), bloque["timestamp"])
            if txs[0] != esperada or not contrato.cuenta_valida(txs[0].get("destino")):
                self.informe.problema(altura, f"la coinbase no es la del ganador con {contrato.recompensa(prefijo)}")
            else:
                self._aplicar(altura, txs[0])
        for tx in txs[1:]:
            tipo = tipo_de(tx)
            if tipo == "transferencia":
                self._transferencia(altura, tx)
            elif tipo == "carga":
                self._carga(altura, tx)
            elif tipo == "participación":
                self._participacion(altura, tx, anterior)
            else:
                self.informe.problema(altura, f"una transacción {tipo} fuera de lugar: {tx.get('id')}")

    def _transferencia(self, altura, tx):
        errores = contrato.validar_transaccion_firmada(tx)
        if errores:
            self.informe.problema(altura, f"transferencia {tx.get('id')} inválida: {'; '.join(errores)}")
            return
        if not self._nonce(altura, tx["origen"], tx):
            return
        if self.informe.saldos.get(tx["origen"], 0) < tx["monto"]:
            self.informe.problema(altura, f"{tx['id']}: el origen no tenía saldo (gasta {tx['monto']})")
            return
        self._aplicar(altura, tx)

    def _carga(self, altura, tx):
        if self.organizador:
            errores = contrato.validar_carga(tx, self.organizador)
            if errores:
                self.informe.problema(altura, f"carga {tx.get('id')} inválida: {'; '.join(errores)}")
                return
        elif not any("organizador" in aviso for aviso in self.informe.avisos):
            self.informe.avisos.append("las firmas de las cargas no se verificaron: falta --organizador")
        if self._nonce(altura, contrato.EMISION, tx):
            self._aplicar(altura, tx)

    def _participacion(self, altura, tx, anterior):
        if not contrato.es_primer_bloque_de_epoca(altura):
            self.informe.problema(altura, f"{tx.get('id')}: la participación va en el primer bloque de la época")
            return
        epoca = contrato.epoca(altura) - 1
        esperado = self._detalle(epoca).get(tx.get("destino"), {}).get("monto")
        timestamp = anterior["timestamp"] if anterior else None
        if tx != contrato.participacion(epoca, tx.get("destino"), esperado, timestamp):
            self.informe.problema(altura, f"{tx.get('id')}: no es el pago de epoca:{epoca} "
                                          f"({esperado} con el timestamp del último bloque de la época)")
            return
        self._aplicar(altura, tx)

    def _detalle(self, epoca):
        """epoca:<n> con el reparto, después de comprobar que los montos salen de sus shares y rachas."""
        if epoca not in self._epocas:
            guardado = self.redis.hgetall(contrato.EPOCA.format(epoca))
            detalle = json.loads(guardado.get("detalle") or "{}")
            fondo = int(guardado.get("fondo") or 0)
            shares = {m: d["shares"] for m, d in detalle.items()}
            rachas = {m: d["racha"] for m, d in detalle.items()}
            montos = {m: d["monto"] for m, d in detalle.items() if d["monto"]}
            if montos != contrato.repartir(fondo, shares, rachas):
                self.informe.problema(None, f"epoca:{epoca}: los montos no salen de repartir {fondo} según sus shares")
            self._epocas[epoca] = detalle
        return self._epocas[epoca]

    def _nonce(self, altura, cuenta, tx):
        esperado = self.nonces.get(cuenta, 0) + 1
        if tx.get("nonce") != esperado:
            self.informe.problema(altura, f"{tx.get('id')}: nonce {tx.get('nonce')}, y le tocaba el {esperado}")
            return False
        self.nonces[cuenta] = esperado
        return True

    def _aplicar(self, altura, tx):
        if tx["id"] in self.vistas:
            self.informe.problema(altura, f"{tx['id']} ya estaba en otro bloque (doble gasto)")
            return
        self.vistas.add(tx["id"])
        self.informe.contar(tipo_de(tx))
        saldos = self.informe.saldos
        if not contrato.es_emision(tx):
            saldos[tx["origen"]] = saldos.get(tx["origen"], 0) - tx["monto"]
        saldos[tx["destino"]] = saldos.get(tx["destino"], 0) + tx["monto"]

    # ── Contra lo que dice Redis ──

    def _comparar_saldos(self):
        en_redis = {cuenta: int(saldo) for cuenta, saldo in self.redis.hgetall(contrato.SALDOS).items()}
        for cuenta in sorted(set(en_redis) | set(self.informe.saldos)):
            cadena, redis_ = self.informe.saldos.get(cuenta, 0), en_redis.get(cuenta, 0)
            if cadena != redis_:
                self.informe.problema(None, f"saldo de {cuenta}: la cadena dice {cadena} y Redis {redis_}")

    def _comparar_nonces(self):
        en_redis = {cuenta: int(n) for cuenta, n in self.redis.hgetall(contrato.NONCES).items()}
        for cuenta, nonce in self.nonces.items():
            if en_redis.get(cuenta, 0) < nonce:  # Redis puede ir adelante (las pendientes), nunca atrás
                self.informe.problema(None, f"nonce de {cuenta}: la cadena llega al {nonce} y Redis dice {en_redis.get(cuenta, 0)}")


def auditar(redis, organizador=""):
    return Auditor(redis, organizador).auditar()


def mostrar(informe, salida=print):
    tipos = " · ".join(f"{n} {tipo}" for tipo, n in sorted(informe.tipos.items()))
    salida(f"{informe.bloques} bloques, {sum(informe.tipos.values())} transacciones ({tipos})")
    salida(f"{len(informe.saldos)} cuentas, {sum(informe.saldos.values())} en circulación")
    for aviso in informe.avisos:
        salida(f"aviso: {aviso}")
    if informe.ok:
        salida("Todo igual: cada hash, raíz, firma y nonce se verificó, y los saldos de Redis son los de la cadena.")
        return
    salida(f"{len(informe.problemas)} problemas:")
    for altura, texto in informe.problemas:
        salida(f"  {'bloque ' + str(altura) if altura is not None else 'Redis'}: {texto}")


def main():
    parser = argparse.ArgumentParser(prog="python -m herramientas.auditar", description=__doc__.split("\n\n")[0])
    parser.add_argument("--organizador", default=config.texto("ORGANIZADOR_CLAVE"),
                        help="la cuenta del organizador, para verificar las firmas de las cargas (ORGANIZADOR_CLAVE)")
    argumentos = parser.parse_args()
    from comun import redisdb
    informe = auditar(redisdb.conectar(config.cargar("auditar")), argumentos.organizador)
    mostrar(informe)
    sys.exit(0 if informe.ok else 1)


if __name__ == "__main__":
    main()
