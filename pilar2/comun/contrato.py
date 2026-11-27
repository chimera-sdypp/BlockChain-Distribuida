"""Contrato entre los servicios: cuentas, transacciones firmadas, bloque, recompensas y nombres de colas y claves.

La especificación completa está en pilar2/CONTRATO.md: es la versión 3. La v2 trajo las cuentas con clave
Ed25519, las transacciones firmadas con nonce por cuenta, la emisión, la coinbase en la raíz y la participación
por shares. La v3 (BC-79, BC-80 y BC-82) pone todo en la cadena: las altas, bajas y permisos son transacciones
firmadas, cada bloque se compromete con el estado (la raíz de estado) y con los shares de la época que paga.
`saldos`, `cuentas`, `nonces:confirmados` y `permisos` de Redis son caché: la verdad es la cadena.
"""

import hashlib
import json
import math
import re
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# Colas, exchange y claves de Redis
COLAS = ("transacciones", "tareas", "rangos", "resultados", "soluciones")
EXCHANGE_EVENTOS = "eventos"            # topic
BLOQUE_CONFIRMADO = "bloque.confirmado"
TRANSACCION_ESTADO = "transaccion.estado"  # el NCT validó (pendiente) o rechazó una transacción
CADENA, SALDOS, PENDIENTES, CAJAS, PRESTAMOS = "cadena", "saldos", "tx:pendientes", "cajas", "prestamos"
TAREA_ACTUAL = "tarea:actual"  # el id de la tarea en curso (una sola a la vez); vence sola si el NCT muere
TAREA_ULTIMA = "tarea:ultima"  # la última tarea publicada: si al armar otra sigue en curso, queda «vencida»
ESTADOS_TAREA = ("en_curso", "confirmada", "descartada", "sin_solucion", "rechazada", "vencida")
COMPROMETIDO = "comprometido"  # cuenta → lo que tiene en transacciones validadas que todavía no entraron en un bloque
TX, BLOQUE, TAREA, NODO = "tx:{}", "bloque:{}", "tarea:{}", "nodo:{}"
NODOS = "nodo:*"  # para SCAN: los mineros son pocos
# COUNT de esos SCAN: cuenta claves recorridas, no encontradas; con 100 y los tx:* de un bulk grande
# (~150.000 claves) eran ~1.500 idas a Redis por SCAN y /metrics tardaba más que la probe del gateway.
NODOS_POR_SCAN = 10_000
# Campos de nodo:<id> (los escribe el gateway): bench y hashes_por_segundo en hashes/s, ultimo_aviso en segundos Unix.
CAMPOS_NODO = ("tipo", "placa", "bench", "hashes_por_segundo", "ultimo_aviso")
ESTADOS_TX = ("recibida", "pendiente", "confirmada", "rechazada")
# Versión 2
CUENTAS = "cuentas"                  # cuenta → JSON {tipo, nombre, alta}
ALTAS_PENDIENTES = "altas:pendientes"  # cuenta → JSON {tipo, nombre, pedida}
NONCES = "nonces"                    # cuenta → último nonce aceptado (pendiente o confirmado); «emision» → la última
                                     # carga o acta del organizador
ESPERA = "espera:{}"                 # sorted set por cuenta: las que llegaron antes que un nonce anterior (score = nonce)
EN_ESPERA = "tx:en_espera"           # sorted set: id → cuándo entró en espera (para rechazar las que no se destraban)
SHARES = "shares:{}"                 # sorted set de la época: minero → shares aceptados (ZINCRBY)
SHARES_TAREA = "tarea:{}:shares"     # conjunto «minero:nonce» de la tarea, para descartar los repetidos
TAREA_SIN_SOLUCION = "tarea:{}:sin_solucion"  # conjunto con los rangos que volvieron sin solución (TrP)
# BC-70: una tarea terminada (y sus dos conjuntos) vence a esta hora. Lo que importa quedó en el bloque.
TAREA_GUARDA = 3600
# BC-94: una transacción rechazada vence a esta hora: le alcanza a la billetera para mostrar el motivo.
RECHAZADA_GUARDA = 3600
# BC-73: dificultad por tiempo.
CAJA_ARMADA = "caja:armada"          # hash {caja, desde}: cuándo se armó por primera vez la caja que se está minando
PREFIJO_REFERENCIA = "prefijo:referencia"  # hash {potencia, altura, prefijo, cuando}: el último recálculo por potencia
EMERGENCIA_SEGUNDOS = 240            # sin bloque en este tiempo, la tarea vence y se rearma con un carácter menos
# BC-74: lo que mide el gateway en vez de creerle al minero.
PRESTAMO_DE = "prestamo_de:{}"       # minero → su préstamo vivo (<tarea>:<rango>): uno a la vez
MEDICION = "medicion:{}"             # lista: los últimos préstamos de cada minero (JSON con trabajo, segundos y shares)
MEDICION_PRESTAMOS = 20              # cuántos préstamos entran en la medición
MEDICION_SHARES_MINIMOS = 20         # shares esperados (según su bench) para que la medición cuente
MEDICION_SEGUNDOS_MINIMOS = 2.0      # un préstamo más corto no se mide: su tiempo es la red, no el hashing
# BC-75: los mineros que contestan `sin_solucion` sin buscar.
SUSPENDIDOS = "suspendidos"          # cuenta → JSON {nombre, motivo, evidencia, cuando}: no reciben rangos hasta que
                                     # el organizador confirme la baja o los reactive
