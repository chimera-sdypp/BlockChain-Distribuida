"""El estado que sale de la cadena (BC-80): saldos, nonces, cuentas y permisos, y su raíz de Merkle.

Un solo código decide qué estado resulta de una cadena: lo usan el NCT (al arrancar y antes de armar cada bloque),
la auditoría, los nodos verificadores, los validadores y la herramienta de claves. Redis es caché: si difiere,
gana la cadena.

    estado = Estado()
    for bloque in bloques:                       # en orden, desde el génesis
        problemas, avisos = aplicar_bloque(estado, bloque, reglas)
        if problemas: ...                        # en modo estricto, el estado no cambia

Reglas de un bloque minado (altura ≥ 1), además del hash, el prefijo y que encadene:
- su `raiz_estado` es la raíz del estado al cerrar el bloque anterior;
- la primera transacción es la coinbase del ganador;
- en el primer bloque de una época van `shares-<e>` (la raíz de la lista de shares de la época anterior) y los
  `part-<e>-<minero>` que salen de esa lista (BC-82), y en ningún otro;
- el resto, en orden: transferencias (firma, cuenta aprobada, nonce siguiente y saldo), cargas (firma del
  organizador y nonce de «emision») y actas (alta, baja y permiso, BC-79).

Las transacciones se aplican una por una: una transferencia puede gastar lo que recibió antes en el mismo bloque.
"""

import json
from dataclasses import dataclass, field

from comun import contrato

# ── Árbol de Merkle ──
# Las hojas y los nodos internos se hashean con un prefijo distinto («h:» y «n:»): así una hoja no se puede
# hacer pasar por un nodo. Un nodo sin hermano sube igual al nivel siguiente.


def hash_hoja(hoja):
    return contrato.md5("h:" + hoja)


def hash_nodo(izquierda, derecha):
    return contrato.md5("n:" + izquierda + derecha)


def raiz_merkle(hashes):
    """La raíz de una lista de hashes de hojas, en su orden. Sin hojas: `RAIZ_VACIA` (32 ceros)."""
    nivel = list(hashes)
    if not nivel:
        return contrato.RAIZ_VACIA
    while len(nivel) > 1:
        nivel = [hash_nodo(nivel[i], nivel[i + 1]) if i + 1 < len(nivel) else nivel[i]
                 for i in range(0, len(nivel), 2)]
    return nivel[0]


def rama_merkle(hashes, indice):
    """Los hermanos desde la hoja `indice` hasta la raíz: [{lado: «izq» | «der», hash}]. Un nodo sin hermano no suma."""
    rama, nivel = [], list(hashes)
    while len(nivel) > 1:
        hermano = indice ^ 1
        if hermano < len(nivel):
            rama.append({"lado": "izq" if hermano < indice else "der", "hash": nivel[hermano]})
        nivel = [hash_nodo(nivel[i], nivel[i + 1]) if i + 1 < len(nivel) else nivel[i]
                 for i in range(0, len(nivel), 2)]
        indice //= 2
    return rama


def raiz_de_rama(hoja, rama):
    """La raíz que da `hoja` (el texto) con su rama: si coincide con la del bloque, la hoja está en ese estado."""
    actual = hash_hoja(hoja)
    for paso in rama:
        actual = hash_nodo(paso["hash"], actual) if paso["lado"] == "izq" else hash_nodo(actual, paso["hash"])
    return actual


# ── Las hojas del estado ──

PERMISO = "permiso:"  # las hojas de los permisos: permiso:<clave>:<prefijo> (las de las cuentas empiezan con hex)


def hoja_cuenta(cuenta, tipo, saldo, nonce):
    """`cuenta:tipo:saldo:nonce` (tipo «-» si no está aprobada). Una cuenta sin nada no tiene hoja."""
    if not saldo and not nonce and not tipo:
        return None
    return f"{cuenta}:{tipo or '-'}:{saldo}:{nonce}"


def hojas_estado(saldos, nonces, tipos, permisos):
    """[(clave, hoja)] ordenadas por clave (es lo mismo que ordenar las hojas). `tipos`: cuenta → tipo."""
    hojas = []
    for cuenta in set(saldos) | set(nonces) | set(tipos):
        hoja = hoja_cuenta(cuenta, tipos.get(cuenta, ""), int(saldos.get(cuenta, 0) or 0), int(nonces.get(cuenta, 0) or 0))
        if hoja:
            hojas.append((cuenta, hoja))
    hojas += [(PERMISO + clave, f"{PERMISO}{clave}:{prefijo}") for clave, prefijo in permisos.items() if prefijo]
    return sorted(hojas)