UMBRAL_SIN_SOLUCION = 1e-6           # un rango sin solución con menos probabilidad que esto es una mentira
MUESTREO_SIN_SOLUCION = 0.05         # de los demás `sin_solucion`, cuántos se reverifican con otro minero
REVERIFICACION = 1_000_000           # se suma al número de rango de una reverificación (el TrP no las ve)


def prob_sin_solucion(tamano, prefijo):
    """Probabilidad de que un rango de `tamano` nonces no tenga ningún hash con `prefijo` (BC-75): e^(-N / 16^p)."""
    return math.exp(-tamano / 16 ** len(prefijo))


def rango_de_la_tarea(rango, rangos):
    """Si `rango` es un número que existe en una tarea partida en `rangos` (BC-92): uno del TrP, de 0 a rangos - 1,
    o su reverificación (BC-75)."""
    return 0 <= rango < rangos or REVERIFICACION <= rango < REVERIFICACION + rangos
# BC-70: lo que se le saca a tx:<id> al confirmarla. Ya está en el bloque: queda {estado, bloque}.
CAMPOS_COPIADOS_TX = ("origen", "destino", "monto", "nonce", "timestamp", "firma", "datos", "motivo")
EPOCA, HISTORIAL = "epoca:{}", "historial:{}"
CAMPOS_HISTORIAL = ("alta", "bloques", "recompensas", "shares", "rangos", "segundos_conectado", "ultimo_aviso",
                    "hashes_por_segundo", "conectado", "resultados_ajenos")
CONECTADOS = "mineros:conectados"    # conjunto: los mineros con conectado = 1, para darlos por desconectados
TIPOS_CUENTA = ("usuario", "minero", "organizador", "validador")
# Versión 3: el estado que sale de la cadena (lo reconstruye comun/estado.py; Redis es caché)
NONCES_CONFIRMADOS = "nonces:confirmados"  # cuenta → último nonce que está en la cadena; «emision» → la última acta o carga
PERMISOS = "permisos"                # clave → prefijo de nombre de los mineros que puede dar de alta y de baja (BC-79)
ALTAS_EN_CAMINO = "altas:en_camino"  # cuenta → JSON {tipo, nombre, tx}: altas validadas que todavía no entraron en un
                                     # bloque. El gateway deja minar a un minero en camino: puede minar su propia alta
ESTADO_SNAPSHOT = "estado:snapshot"  # JSON del estado a una altura (comun/estado.py), para no recorrer todo al arrancar
ESTADO_REVISION = "estado:revision"  # lock: una sola réplica del NCT compara el caché con la cadena a la vez
EPOCA_PUBLICADA = "epoca:{}"         # el mismo epoca:<n>: la lista de shares que se publica (BC-82)

CEROS = "0" * 32
RAIZ_VACIA = CEROS  # la raíz de estado de un árbol sin hojas (la del génesis: el estado antes del bloque 0)
# Los saldos van a Redis con HINCRBY (entero de 64 bits, hasta ~9,2 × 10^18): con este tope no se desborda.
MONTO_MAXIMO = 10**12
NONCE_MAXIMO = 2**63 - 1

# Tarea modelo (bench): el prefijo no aparece nunca, así el minero recorre el rango entero.
BENCH = {"bloque": "bench;", "desde": 0, "hasta": 2**24 - 1, "prefijo": "f" * 32}


def md5(texto):
    return hashlib.md5(texto.encode()).hexdigest()


def hash_bloque(texto, nonce):
    return md5(f"{texto}{nonce}")


def cumple(hash_hex, prefijo):
    return hash_hex.startswith(prefijo)


def recompensa(prefijo, base=10):
    """Lo que cobra el minero que encontró el nonce (la coinbase): más largo el prefijo, más paga."""
    return base * len(prefijo)


# ── Cuentas y firmas (v2) ──

EMISION = "emision"  # reservada: el origen de la plata nueva (génesis, carga, coinbase y participación) y de las actas
                     # del organizador (alta, baja y permiso)
CUENTA = re.compile(r"^[0-9a-f]{64}$")  # clave pública Ed25519 en hex, en minúscula
FIRMA = re.compile(r"^[0-9a-f]{128}$")


NOMBRE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")  # el alias de una cuenta, sólo para mostrar


def cuenta_valida(valor):
    """Una cuenta es una clave pública Ed25519 (32 bytes) en hex. El nombre va aparte, en `cuentas`."""
    return isinstance(valor, str) and bool(CUENTA.match(valor))