def raiz_estado(saldos, nonces, tipos, permisos):
    return raiz_merkle(hash_hoja(hoja) for _, hoja in hojas_estado(saldos, nonces, tipos, permisos))


def prueba(hojas, cuenta):
    """{hoja, rama, raiz} de `cuenta` sobre la lista de `hojas_estado`; hoja None si la cuenta no tiene nada."""
    hashes = [hash_hoja(hoja) for _, hoja in hojas]
    indice = next((i for i, (clave, _) in enumerate(hojas) if clave == cuenta), None)
    if indice is None:
        return {"hoja": None, "rama": [], "raiz": raiz_merkle(hashes)}
    return {"hoja": hojas[indice][1], "rama": rama_merkle(hashes, indice), "raiz": raiz_merkle(hashes)}


# ── El estado ──

class Estado:
    """El estado al cerrar el bloque `altura` (cuyo hash es `punta`). Antes del génesis, altura −1."""

    VERSION = 3

    def __init__(self):
        self.altura, self.punta, self.timestamp = -1, None, 0
        self.saldos, self.nonces, self.cuentas, self.permisos = {}, {}, {}, {}
        self.organizador = ""
        self.shares = {}  # época → la raíz de su lista de shares, la que quedó en la cadena (BC-82)
        self._hashes, self._sucias, self._orden = {}, set(), None

    # Las hojas se recalculan sólo para lo que cambió: la raíz de cada bloque cuesta lo que se tocó más un árbol.
    def _ensuciar(self, clave):
        self._sucias.add(clave)

    def tipos(self):
        return {cuenta: datos["tipo"] for cuenta, datos in self.cuentas.items()}

    def _hoja(self, clave):
        if clave.startswith(PERMISO):
            prefijo = self.permisos.get(clave[len(PERMISO):])
            return f"{clave}:{prefijo}" if prefijo else None
        datos = self.cuentas.get(clave)
        return hoja_cuenta(clave, datos["tipo"] if datos else "", self.saldos.get(clave, 0), self.nonces.get(clave, 0))

    def _al_dia(self):
        for clave in self._sucias:
            hoja = self._hoja(clave)
            if hoja is None:
                if self._hashes.pop(clave, None) is not None:
                    self._orden = None
            else:
                if clave not in self._hashes:
                    self._orden = None
                self._hashes[clave] = hash_hoja(hoja)
        self._sucias.clear()
        if self._orden is None:
            self._orden = sorted(self._hashes)

    def raiz(self):
        """La raíz de estado: la que tiene que llevar el bloque siguiente."""
        self._al_dia()
        return raiz_merkle(self._hashes[clave] for clave in self._orden)

    def hojas(self):
        self._al_dia()
        return [(clave, self._hoja(clave)) for clave in self._orden]

    def prueba(self, cuenta):
        return prueba(self.hojas(), cuenta)

    def sumar(self, cuenta, monto):
        self.saldos[cuenta] = self.saldos.get(cuenta, 0) + monto
        self._ensuciar(cuenta)

    def poner_nonce(self, cuenta, nonce):
        self.nonces[cuenta] = nonce
        self._ensuciar(cuenta)

    def poner_cuenta(self, cuenta, datos):
        if datos is None:
            self.cuentas.pop(cuenta, None)
        else:
            self.cuentas[cuenta] = datos
        self._ensuciar(cuenta)

    def poner_permiso(self, clave, prefijo):
        if prefijo:
            self.permisos[clave] = prefijo
        else:
            self.permisos.pop(clave, None)
        self._ensuciar(PERMISO + clave)

    # ── Snapshot ──

    def a_dict(self):
        return {"version": self.VERSION, "altura": self.altura, "punta": self.punta, "timestamp": self.timestamp,
                "saldos": self.saldos, "nonces": self.nonces, "cuentas": self.cuentas, "permisos": self.permisos,
                "organizador": self.organizador, "shares": {str(e): r for e, r in self.shares.items()},
                "raiz": self.raiz()}

    @classmethod
    def desde_dict(cls, datos):
        """El estado de un snapshot. Tira ValueError si la raíz no es la de su contenido (lo tocaron)."""
        if datos.get("version") != cls.VERSION:
            raise ValueError(f"snapshot de la versión {datos.get('version')}, no de la {cls.VERSION}")
        estado = cls()
        estado.altura, estado.punta, estado.timestamp = datos["altura"], datos["punta"], datos["timestamp"]
        estado.saldos = {c: int(s) for c, s in datos["saldos"].items()}
        estado.nonces = {c: int(n) for c, n in datos["nonces"].items()}
        estado.cuentas, estado.permisos = dict(datos["cuentas"]), dict(datos["permisos"])
        estado.organizador = datos.get("organizador", "")
        estado.shares = {int(e): r for e, r in datos.get("shares", {}).items()}
        estado._sucias = set(estado.saldos) | set(estado.nonces) | set(estado.cuentas) | \
            {PERMISO + clave for clave in estado.permisos}
        if "raiz" in datos and estado.raiz() != datos["raiz"]:
            raise ValueError("la raíz del snapshot no es la de su contenido")
        return estado

    def copia(self):
        return Estado.desde_dict(json.loads(json.dumps(self.a_dict())))