def nombre_valido(valor):
    """El alias de una cuenta o de un minero (el MINERO_ID): de 1 a 32 letras, números, «_», «.» o «-»."""
    return isinstance(valor, str) and bool(NOMBRE.match(valor))


def nueva_clave():
    """(privada, pública) en hex. La privada son los 32 bytes de la semilla Ed25519."""
    privada = Ed25519PrivateKey.generate()
    return privada.private_bytes_raw().hex(), _publica(privada)


def clave_publica(privada_hex):
    return _publica(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(privada_hex)))


def _publica(privada):
    return privada.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _bytes(mensaje):
    return mensaje if isinstance(mensaje, bytes) else mensaje.encode()


def firmar(privada_hex, mensaje):
    """Firma Ed25519 (RFC 8032) en hex: 128 caracteres. Es determinística: el mismo mensaje da la misma firma."""
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(privada_hex)).sign(_bytes(mensaje)).hex()


def verificar_firma(cuenta, mensaje, firma):
    """Si `firma` es la firma de `cuenta` sobre `mensaje`. Nunca tira: lo mal formado da False."""
    if not cuenta_valida(cuenta) or not isinstance(firma, str) or not FIRMA.match(firma):
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(cuenta)).verify(bytes.fromhex(firma), _bytes(mensaje))
    except (InvalidSignature, ValueError):
        return False
    return True


# ── Transacción firmada (v2) ──

def mensaje_transaccion(tx):
    """Lo que firma el origen. El nonce es el número de transacción de la cuenta origen (1, 2, 3…)."""
    return f"{tx['origen']}:{tx['destino']}:{tx['monto']}:{tx['nonce']}:{tx['timestamp']}"


def id_transaccion(tx):
    """El hash de lo firmado más la firma: la misma transacción reenviada da el mismo id."""
    return md5(f"{mensaje_transaccion(tx)}:{tx['firma']}")


def transaccion(privada_hex, destino, monto, nonce, timestamp=None):
    """Arma y firma una transferencia (lo que hace el cliente, en el navegador o en un script)."""
    tx = {"origen": clave_publica(privada_hex), "destino": destino, "monto": monto, "nonce": nonce,
          "timestamp": int(time.time()) if timestamp is None else timestamp}
    tx["firma"] = firmar(privada_hex, mensaje_transaccion(tx))
    return {"id": id_transaccion(tx), **tx}


def _entero(valor, minimo, maximo):
    return isinstance(valor, int) and not isinstance(valor, bool) and minimo <= valor <= maximo


def validar_transaccion_firmada(datos):
    """Validación de formato y firma de una transferencia (la hace el backend). Lista de errores; vacía si es válida.

    El saldo y el nonce contra la cuenta los valida el NCT.
    """
    if not isinstance(datos, dict):
        return ["el cuerpo tiene que ser un objeto JSON"]
    errores = [f"{campo}: clave pública Ed25519 en hex (64 caracteres en minúscula)"
               for campo in ("origen", "destino") if not cuenta_valida(datos.get(campo))]
    if not _entero(datos.get("monto"), 1, MONTO_MAXIMO):
        errores.append(f"monto: entero entre 1 y {MONTO_MAXIMO}")
    if not _entero(datos.get("nonce"), 1, NONCE_MAXIMO):
        errores.append("nonce: entero desde 1 (el número de transacción de la cuenta origen)")
    if not _entero(datos.get("timestamp"), 0, NONCE_MAXIMO):
        errores.append("timestamp: entero, segundos Unix")
    if not isinstance(datos.get("firma"), str) or not FIRMA.match(datos["firma"]):
        errores.append("firma: Ed25519 en hex (128 caracteres en minúscula)")
    if datos.get("datos", "") != "":
        errores.append("datos: una transferencia no lleva datos (no van firmados)")
    if errores:
        return errores
    if datos["origen"] == datos["destino"]:
        return ["origen y destino tienen que ser distintos"]
    if not verificar_firma(datos["origen"], mensaje_transaccion(datos), datos["firma"]):
        return ["firma: no es la del origen sobre origen:destino:monto:nonce:timestamp"]
    if "id" in datos and datos["id"] != id_transaccion(datos):
        return ["id: no es el hash de la transacción"]
    return []


# ── Transacciones de emisión (v2): origen «emision», con un id fijo que no se puede repetir ──

def _emision(id_, destino, monto, nonce, timestamp, firma="", datos=""):
    tx = {"id": id_, "origen": EMISION, "destino": destino, "monto": monto, "nonce": nonce,
          "timestamp": timestamp, "firma": firma}
    if datos:
        tx["datos"] = datos
    return tx


def carga(privada_organizador, numero, destino, monto, timestamp):
    """Carga de saldo (modelo exchange): `carga-<n>`, firmada por el organizador. `n` es el nonce de «emision»."""
    tx = _emision(f"carga-{numero}", destino, monto, numero, timestamp)
    tx["firma"] = firmar(privada_organizador, mensaje_transaccion(tx))
    return tx


def validar_carga(tx, organizador):
    """Lista de errores de una carga; vacía si es válida. Que `n` siga a la última (`nonces`) lo controla el NCT."""
    if not isinstance(tx, dict) or tx.get("origen") != EMISION:
        return ["origen: tiene que ser «emision»"]
    errores = []
    if not cuenta_valida(tx.get("destino")):
        errores.append("destino: clave pública Ed25519 en hex (64 caracteres en minúscula)")
    if not _entero(tx.get("monto"), 1, MONTO_MAXIMO):
        errores.append(f"monto: entero entre 1 y {MONTO_MAXIMO}")
    if not _entero(tx.get("nonce"), 1, NONCE_MAXIMO) or tx.get("id") != f"carga-{tx.get('nonce')}":
        errores.append("id: tiene que ser carga-<n>, con n = nonce desde 1")
    if not _entero(tx.get("timestamp"), 0, NONCE_MAXIMO):
        errores.append("timestamp: entero, segundos Unix")
    if tx.get("datos", "") != "":
        errores.append("datos: una carga no lleva datos")
    if not errores and not verificar_firma(organizador, mensaje_transaccion(tx), tx.get("firma")):
        errores.append("firma: no es la del organizador")
    return errores


def coinbase(altura, minero, monto, timestamp):
    """La recompensa del bloque, adentro de la raíz y primera: `cb-<altura>-<minero>`. La arma el gateway."""
    return _emision(f"cb-{altura}-{minero}", minero, monto, 0, timestamp)


def participacion(epoca_, minero, monto, timestamp):
    """El pago por shares de una época: `part-<época>-<minero>`. La arma el NCT al cerrar la época."""
    return _emision(f"part-{epoca_}-{minero}", minero, monto, 0, timestamp)


def es_emision(tx):
    return tx.get("origen") == EMISION


def es_coinbase(tx):
    return es_emision(tx) and str(tx.get("id", "")).startswith("cb-")


# Los tipos de transacción, por el prefijo del id. Las de origen «emision» las arma el sistema o las firma el
# organizador; las actas (alta, baja, permiso) son las decisiones del organizador (BC-79).
_TIPOS_EMISION = (("genesis-", "genesis"), ("organizador-", "organizador"), ("cb-", "coinbase"), ("carga-", "carga"),
                  ("part-", "participacion"), ("shares-", "shares"), ("alta-", "alta"), ("baja-", "baja"),
                  ("permiso-", "permiso"))
ACTAS = ("alta", "baja", "permiso")
MOTIVO_MAXIMO = 100


def tipo_de(tx):
    """transferencia, genesis, organizador, coinbase, carga, participacion, shares, alta, baja, permiso o desconocida.

    Una alta o baja con origen en una cuenta es **delegada**: la firma la clave a la que el organizador le dio un
    permiso (el escalador, para las VMs), y sólo vale para mineros con ese prefijo de nombre.
    """
    id_ = str(tx.get("id", ""))
    if tx.get("origen") != EMISION:
        return next((acta for acta in ("alta", "baja") if id_.startswith(f"{acta}-")), "transferencia")
    return next((tipo for prefijo, tipo in _TIPOS_EMISION if id_.startswith(prefijo)), "desconocida")


def es_acta(tx):
    return tipo_de(tx) in ACTAS


def es_delegada(tx):
    return es_acta(tx) and tx.get("origen") != EMISION


def id_acta(tipo, origen, numero):
    """`alta-<n>` (del organizador: n es el nonce de «emision», el mismo de las cargas) o `alta-<clave>-<n>`
    (delegada: n es el nonce de la clave que firma)."""
    return f"{tipo}-{numero}" if origen == EMISION else f"{tipo}-{origen}-{numero}"


def mensaje_acta(tx):
    """Lo que firma el organizador (o la clave con permiso): el tipo adelante, así una firma de carga no sirve de alta."""
    return f"{tipo_de(tx)}:{tx['origen']}:{tx['destino']}:{tx['nonce']}:{tx['timestamp']}:{tx.get('datos', '')}"


def acta(privada_hex, tipo, numero, destino, datos, timestamp, delegada=False):
    """Una decisión firmada que va en la cadena: alta, baja o permiso (BC-79)."""
    origen = clave_publica(privada_hex) if delegada else EMISION
    tx = {"id": id_acta(tipo, origen, numero), "origen": origen, "destino": destino, "monto": 0, "nonce": numero,
          "timestamp": timestamp, "datos": datos}
    tx["firma"] = firmar(privada_hex, mensaje_acta(tx))
    return tx


def alta(privada_hex, numero, cuenta, tipo, nombre, timestamp, delegada=False):
    """`alta-<n>`: la cuenta queda aprobada como `tipo` cuando el bloque se confirma."""
    return acta(privada_hex, "alta", numero, cuenta, f"{tipo}:{nombre}", timestamp, delegada)


def baja(privada_hex, numero, cuenta, motivo, timestamp, delegada=False):
    """`baja-<n>`: la cuenta deja de operar cuando el bloque se confirma (el saldo queda donde está)."""
    return acta(privada_hex, "baja", numero, cuenta, motivo, timestamp, delegada)