@dataclass
class Reglas:
    """Lo que el que verifica sabe de antemano (no se lo cree a la cadena ni al servidor).

    - `organizador`: si se conoce, el del génesis tiene que ser éste. Si no, se toma el del génesis.
    - `fondo_epoca`: lo que se reparte por época (FONDO_EPOCA).
    - `shares`: función época → detalle {minero: {shares, racha, …}} de lo publicado (/api/epocas/<n>), o None si no
      se consiguió. Con eso se verifica el pago de cada época; sin eso, queda un aviso.
    - `verificar_firmas`: False sólo para lo que ya se verificó (el NCT siguiendo sus propios bloques).
    """
    organizador: str = ""
    fondo_epoca: int = contrato.FONDO_EPOCA
    shares: object = None
    verificar_firmas: bool = True
    _detalles: dict = field(default_factory=dict)

    def detalle(self, epoca, raiz):
        """La lista publicada de `epoca`, sólo si da la `raiz` de la cadena: None si no se consiguió, False si no da."""
        if epoca in self._detalles:
            return self._detalles[epoca]
        detalle = self.shares(epoca) if self.shares else None
        if detalle is not None and contrato.raiz_shares(detalle) != raiz:
            detalle = False
        if detalle is not None:
            self._detalles[epoca] = detalle
        return detalle


class _Cambios:
    """Lo que cambia un bloque, encima del estado: si el bloque no sirve, se tira sin tocar el estado."""

    def __init__(self, estado):
        self.estado = estado
        self.saldos, self.nonces, self.cuentas, self.permisos = {}, {}, {}, {}
        self.organizador = estado.organizador
        self.shares = None  # las raíces de shares, si el bloque paga una época

    def saldo(self, cuenta):
        return self.saldos.get(cuenta, self.estado.saldos.get(cuenta, 0))

    def nonce(self, cuenta):
        return self.nonces.get(cuenta, self.estado.nonces.get(cuenta, 0))

    def cuenta(self, cuenta):
        return self.cuentas[cuenta] if cuenta in self.cuentas else self.estado.cuentas.get(cuenta)

    def permiso(self, clave):
        return self.permisos[clave] if clave in self.permisos else self.estado.permisos.get(clave)

    def sumar(self, cuenta, monto):
        self.saldos[cuenta] = self.saldo(cuenta) + monto

    def aplicar(self):
        for cuenta, saldo in self.saldos.items():
            self.estado.sumar(cuenta, saldo - self.estado.saldos.get(cuenta, 0))
        for cuenta, nonce in self.nonces.items():
            self.estado.poner_nonce(cuenta, nonce)
        for cuenta, datos in self.cuentas.items():
            self.estado.poner_cuenta(cuenta, datos)
        for clave, prefijo in self.permisos.items():
            self.estado.poner_permiso(clave, prefijo)
        self.estado.organizador = self.organizador


def _sin_datos_vacios(tx):
    return {k: v for k, v in tx.items() if not (k == "datos" and v == "")}


def _igual(tx, esperada):
    return _sin_datos_vacios(tx) == _sin_datos_vacios(esperada)


ALTA_REPETIDA = "la cuenta ya tiene alta"


def problema_de_acta(tx, cuenta_de, permiso_de, organizador):
    """Lo que depende del estado en un acta ya firmada; None si vale. `cuenta_de(c)` da {tipo, nombre} o None,
    `permiso_de(clave)` el prefijo o None. Lo usan el NCT (con el caché, al validar) y `aplicar_bloque`."""
    tipo, destino = contrato.tipo_de(tx), tx["destino"]
    if tipo in ("alta", "baja") and destino == organizador:
        return "el organizador no se da de alta ni de baja: está en el génesis"
    existente = cuenta_de(destino)
    if tipo == "alta" and existente:
        # BC-93: aprobar dos veces la misma cuenta no puede volver a darle el alta (ni su carga inicial). Después de
        # una baja la cuenta ya no está: se puede volver a dar de alta (el escalador reusa la clave de cada VM).
        return ALTA_REPETIDA
    if not contrato.es_delegada(tx):
        return None
    prefijo = permiso_de(tx["origen"])
    if not prefijo:
        return "sin permiso: la clave no tiene un permiso del organizador"
    if tipo == "alta":
        tipo_cuenta, nombre = contrato.datos_alta(tx)
        if tipo_cuenta != "minero" or not nombre.startswith(prefijo):
            return f"sin permiso: sólo puede dar de alta mineros {prefijo}*"
    elif not existente or existente.get("tipo") != "minero" or not str(existente.get("nombre", "")).startswith(prefijo):
        return f"sin permiso: sólo puede dar de baja mineros {prefijo}*"
    return None