def permiso(privada_hex, numero, clave, prefijo, timestamp):
    """`permiso-<n>`: `clave` puede dar de alta y de baja mineros cuyo nombre empieza con `prefijo`. Vacío: lo revoca."""
    return acta(privada_hex, "permiso", numero, clave, prefijo, timestamp)


def datos_alta(tx):
    """(tipo, nombre) de un alta o de una transacción del génesis."""
    tipo, _, nombre = str(tx.get("datos", "")).partition(":")
    return tipo, nombre


def validar_acta(tx, organizador):
    """Formato y firma de un acta; vacío si sirve. Lo que depende del estado (el permiso, a quién se da de baja) lo
    controlan el NCT y comun/estado.py."""
    if not isinstance(tx, dict):
        return ["el cuerpo tiene que ser un objeto JSON"]
    tipo, origen = tipo_de(tx), tx.get("origen")
    if tipo not in ACTAS:
        return ["id: alta-<n>, baja-<n> o permiso-<n>"]
    errores = []
    if origen != EMISION and (not cuenta_valida(origen) or tipo == "permiso"):
        errores.append("origen: «emision» (el organizador) o la clave con permiso, sólo en altas y bajas")
    if not cuenta_valida(tx.get("destino")):
        errores.append("destino: clave pública Ed25519 en hex (64 caracteres en minúscula)")
    if tx.get("monto") != 0 or isinstance(tx.get("monto"), bool):
        errores.append("monto: 0 (un acta no mueve plata)")
    if not _entero(tx.get("nonce"), 1, NONCE_MAXIMO) or tx.get("id") != id_acta(tipo, origen, tx.get("nonce")):
        errores.append(f"id: {tipo}-<n> (o {tipo}-<clave>-<n> si es delegada), con n = nonce desde 1")
    if not _entero(tx.get("timestamp"), 0, NONCE_MAXIMO):
        errores.append("timestamp: entero, segundos Unix")
    datos = tx.get("datos", "")
    if not isinstance(datos, str):
        errores.append("datos: texto")
    elif tipo == "alta":
        tipo_cuenta, separador, nombre = datos.partition(":")
        if not separador or tipo_cuenta not in TIPOS_ALTA or not nombre_valido(nombre):
            errores.append(f"datos: <tipo>:<nombre>, con tipo {' | '.join(TIPOS_ALTA)} y un nombre de 1 a 32 caracteres")
    elif tipo == "baja":
        if len(datos) > MOTIVO_MAXIMO or "|" in datos or "\n" in datos:
            errores.append(f"datos: el motivo, hasta {MOTIVO_MAXIMO} caracteres, sin «|» ni saltos de línea")
    elif datos and not nombre_valido(datos):
        errores.append("datos: el prefijo de nombre (de 1 a 32 letras, números, «_», «.» o «-»), o vacío para revocar")
    if errores:
        return errores
    firmante = organizador if origen == EMISION else origen
    if not firmante:
        return ["no hay ORGANIZADOR_CLAVE: nadie puede firmar actas"]
    if not verificar_firma(firmante, mensaje_acta(tx), tx.get("firma")):
        return ["firma: no es la del organizador" if origen == EMISION else "firma: no es la del origen"]
    return []


def validar_de_entrada(datos, organizador):
    """Lo que valida el backend al recibir: una transferencia, una carga o un acta. Lista de errores; vacía si sirve."""
    if isinstance(datos, dict) and (datos.get("origen") == EMISION or es_acta(datos)):
        if es_acta(datos):
            return validar_acta(datos, organizador)
        if not organizador:
            return ["origen: «emision» es para las cargas y las actas, y no hay ORGANIZADOR_CLAVE"]
        return validar_carga(datos, organizador)
    return validar_transaccion_firmada(datos)


# ── Shares de la época, publicados y en la cadena (BC-82) ──

def lista_shares(detalle):
    """`minero:shares:racha` de cada minero con shares, ordenados: lo que se publica en /api/epocas/<n>."""
    return [f"{minero}:{d['shares']}:{d['racha']}" for minero, d in sorted(detalle.items()) if d.get("shares", 0) > 0]


def raiz_shares(detalle):
    return md5("|".join(lista_shares(detalle)))


def shares_epoca(epoca_, fondo, raiz, timestamp):
    """`shares-<época>`: compromete en la cadena la lista de shares (y el fondo) con que se pagó la época."""
    return _emision(f"shares-{epoca_}", "", 0, 0, timestamp, datos=f"{fondo}:{raiz}")


def transaccion_guardada(id_, campos):
    """tx:<id> como lo devuelve Redis (todo texto) → la transacción con sus tipos, como va en el bloque."""
    tx = {"id": id_, "origen": campos["origen"], "destino": campos["destino"], "monto": int(campos["monto"]),
          "nonce": int(campos.get("nonce", 0)), "timestamp": int(campos["timestamp"]),
          "firma": campos.get("firma", "")}
    if campos.get("datos"):
        tx["datos"] = campos["datos"]
    return tx