def _datos_cuenta(tx):
    tipo, nombre = contrato.datos_alta(tx)
    return {"tipo": tipo, "nombre": nombre, "alta": tx["timestamp"]}


def aplicar_transaccion(cambios, tx, reglas):
    """Una transacción de la caja (transferencia, carga o acta) sobre `cambios`. Devuelve el problema (y no cambia
    nada) o None. Las de emisión del sistema (coinbase, participación, génesis) no van por acá."""
    tipo = contrato.tipo_de(tx)
    try:
        if tipo == "transferencia":
            return _transferencia(cambios, tx, reglas)
        if tipo == "carga":
            return _carga(cambios, tx, reglas)
        if tipo in contrato.ACTAS:
            return _acta(cambios, tx, reglas)
    except (KeyError, TypeError, ValueError) as error:
        return f"{tx.get('id')}: mal formada ({error!r})"
    return f"{tx.get('id')}: una transacción {tipo} fuera de lugar"


def _siguiente_nonce(cambios, cuenta, tx):
    esperado = cambios.nonce(cuenta) + 1
    if tx.get("nonce") != esperado:
        return f"{tx.get('id')}: nonce {tx.get('nonce')}, y le tocaba el {esperado}"
    return None


def _transferencia(cambios, tx, reglas):
    errores = contrato.validar_transaccion_firmada(tx) if reglas.verificar_firmas else []
    if errores:
        return f"transferencia {tx.get('id')} inválida: {'; '.join(errores)}"
    if tx.get("datos", "") != "":
        return f"transferencia {tx.get('id')}: no lleva datos"
    origen, destino, monto = tx["origen"], tx["destino"], tx["monto"]
    if not contrato.cuenta_valida(origen) or not contrato.cuenta_valida(destino) or not isinstance(monto, int) \
            or isinstance(monto, bool) or monto < 1:
        return f"transferencia {tx.get('id')} mal formada"
    if cambios.cuenta(origen) is None:
        return f"{tx['id']}: el origen no tiene un alta en la cadena (cuenta no aprobada)"
    problema = _siguiente_nonce(cambios, origen, tx)
    if problema:
        return problema
    if cambios.saldo(origen) < monto:
        return f"{tx['id']}: el origen no tiene saldo (tiene {cambios.saldo(origen)}, gasta {monto})"
    cambios.sumar(origen, -monto)
    cambios.sumar(destino, monto)
    cambios.nonces[origen] = tx["nonce"]
    return None


def _carga(cambios, tx, reglas):
    if not cambios.organizador:
        return f"carga {tx.get('id')}: no hay organizador en el génesis para verificar la firma"
    errores = contrato.validar_carga(tx, cambios.organizador) if reglas.verificar_firmas else []
    if errores:
        return f"carga {tx.get('id')} inválida: {'; '.join(errores)}"
    problema = _siguiente_nonce(cambios, contrato.EMISION, tx)
    if problema:
        return problema
    cambios.sumar(tx["destino"], tx["monto"])
    cambios.nonces[contrato.EMISION] = tx["nonce"]
    return None


def _acta(cambios, tx, reglas):
    if reglas.verificar_firmas:
        errores = contrato.validar_acta(tx, cambios.organizador)
        if errores:
            return f"{contrato.tipo_de(tx)} {tx.get('id')} inválida: {'; '.join(errores)}"
    firmante = contrato.EMISION if tx["origen"] == contrato.EMISION else tx["origen"]
    problema = _siguiente_nonce(cambios, firmante, tx) or \
        problema_de_acta(tx, cambios.cuenta, cambios.permiso, cambios.organizador)
    if problema:
        return f"{tx['id']}: {problema}" if not problema.startswith(tx["id"]) else problema
    tipo = contrato.tipo_de(tx)
    if tipo == "alta":
        cambios.cuentas[tx["destino"]] = _datos_cuenta(tx)
    elif tipo == "baja":
        cambios.cuentas[tx["destino"]] = None
    else:
        cambios.permisos[tx["destino"]] = tx.get("datos", "") or None
    cambios.nonces[firmante] = tx["nonce"]
    return None