# ── Bloque (v2) ──

def serializar_transaccion(tx):
    """id:origen:destino:monto:nonce:timestamp:datos:firma (v3). `datos` va vacío salvo en las actas, el génesis y
    los shares de la época; la firma, vacía en las de emisión que no firma nadie (génesis, coinbase, participación)."""
    return (f"{tx['id']}:{tx['origen']}:{tx['destino']}:{tx['monto']}:{tx['nonce']}:{tx['timestamp']}:"
            f"{tx.get('datos', '')}:{tx['firma']}")


def raiz_transacciones(transacciones):
    """MD5 de las transacciones en el orden del bloque, separadas por «|»."""
    return md5("|".join(serializar_transaccion(tx) for tx in transacciones))


def raiz_bloque(transacciones, raiz_estado):
    """Lo que va en el texto (v3): MD5(raíz de transacciones | raíz de estado). Sigue en 32 caracteres, así el texto
    mide 77 y los mineros no cambian."""
    return md5(f"{raiz_transacciones(transacciones)}|{raiz_estado}")


def _texto(previous_hash, timestamp, transacciones, raiz_estado):
    return f"{previous_hash};{timestamp};{raiz_bloque(transacciones, raiz_estado)};"


def texto_de_bloque(bloque):
    """El texto de un bloque guardado (con `raiz_estado`): lo que recalculan los que verifican."""
    return _texto(bloque["previous_hash"], bloque["timestamp"], bloque["transactions"], bloque["raiz_estado"])


def transacciones_del_bloque(tarea, minero):
    """Las transacciones del bloque si lo gana `minero`: su coinbase primero y después las de la caja."""
    cb = coinbase(tarea["altura"], minero, recompensa(tarea["prefijo"]), tarea["timestamp"])
    return [cb, *tarea["transacciones"]]


def texto_para(tarea, minero):
    """El string que mina `minero` (el nonce se le concatena al final, en decimal).

    `tarea` necesita previous_hash, timestamp, altura, prefijo, transacciones y raiz_estado (la del estado al
    cerrar el bloque anterior, igual para todos). Lleva la raíz y no las transacciones, así mide siempre 77
    caracteres (el minero CUDA acepta hasta 120). Como la coinbase está en la raíz, cada minero mina un texto
    distinto: un nonce no le sirve a otro.
    """
    return _texto(tarea["previous_hash"], tarea["timestamp"], transacciones_del_bloque(tarea, minero),
                  tarea["raiz_estado"])


def tarea_guardada(campos):
    """tarea:<id> como lo devuelve Redis (todo texto) → lo que necesita `texto_para`."""
    return {"previous_hash": campos["previous_hash"], "timestamp": int(campos["timestamp"]),
            "altura": int(campos["altura"]), "prefijo": campos["prefijo"],
            "transacciones": json.loads(campos["transacciones"]), "raiz_estado": campos["raiz_estado"]}


def bloque_genesis(saldos, timestamp, organizador="", nombres=None):
    """Bloque 0: sin minar (nonce 0, sin prefijo ni coinbase).

    v3: primero `organizador-<cuenta>` (la clave del organizador queda en la cadena: los que verifican la leen de
    acá) y después un `genesis-<cuenta>` por cada saldo inicial, que además le da el alta como usuario con su nombre
    (`datos` = usuario:<nombre>). Su raíz de estado es la del árbol vacío.
    """
    nombres = nombres or {}
    transacciones = [_emision(f"organizador-{organizador}", organizador, 0, 0, timestamp)] if organizador else []
    transacciones += [_emision(f"genesis-{cuenta}", cuenta, monto, 0, timestamp, datos=f"usuario:{nombres.get(cuenta, '')}")
                      for cuenta, monto in sorted(saldos.items())]
    texto = _texto(CEROS, timestamp, transacciones, RAIZ_VACIA)
    return {"previous_hash": CEROS, "timestamp": timestamp, "transactions": transacciones, "nonce": 0,
            "block_hash": hash_bloque(texto, 0), "prefijo": "", "minero": "", "recompensa": 0, "altura": 0,
            "raiz_estado": RAIZ_VACIA}


def potencia_efectiva(nodo):
    """Los hashes/s con que cuenta el sistema para un minero (BC-74): su `bench`, salvo que el gateway haya medido
    menos. `nodo` es nodo:<cuenta> como lo devuelve Redis (todo texto).

    - `medido_estado` = "medido": min(bench, medido). Un bench inflado no sirve; uno que no mina mide 0.
    - "cota" (sus préstamos llegan al tope de shares: sólo se sabe que es al menos eso) o sin medición: el bench.
    """
    try:
        bench = float(nodo.get("bench") or 0)
        if nodo.get("medido_estado") == "medido":
            return min(bench, float(nodo.get("medido") or 0))
    except (TypeError, ValueError):
        return 0.0
    return bench


# ── Shares y épocas (v2) ──

BLOQUES_POR_EPOCA = 10


def prefijo_share(prefijo):
    """Un share es un hash con un carácter menos de prefijo. Con prefijo de 1 no hay shares (todo hash serviría)."""
    return prefijo[:-1] if len(prefijo) >= 2 else None


def es_share(hash_hex, prefijo):
    corto = prefijo_share(prefijo)
    return corto is not None and cumple(hash_hex, corto)


def epoca(altura):
    """Época de un bloque: del 0 al 9 son la 0, del 10 al 19 la 1…"""
    return altura // BLOQUES_POR_EPOCA


FONDO_EPOCA = 100      # lo que se reparte por época entre los que mandaron shares
RACHA_MAXIMA = 6       # épocas seguidas con shares que cuentan: +10 % cada una después de la primera, hasta +50 %
SHARES_POR_RANGO = 64  # tope de shares que el minero junta por rango (y que el gateway acepta por pedido)


def peso_participacion(shares, racha):
    """shares × (1 + bonus), en décimos para que sea entero. La racha 1 no suma; la 6 o más, +50 %."""
    return shares * (10 + min(max(racha, 1), RACHA_MAXIMA) - 1)


def repartir(fondo, shares, rachas):
    """{minero: monto} del fondo de una época, en proporción a shares × racha. Enteros: el resto se pierde."""
    pesos = {minero: peso_participacion(n, rachas.get(minero, 1)) for minero, n in shares.items() if n > 0}
    total = sum(pesos.values())
    montos = {minero: fondo * peso // total for minero, peso in pesos.items()} if total else {}
    return {minero: monto for minero, monto in sorted(montos.items()) if monto > 0}


def es_primer_bloque_de_epoca(altura):
    """En el primer bloque de cada época (10, 20…) entra el pago de la anterior."""
    return altura > 0 and altura % BLOQUES_POR_EPOCA == 0


# ── Pedidos firmados de los mineros al gateway (v2) ──

VENTANA_PEDIDO = 30  # segundos: un pedido con la hora más corrida que esto se rechaza (no se puede repetir uno viejo)


def mensaje_pedido(metodo, ruta, tiempo, cuerpo=b""):
    """Lo que firma el minero: método, ruta (con la query, como va en el pedido), tiempo y el cuerpo tal cual."""
    return f"{metodo.upper()}\n{ruta}\n{tiempo}\n".encode() + _bytes(cuerpo)


# El encabezado con la cuenta que firma: los mineros al gateway (BC-57) y el organizador al panel (BC-58).
MINERO, ORGANIZADOR = "X-Minero", "X-Organizador"
_QUIEN = {MINERO: "del minero", ORGANIZADOR: "del organizador"}


def firmar_pedido(privada_hex, metodo, ruta, cuerpo=b"", tiempo=None, quien=MINERO):
    """Los encabezados `quien` (X-Minero o X-Organizador), X-Tiempo y X-Firma de un pedido."""
    tiempo = int(time.time()) if tiempo is None else tiempo
    return {quien: clave_publica(privada_hex), "X-Tiempo": str(tiempo),
            "X-Firma": firmar(privada_hex, mensaje_pedido(metodo, ruta, tiempo, cuerpo))}


def problema_de_cabeceras(encabezados, ahora=None, quien=MINERO):
    """El motivo por el que un pedido firmado no puede servir, mirando sólo las cabeceras (la cuenta y la hora); vacío
    si puede. BC-94: el gateway lo mira antes de leer el cuerpo, así un pedido viejo o sin cuenta no le hace leer nada.

    `encabezados` puede ser un dict o los headers de Starlette (que no distinguen mayúsculas).
    `quien` es el encabezado con la cuenta: X-Minero o X-Organizador.
    """
    cuenta, tiempo = _cabecera(encabezados, quien), _cabecera(encabezados, "X-Tiempo")
    if not cuenta_valida(cuenta):
        return f"{quien}: clave pública Ed25519 en hex"
    if not isinstance(tiempo, str) or not re.fullmatch(r"[0-9]{1,12}", tiempo):
        return "X-Tiempo: segundos Unix"
    ahora = int(time.time()) if ahora is None else ahora
    if abs(ahora - int(tiempo)) > VENTANA_PEDIDO:
        return f"X-Tiempo: corrido más de {VENTANA_PEDIDO} s"
    return ""


def _cabecera(encabezados, nombre):
    return encabezados.get(nombre) or encabezados.get(nombre.lower())


def verificar_pedido(encabezados, metodo, ruta, cuerpo=b"", ahora=None, quien=MINERO):
    """(cuenta, None) si el pedido está bien firmado y en hora; (None, motivo) si no (ver problema_de_cabeceras)."""
    motivo = problema_de_cabeceras(encabezados, ahora, quien)
    if motivo:
        return None, motivo
    cuenta, tiempo, firma = (_cabecera(encabezados, n) for n in (quien, "X-Tiempo", "X-Firma"))
    if not verificar_firma(cuenta, mensaje_pedido(metodo, ruta, tiempo, cuerpo), firma):
        return None, f"X-Firma: no es la {_QUIEN.get(quien, quien)} sobre el pedido"
    return cuenta, None


# ── Pedidos de alta (v2, BC-58) ──

TIPOS_ALTA = ("usuario", "minero", "validador")  # el organizador no pide el alta: está en el génesis


def mensaje_alta(tipo, cuenta, nombre):
    """Lo que firma quien pide el alta: demuestra que tiene la privada de la cuenta que pide."""
    return f"alta:{tipo}:{cuenta}:{nombre}"


def pedido_alta(privada_hex, tipo, nombre):
    """El cuerpo de POST /api/altas: {tipo, clave_publica, nombre, firma} (lo que arma la página «Pedir alta»)."""
    cuenta = clave_publica(privada_hex)
    return {"tipo": tipo, "clave_publica": cuenta, "nombre": nombre,
            "firma": firmar(privada_hex, mensaje_alta(tipo, cuenta, nombre))}


def validar_pedido_alta(datos):
    """Los errores de un pedido de alta; vacío si sirve."""
    if not isinstance(datos, dict):
        return ["el cuerpo tiene que ser un objeto JSON"]
    errores = []
    if datos.get("tipo") not in TIPOS_ALTA:
        errores.append(f"tipo: {' | '.join(TIPOS_ALTA)}")
    if not cuenta_valida(datos.get("clave_publica")):
        errores.append("clave_publica: clave pública Ed25519 en hex (64 caracteres en minúscula)")
    if not nombre_valido(datos.get("nombre")):
        errores.append("nombre: de 1 a 32 letras, números, «_», «.» o «-»")
    if errores:
        return errores
    if not verificar_firma(datos["clave_publica"], mensaje_alta(datos["tipo"], datos["clave_publica"], datos["nombre"]),
                           datos.get("firma")):
        return ["firma: no es la de clave_publica sobre alta:tipo:clave_publica:nombre"]
    return []


# ── Nodos verificadores y validadores (BC-83, BC-86) ──

FIRMAS = "firmas:{}"                 # hash por altura: validador → JSON {block_hash, firma, cuando}
FIRMAS_ULTIMA = "firmas:ultima"      # sorted set: validador → la última altura que firmó en esta cadena
FIRMAS_DOBLES = "firmas:dobles"      # lista: las pruebas de doble firma (dos bloques distintos a la misma altura)
FINALIZADA = "finalizada"            # hash {altura, block_hash}: el último bloque finalizado
BLOQUE_FINALIZADO = "bloque.finalizado"


def mensaje_firma_bloque(altura, block_hash):
    """Lo que firma un validador: «verifiqué la cadena hasta este bloque». Firmar uno firma también los anteriores."""
    return f"firma:{altura}:{block_hash}"


def firma_bloque(privada_hex, altura, block_hash):
    return {"validador": clave_publica(privada_hex), "altura": altura, "block_hash": block_hash,
            "firma": firmar(privada_hex, mensaje_firma_bloque(altura, block_hash))}


def validar_firma_bloque(datos):
    """Formato y firma de un POST /api/firmas; vacío si sirve. Que sea un validador de la cadena lo mira quien recibe."""
    if not isinstance(datos, dict):
        return ["el cuerpo tiene que ser un objeto JSON"]
    errores = []
    if not cuenta_valida(datos.get("validador")):
        errores.append("validador: clave pública Ed25519 en hex")
    if not _entero(datos.get("altura"), 0, NONCE_MAXIMO):
        errores.append("altura: entero desde 0")
    if not isinstance(datos.get("block_hash"), str) or not re.fullmatch(r"[0-9a-f]{32}", datos["block_hash"]):
        errores.append("block_hash: MD5 en hex")
    if errores:
        return errores
    if not verificar_firma(datos["validador"], mensaje_firma_bloque(datos["altura"], datos["block_hash"]),
                           datos.get("firma")):
        return ["firma: no es la del validador sobre firma:<altura>:<block_hash>"]
    return []


def umbral_finalidad(validadores):
    """Firmas que finalizan un bloque: 2/3 de los validadores, redondeando para arriba (2 de 3). Sin validadores, nada."""
    return math.ceil(2 * validadores / 3) if validadores else 0


def altura_finalizada(validadores, ultimas):
    """La mayor altura firmada por al menos el umbral de `validadores` (un conjunto), según `ultimas` (validador → la
    última altura que firmó). -1 si nada está finalizado."""
    umbral = umbral_finalidad(len(validadores))
    alturas = sorted((ultimas[v] for v in validadores if v in ultimas), reverse=True)
    return alturas[umbral - 1] if umbral and len(alturas) >= umbral else -1


def mensaje_atestacion(nodo, altura, punta, raiz, tiempo):
    """Lo que firma un nodo verificador al decir hasta dónde verificó (BC-83): así se puede citar lo que dijo."""
    return f"atestacion:{nodo}:{altura}:{punta}:{raiz}:{tiempo}"