def _genesis(estado, cambios, bloque, reglas, problemas):
    if bloque.get("previous_hash") != contrato.CEROS or bloque.get("nonce") != 0:
        problemas.append("el génesis tiene que tener previous_hash en ceros y nonce 0")
    if bloque.get("raiz_estado") != contrato.RAIZ_VACIA:
        problemas.append("la raíz de estado del génesis es la del árbol vacío")
    timestamp = bloque["timestamp"]
    for i, tx in enumerate(bloque.get("transactions") or []):
        tipo = contrato.tipo_de(tx)
        destino = tx.get("destino")
        if tipo == "organizador" and i == 0:
            if not _igual(tx, contrato._emision(f"organizador-{destino}", destino, 0, 0, timestamp)) \
                    or not contrato.cuenta_valida(destino):
                problemas.append("la transacción del organizador del génesis está mal formada")
                continue
            if reglas.organizador and destino != reglas.organizador:
                problemas.append(f"el organizador del génesis es {destino} y no el conocido ({reglas.organizador})")
            cambios.organizador = destino
            cambios.cuentas[destino] = {"tipo": "organizador", "nombre": "organizador", "alta": timestamp}
        elif tipo == "genesis":
            tipo_cuenta, nombre = contrato.datos_alta(tx)
            esperada = contrato._emision(f"genesis-{destino}", destino, tx.get("monto"), 0, timestamp,
                                         datos=f"usuario:{nombre}")
            if not _igual(tx, esperada) or not contrato.cuenta_valida(destino) or tipo_cuenta != "usuario" \
                    or not isinstance(tx.get("monto"), int) or tx["monto"] < 0 \
                    or (nombre and not contrato.nombre_valido(nombre)):
                problemas.append(f"transacción del génesis mal formada: {tx.get('id')}")
                continue
            cambios.sumar(destino, tx["monto"])
            cambios.cuentas[destino] = {"tipo": "usuario", "nombre": nombre, "alta": timestamp}
        else:
            problemas.append(f"una transacción {tipo} en el génesis: {tx.get('id')}")
    if not cambios.organizador and reglas.organizador:
        cambios.organizador = reglas.organizador


def _pago_de_epoca(estado, cambios, bloque, txs, i, reglas, problemas, avisos, altura):
    """Valida `shares-<e>` y los `part-<e>-*` desde la posición `i`; devuelve dónde sigue la caja."""
    epoca = contrato.epoca(altura) - 1
    timestamp = estado.timestamp  # el del último bloque de la época
    if i < len(txs) and contrato.tipo_de(txs[i]) == "shares":
        tx = txs[i]
        fondo_texto, _, raiz = str(tx.get("datos", "")).partition(":")
        fondo = int(fondo_texto) if fondo_texto.isdigit() else -1
        if not _igual(tx, contrato.shares_epoca(epoca, fondo, raiz, timestamp)) or len(raiz) != 32:
            problemas.append(f"{tx.get('id')}: no es shares-{epoca} con el timestamp del último bloque de la época")
            return i + 1
        i += 1
        pagos = {}
        while i < len(txs) and contrato.tipo_de(txs[i]) == "participacion":
            pago, minero = txs[i], txs[i].get("destino")
            if not _igual(pago, contrato.participacion(epoca, minero, pago.get("monto"), timestamp)) \
                    or not contrato.cuenta_valida(minero) or not isinstance(pago.get("monto"), int) \
                    or pago["monto"] < 1 or minero in pagos:
                problemas.append(f"{pago.get('id')}: no es un pago de la época {epoca}")
            else:
                pagos[minero] = pago["monto"]
            i += 1
        if list(pagos) != sorted(pagos):
            problemas.append(f"los pagos de la época {epoca} no están en orden de minero")
        if fondo != reglas.fondo_epoca:
            problemas.append(f"la época {epoca} reparte {fondo} y el fondo es {reglas.fondo_epoca}")
        detalle = reglas.detalle(epoca, raiz)
        if detalle is False:
            problemas.append(f"la lista publicada de la época {epoca} no da la raíz de la cadena ({raiz})")
        elif detalle is None:
            avisos.append(f"época {epoca}: el pago no se verificó (no se consiguió la lista de shares)")
        else:
            problemas.extend(_verificar_reparto(estado, epoca, detalle, pagos, fondo, reglas, avisos))
        for minero, monto in pagos.items():
            cambios.sumar(minero, monto)
        estado_shares = dict(estado.shares)
        estado_shares[epoca] = raiz
        cambios.shares = estado_shares
    elif i < len(txs) and contrato.tipo_de(txs[i]) == "participacion":
        problemas.append(f"{txs[i].get('id')}: un pago sin la lista de shares de la época (shares-{epoca})")
        while i < len(txs) and contrato.tipo_de(txs[i]) == "participacion":
            i += 1
    return i


def _verificar_reparto(estado, epoca, detalle, pagos, fondo, reglas, avisos):
    problemas = []
    shares = {m: d["shares"] for m, d in detalle.items() if d.get("shares", 0) > 0}
    rachas = {m: d["racha"] for m, d in detalle.items() if d.get("shares", 0) > 0}
    if contrato.repartir(fondo, shares, rachas) != pagos:
        problemas.append(f"los pagos de la época {epoca} no son el reparto de su lista de shares")
    for minero, racha in rachas.items():
        esperada = 1
        while esperada < contrato.RACHA_MAXIMA and epoca - esperada >= 0:
            anterior = epoca - esperada
            raiz = estado.shares.get(anterior)
            if raiz is None:
                break  # esa época no tuvo shares en la cadena
            previa = reglas.detalle(anterior, raiz)
            if not previa:
                avisos.append(f"época {epoca}: la racha de {minero[:12]} no se verificó (falta la lista de la {anterior})")
                esperada = racha
                break
            if previa.get(minero, {}).get("shares", 0) <= 0:
                break
            esperada += 1
        if racha != esperada:
            problemas.append(f"época {epoca}: {minero[:12]} tiene racha {racha} y le corresponde {esperada}")
    return problemas


def aplicar_bloque(estado, bloque, reglas=None, estricto=True, hash_esperado=None):
    """Verifica `bloque` contra `estado` (el de la punta) y lo aplica. Devuelve (problemas, avisos).

    Estricto: si hay un problema, el estado queda como estaba (así siguen los nodos y los validadores). Si no, se
    aplica lo que se pueda y se anotan los problemas (el NCT y la auditoría, que no pueden corregir el pasado).
    """
    reglas = reglas or Reglas()
    problemas, avisos = [], []
    altura = estado.altura + 1
    cambios = _Cambios(estado)
    try:
        txs = bloque["transactions"]
        if hash_esperado and bloque.get("block_hash") != hash_esperado:
            problemas.append(f"el bloque dice ser {bloque.get('block_hash')} y se pidió {hash_esperado}")
        if bloque.get("altura", altura) != altura:
            problemas.append(f"dice tener altura {bloque.get('altura')} y le toca la {altura}")
        if contrato.hash_bloque(contrato.texto_de_bloque(bloque), bloque["nonce"]) != bloque["block_hash"]:
            problemas.append("el hash no sale del texto (raíz de transacciones y de estado) y el nonce")
        if altura == 0:
            _genesis(estado, cambios, bloque, reglas, problemas)
        else:
            _minado(estado, cambios, bloque, txs, altura, reglas, problemas, avisos)
    except (KeyError, TypeError, ValueError) as error:
        problemas.append(f"el bloque está mal formado: {error!r}")
    if problemas and estricto:
        return problemas, avisos
    cambios.aplicar()
    if cambios.shares is not None:
        estado.shares = cambios.shares
    estado.altura, estado.punta, estado.timestamp = altura, bloque.get("block_hash"), bloque.get("timestamp", 0)
    return problemas, avisos


def _minado(estado, cambios, bloque, txs, altura, reglas, problemas, avisos):
    prefijo = bloque.get("prefijo") or ""
    if bloque.get("previous_hash") != estado.punta:
        problemas.append("no encadena: su previous_hash no es la punta")
    if bloque.get("raiz_estado") != estado.raiz():
        problemas.append(f"la raíz de estado no es la del bloque anterior (dice {bloque.get('raiz_estado')}, "
                         f"y es {estado.raiz()})")
    if not prefijo or not contrato.cumple(bloque["block_hash"], prefijo):
        problemas.append(f"el hash no cumple el prefijo {prefijo!r}")
    if not txs or contrato.tipo_de(txs[0]) != "coinbase":
        problemas.append("la primera transacción no es la coinbase")
        i = 0
    else:
        minero = txs[0].get("destino")
        esperada = contrato.coinbase(altura, minero, contrato.recompensa(prefijo), bloque["timestamp"])
        if not _igual(txs[0], esperada) or not contrato.cuenta_valida(minero):
            problemas.append(f"la coinbase no es la del ganador con {contrato.recompensa(prefijo)}")
        else:
            cambios.sumar(minero, txs[0]["monto"])
        i = 1
    if contrato.es_primer_bloque_de_epoca(altura):
        i = _pago_de_epoca(estado, cambios, bloque, txs, i, reglas, problemas, avisos, altura)
    for tx in txs[i:]:
        problema = aplicar_transaccion(cambios, tx, reglas)
        if problema:
            problemas.append(problema)


def filtrar(estado, transacciones, reglas=None, altura=None):
    """Lo que hace el NCT antes de armar un bloque: las transacciones de la caja que valen contra el estado de la
    cadena, en orden, y las que no [(tx, motivo)]. Así nunca arma un bloque que los nodos rechazarían."""
    reglas = reglas or Reglas(verificar_firmas=False)
    cambios = _Cambios(estado)
    validas, rechazadas = [], []
    for tx in transacciones:
        problema = aplicar_transaccion(cambios, tx, reglas)
        if problema:
            rechazadas.append((tx, problema))
        else:
            validas.append(tx)
    return validas, rechazadas


def reconstruir(bloques, reglas=None, estricto=True, estado=None):
    """El estado de una cadena (los bloques en orden, desde el génesis o desde el siguiente a `estado`).

    Devuelve (estado, [(altura, problemas)]). En modo estricto se para en el primer bloque que no sirve.
    """
    estado = estado or Estado()
    malos = []
    for bloque in bloques:
        problemas, _ = aplicar_bloque(estado, bloque, reglas, estricto)
        if problemas:
            malos.append((estado.altura + (0 if not estricto else 1), problemas))
            if estricto:
                break
    return estado, malos


# ── Lo que hay en Redis: los bloques y el caché ──

LOTE = 200


def bloque_guardado(campos):
    """bloque:<hash> como lo devuelve Redis (todo texto) → el bloque con sus tipos."""
    bloque = dict(campos)
    bloque["transactions"] = json.loads(campos["transactions"])
    for campo in ("nonce", "timestamp", "altura"):
        if campo in bloque:
            bloque[campo] = int(bloque[campo])
    bloque.pop("duracion", None)
    return bloque


def bloques_de_redis(redis, desde=0, hasta=None):
    """Los bloques de `cadena` desde la altura `desde`, en orden, de a LOTE. (hash, bloque o None si no está)."""
    hasta = redis.llen(contrato.CADENA) - 1 if hasta is None else hasta
    for inicio in range(desde, hasta + 1, LOTE):
        hashes = redis.lrange(contrato.CADENA, inicio, min(inicio + LOTE - 1, hasta))
        tuberia = redis.pipeline(transaction=False)
        for hash_ in hashes:
            tuberia.hgetall(contrato.BLOQUE.format(hash_))
        for hash_, campos in zip(hashes, tuberia.execute()):
            yield hash_, (bloque_guardado(campos) if campos else None)


def shares_de_redis(redis):
    """La función de `Reglas.shares` con lo publicado en epoca:<n> (lo que sirve /api/epocas/<n>)."""
    def leer(epoca):
        guardado = redis.hget(contrato.EPOCA.format(epoca), "detalle")
        return json.loads(guardado) if guardado else None
    return leer


def cache_de_redis(redis):
    """(altura, punta, saldos, nonces confirmados, cuentas, permisos) de Redis, todo de la misma punta (MULTI)."""
    tuberia = redis.pipeline()
    tuberia.llen(contrato.CADENA)
    tuberia.lindex(contrato.CADENA, -1)
    tuberia.hgetall(contrato.SALDOS)
    tuberia.hgetall(contrato.NONCES_CONFIRMADOS)
    tuberia.hgetall(contrato.CUENTAS)
    tuberia.hgetall(contrato.PERMISOS)
    largo, punta, saldos, nonces, cuentas, permisos = tuberia.execute()
    return {"altura": largo - 1, "punta": punta, "saldos": {c: int(s) for c, s in saldos.items()},
            "nonces": {c: int(n) for c, n in nonces.items()},
            "cuentas": {c: json.loads(d) for c, d in cuentas.items()}, "permisos": permisos}


def hojas_de_cache(cache):
    tipos = {c: d.get("tipo", "") for c, d in cache["cuentas"].items()}
    return hojas_estado(cache["saldos"], cache["nonces"], tipos, cache["permisos"])


def diferencias(estado, cache):
    """{campo: [(clave, cadena, caché)]} de lo que no coincide. Un saldo en 0 y uno que no está son lo mismo."""
    resultado = {}
    for campo, cadena, guardado in (("saldos", estado.saldos, cache["saldos"]),
                                    ("nonces", estado.nonces, cache["nonces"]),
                                    ("cuentas", estado.cuentas, cache["cuentas"]),
                                    ("permisos", estado.permisos, cache["permisos"])):
        vacio = 0 if campo in ("saldos", "nonces") else None
        distintas = [(clave, cadena.get(clave, vacio), guardado.get(clave, vacio))
                     for clave in sorted(set(cadena) | set(guardado))
                     if (cadena.get(clave, vacio) or vacio) != (guardado.get(clave, vacio) or vacio)]
        if distintas:
            resultado[campo] = distintas
    return resultado


def reparar(redis, estado, diferentes):
    """Pisa el caché con lo que dice la cadena, sólo si la punta sigue siendo la del estado (WATCH). Devuelve si
    escribió. `nonces` (que también lleva las pendientes) nunca queda por debajo de lo confirmado."""
    import redis as redislib
    with redis.pipeline() as tuberia:
        try:
            tuberia.watch(contrato.CADENA)
            if tuberia.llen(contrato.CADENA) - 1 != estado.altura:
                return False
            aceptados = tuberia.hgetall(contrato.NONCES)
            tuberia.multi()
            for campo, clave_redis in (("saldos", contrato.SALDOS), ("nonces", contrato.NONCES_CONFIRMADOS),
                                       ("cuentas", contrato.CUENTAS), ("permisos", contrato.PERMISOS)):
                for clave, cadena, _ in diferentes.get(campo, []):
                    if cadena in (None, 0) and campo != "saldos":
                        tuberia.hdel(clave_redis, clave)
                    elif campo == "cuentas":
                        tuberia.hset(clave_redis, clave, json.dumps(cadena))
                    else:
                        tuberia.hset(clave_redis, clave, cadena)
            for cuenta, nonce in estado.nonces.items():
                if int(aceptados.get(cuenta, 0)) < nonce:
                    tuberia.hset(contrato.NONCES, cuenta, nonce)
            tuberia.execute()
            return True
        except redislib.WatchError:
            return False


class Seguidor:
    """El estado de la cadena de Redis, en memoria y al día: lo que usa el NCT para armar cada bloque.

    No es estricto: un bloque que no cumple las reglas (alguien lo tocó en Redis) igual se aplica y queda en
    `problemas`, porque el NCT no puede corregir el pasado. Los nodos verificadores, en cambio, lo rechazan.
    Cada `snapshot_cada` bloques deja el estado en `estado:snapshot`, y al arrancar sigue desde ahí si el snapshot
    da la raíz que quedó en la cadena.
    """

    def __init__(self, redis, reglas, snapshot_cada=100, al_problema=None):
        import threading
        self.redis, self.reglas, self.snapshot_cada = redis, reglas, snapshot_cada
        self.estado = Estado()
        self.al_problema = al_problema or (lambda altura, problemas: None)
        self.lock = threading.RLock()

    def arrancar(self):
        """Desde el snapshot (si sirve) o desde el génesis. Devuelve desde qué altura se recorrió."""
        with self.lock:
            self.estado = self._snapshot() or Estado()
            desde = self.estado.altura + 1
            self.ponerse_al_dia()
            return desde

    def _snapshot(self):
        guardado = self.redis.get(contrato.ESTADO_SNAPSHOT)
        if not guardado:
            return None
        try:
            estado = Estado.desde_dict(json.loads(guardado))
        except (ValueError, KeyError, TypeError):
            return None
        if self.redis.lindex(contrato.CADENA, estado.altura) != estado.punta:
            return None
        siguiente = self.redis.lindex(contrato.CADENA, estado.altura + 1)
        if siguiente and self.redis.hget(contrato.BLOQUE.format(siguiente), "raiz_estado") != estado.raiz():
            return None  # no es el estado que quedó comprometido en la cadena
        return estado

    def ponerse_al_dia(self):
        """Aplica los bloques nuevos de `cadena`. Si la cadena cambió por debajo (se reinició), empieza de nuevo."""
        with self.lock:
            if self.estado.altura >= 0 and self.redis.lindex(contrato.CADENA, self.estado.altura) != self.estado.punta:
                self.estado = Estado()
            for hash_, bloque in bloques_de_redis(self.redis, desde=self.estado.altura + 1):
                if bloque is None:
                    self.al_problema(self.estado.altura + 1, [f"no está bloque:{hash_}"])
                    break
                problemas, _ = aplicar_bloque(self.estado, bloque, self.reglas, estricto=False, hash_esperado=hash_)
                if problemas:
                    self.al_problema(self.estado.altura, problemas)
                if self.snapshot_cada and self.estado.altura % self.snapshot_cada == 0 and self.estado.altura > 0:
                    self.redis.set(contrato.ESTADO_SNAPSHOT, json.dumps(self.estado.a_dict()))
            return self.estado
