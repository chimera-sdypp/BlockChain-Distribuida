"""Prueba de los ataques del operador (BC-85): lo que puede hacer el dueño de la nube, y que no pase sin que se note.

Sigue a la de los mineros que mienten (BC-76, trampas.py), pero del otro lado: acá el tramposo es quien opera la
nube. Cada escenario toca Redis (desde un pod del TrP, que tiene el contrato y la clave de Redis) como lo haría el
operador, mira qué hacen el NCT, los nodos verificadores del cluster (/nodos/0 y /nodos/1), un nodo nuevo que
arranca de cero fuera del cluster y tres validadores locales, y deja todo como estaba.

    pilar2/.venv/bin/python pilar3/pruebas/operador.py --backend http://localhost --gateway http://localhost:8081
        [--solo saldo,alta,editado,reescrita,epoca,invalido,deshacer,sin-validadores,reminada,pago-epoca,otra-cadena]

v4 (BC-96): `reminada` (la historia rehecha con prefijo 1), `pago-epoca` (pagar más que el fondo sin la lista
publicada) y `otra-cadena` (lo firmado para otra cadena, como lo de antes de un reinicio).

Necesita la cadena v4 con tráfico (manda transferencias de alice, bob y carol), un minero CPU local (lo arranca) y
la clave de organizador de prueba (la del k3s). Escribe resultados/operador.csv y resultados/operador.log.
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
PILAR2 = RAIZ / "pilar2"
RESULTADOS = Path(__file__).resolve().parent / "resultados"
sys.path.insert(0, str(PILAR2))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from comun import contrato  # noqa: E402
from comun.tests.claves import CUENTA, PRIVADA  # noqa: E402
import trampas  # noqa: E402  (el minero local, el tráfico, kubectl y el registro de comprobaciones)

NS = "blockchain"
anotar, comprobar = trampas.anotar, trampas.comprobar


def kubectl(*args, entrada=None, check=True):
    return subprocess.run(["kubectl", "-n", NS, *args], input=entrada, capture_output=True, text=True,
                          check=check).stdout


def redis(script):
    """Corre `script` (con `r` conectado al Redis del sistema) en un pod del TrP y devuelve su última línea en JSON.
    El TrP, y no el NCT, porque hay escenarios con el NCT apagado."""
    codigo = "import json\nfrom comun import config, contrato, redisdb\nr = redisdb.conectar(config.cargar('operador'))\n"
    salida = kubectl("exec", "-i", "deploy/trp", "--", "python", "-", entrada=codigo + script)
    return json.loads(salida.strip().splitlines()[-1])


def get(url):
    with urllib.request.urlopen(url, timeout=20) as respuesta:
        return json.loads(respuesta.read())


def esperar_que(condicion, segundos, pausa=3, descripcion=""):
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        try:
            resultado = condicion()
        except Exception:  # noqa: BLE001 — un pod que se está reiniciando
            resultado = None
        if resultado:
            return resultado
        time.sleep(pausa)
    anotar(f"No pasó en {segundos} s: {descripcion}")
    return None


def nct(replicas):
    kubectl("scale", "deploy/nct", f"--replicas={replicas}")
    if replicas:
        kubectl("rollout", "status", "deploy/nct", "--timeout=180s")
    else:
        esperar_que(lambda: not kubectl("get", "pods", "-l", "app=nct", "-o", "name"), 120, descripcion="NCT apagado")


def nodos_del_cluster(base):
    return [get(f"{base}/nodos/{n}/api/estado") for n in (0, 1)]


def reiniciar_nodos_del_cluster():
    """Un nodo que vio la historia reescrita se queda parado a propósito: lo revisa una persona y lo reinicia."""
    kubectl("delete", "pod", "nodo-0", "nodo-1", "--wait=true")
    kubectl("rollout", "status", "sts/nodo", "--timeout=180s")


class NodoLocal:
    """Un nodo (o validador) corriendo en esta máquina, fuera del cluster, con su propio directorio."""

    def __init__(self, nombre, base, puerto, validador=False, datos=None):
        self.nombre, self.base, self.puerto, self.validador = nombre, base, puerto, validador
        self.datos = Path(datos or tempfile.mkdtemp(prefix=f"nodo-{nombre}-"))
        self.url = f"http://127.0.0.1:{puerto}"
        self.proceso = None

    def arrancar(self):
        RESULTADOS.joinpath("operador-logs").mkdir(parents=True, exist_ok=True)
        self.salida = open(RESULTADOS / "operador-logs" / f"{self.nombre}.txt", "a")
        argumentos = [sys.executable, "-m", "nodo", "--fuente", self.base, "--datos", str(self.datos),
                      "--puerto", str(self.puerto), "--intervalo", "2"]
        if self.validador:
            argumentos.append("--validador")
        self.proceso = subprocess.Popen(argumentos, cwd=PILAR2, stdout=self.salida, stderr=subprocess.STDOUT,
                                        env={**os.environ, "LOGS_DIR": str(self.datos / "logs")})
        esperar_que(lambda: get(f"{self.url}/api/estado"), 60, 1, f"{self.nombre} no arrancó")
        return self

    def estado(self):
        return get(f"{self.url}/api/estado")

    def cuenta(self):
        return contrato.clave_publica((self.datos / "nodo.clave").read_text().strip())

    def privada(self):
        return (self.datos / "nodo.clave").read_text().strip()

    def al_dia(self, segundos=180):
        """Espera a que deje de avanzar (llegó a la punta, o se paró por un rechazo o un conflicto)."""
        anterior = None
        for _ in range(segundos // 3):
            actual = self.estado()
            if actual["conflicto"] or actual["rechazado"] or (actual["atraso"] == 0 and actual == anterior):
                return actual
            anterior = actual
            time.sleep(3)
        return self.estado()

    def parar(self):
        if self.proceso and self.proceso.poll() is None:
            self.proceso.terminate()
            try:
                self.proceso.wait(15)
            except subprocess.TimeoutExpired:
                self.proceso.kill()

    def borrar(self):
        self.parar()
        shutil.rmtree(self.datos, ignore_errors=True)


def verificador_nuevo(base, puerto=8099):
    """Un nodo que arranca de cero: lo que vería cualquiera que se suma hoy."""
    nodo = NodoLocal(f"nuevo-{puerto}", base, puerto).arrancar()
    estado = nodo.al_dia()
    nodo.borrar()
    return estado


def punta():
    return redis("h = r.lindex('cadena', -1)\nb = r.hgetall('bloque:' + h)\n"
                 "print(json.dumps({**b, 'transactions': json.loads(b['transactions']), 'altura': int(b['altura']),"
                 " 'timestamp': int(b['timestamp']), 'nonce': int(b['nonce'])}))")


PISO = {}


def piso(base):
    """El piso de prefijo de la cadena (v4): un bloque con menos ya no lo acepta nadie."""
    if base not in PISO:
        PISO[base] = get(f"{base}/api/cadena")["prefijo_minimo"]
    return PISO[base]


def minar(texto, prefijo):
    return next(n for n in range(16 ** (len(prefijo) + 2)) if contrato.hash_bloque(texto, n).startswith(prefijo))


def bloque_minado(previous_hash, timestamp, altura, transacciones, raiz_estado, minero, prefijo="0"):
    """Un bloque con PoW válida, con el prefijo que elija el operador (el piso de la cadena, para que valga)."""
    tarea = {"previous_hash": previous_hash, "timestamp": timestamp, "altura": altura, "prefijo": prefijo,
             "transacciones": transacciones, "raiz_estado": raiz_estado}
    texto = contrato.texto_para(tarea, minero)
    nonce = minar(texto, prefijo)
    return {"previous_hash": previous_hash, "timestamp": timestamp, "altura": altura, "nonce": nonce,
            "transactions": contrato.transacciones_del_bloque(tarea, minero), "prefijo": prefijo,
            "block_hash": contrato.hash_bloque(texto, nonce), "raiz_estado": raiz_estado}


def reminar(bloque, prefijo):
    """El mismo bloque (las mismas transacciones, coinbase incluida) con otro prefijo y su nonce."""
    texto = contrato.texto_de_bloque(bloque)
    nonce = minar(texto, prefijo)
    return {**bloque, "prefijo": prefijo, "nonce": nonce, "block_hash": contrato.hash_bloque(texto, nonce)}


def bloque_en(altura):
    return redis(f"h = r.lindex('cadena', {altura})\nb = r.hgetall('bloque:' + h)\nb.pop('duracion', None)\n"
                 "print(json.dumps({**b, 'transactions': json.loads(b['transactions']), 'altura': int(b['altura']),"
                 " 'timestamp': int(b['timestamp']), 'nonce': int(b['nonce'])}))")


def guardar_bloque(bloque, en=None):
    """RPUSH (o LSET en la altura `en`) en `cadena` y bloque:<hash>, como lo haría el operador."""
    campos = {**bloque, "transactions": json.dumps(bloque["transactions"])}
    poner = f"r.lset('cadena', {en}, h)" if en is not None else "r.rpush('cadena', h)"
    redis(f"campos = {json.dumps(campos)}\nh = campos['block_hash']\nr.hset('bloque:' + h, mapping=campos)\n{poner}\n"
          "print(json.dumps(True))")


# ── Escenarios ──

def genesis(a):
    """El hash del génesis (v4): va en todo lo que se firma."""
    return get(f"{a.backend}/api/cadena")["genesis"]


def saldo(a):
    """HINCRBY saldos: el NCT valida con el número tocado, pero arma el bloque contra la cadena y corrige el caché."""
    antes = get(f"{a.backend}/api/saldos/{CUENTA['carol']}")["saldo"]
    redis(f"r.hincrby('saldos', '{CUENTA['carol']}', 1000000)\nprint(json.dumps(True))")
    nonce = get(f"{a.backend}/api/cuentas/{CUENTA['carol']}")["nonce"] + 1
    gasto = contrato.transaccion(PRIVADA["carol"], CUENTA["bob"], antes + 500_000, nonce, cadena=genesis(a))
    codigo, _ = trampas.Backend(a.backend).mandar(gasto)
    final = esperar_que(lambda: (t := trampas.Backend(a.backend).estado(gasto["id"])) and t["estado"] in
                        ("confirmada", "rechazada") and t, 300, descripcion="el gasto con el saldo inventado")
    comprobar("saldo", "gastar el saldo inventado", "rechazada (no vale contra la cadena)",
              f"{codigo} → {final and final['estado']} ({final and final.get('motivo')})",
              bool(final) and final["estado"] == "rechazada")
    corregido = esperar_que(lambda: get(f"{a.backend}/api/saldos/{CUENTA['carol']}")["saldo"] < antes + 1000000, 120,
                            descripcion="la revisión del caché")
    divergencias = kubectl("logs", "-l", "app=nct", "--tail=-1", "--since=10m").count("Caché distinto de la cadena en saldos")
    comprobar("saldo", "el NCT corrige el caché desde la cadena", "el saldo vuelve y queda en el log y la métrica",
              f"saldo {get(a.backend + '/api/saldos/' + CUENTA['carol'])['saldo']} (antes {antes}), "
              f"{divergencias} línea(s) «Caché distinto»", bool(corregido) and divergencias >= 1)
    estados = nodos_del_cluster(a.backend)
    comprobar("saldo", "los nodos siguen sin rechazar nada", "ningún bloque malo llegó a la cadena",
              f"rechazado: {[e['rechazado'] for e in estados]}", not any(e["rechazado"] for e in estados))


def alta(a):
    """HSET cuentas: una cuenta «aprobada» sin un alta firmada no llega a operar."""
    privada, zoe = contrato.nueva_clave()
    backend = trampas.Backend(a.backend)
    backend.mandar(contrato.carga(PRIVADA["organizador"], backend.nonce(contrato.EMISION) + 1, zoe, 5, int(time.time()),
                                  cadena=genesis(a)))
    redis(f"r.hset('cuentas', '{zoe}', json.dumps({{'tipo': 'usuario', 'nombre': 'trucha', 'alta': 1}}))\n"
          "print(json.dumps(True))")
    esperar_que(lambda: get(f"{a.backend}/api/saldos/{zoe}")["saldo"] >= 5 or None, 300, descripcion="los 5 de zoe")
    redis(f"r.hset('cuentas', '{zoe}', json.dumps({{'tipo': 'usuario', 'nombre': 'trucha', 'alta': 1}}))\n"
          "print(json.dumps(True))")  # otra vez: la revisión pudo haberla borrado
    tx = contrato.transaccion(privada, CUENTA["bob"], 1, 1, cadena=genesis(a))
    codigo, _ = backend.mandar(tx)
    final = esperar_que(lambda: (t := backend.estado(tx["id"])) and t["estado"] in ("confirmada", "rechazada") and t,
                        300, descripcion="la transferencia de la cuenta trucha")
    comprobar("alta", "transferir con un alta escrita a mano", "rechazada",
              f"{codigo} → {final and final['estado']} ({final and final.get('motivo')})",
              bool(final) and final["estado"] == "rechazada")
    borrada = esperar_que(lambda: not get(f"{a.backend}/api/cuentas/{zoe}")["aprobada"], 120,
                          descripcion="la revisión del caché")
    comprobar("alta", "la revisión saca la cuenta de `cuentas`", "aprobada: false", f"aprobada: {not borrada}",
              bool(borrada))


def editado(a):
    """HSET bloque:<hash>: los nodos que ya lo verificaron no lo vuelven a leer, pero uno nuevo no lo acepta."""
    hash_ = redis("print(json.dumps(r.lindex('cadena', 2)))")
    original = redis(f"print(json.dumps(r.hget('bloque:{hash_}', 'transactions')))")
    txs = json.loads(original)
    txs[0]["monto"] += 1000  # la coinbase del bloque 2, inflada
    redis(f"r.hset('bloque:{hash_}', 'transactions', {json.dumps(json.dumps(txs))})\nprint(json.dumps(True))")
    try:
        nuevo = verificador_nuevo(a.backend)
        comprobar("editado", "un nodo nuevo baja la cadena con el bloque 2 editado", "rechaza el bloque 2",
                  f"altura {nuevo['altura']}, rechazado {nuevo['rechazado'] and nuevo['rechazado']['altura']}: "
                  f"{nuevo['rechazado'] and nuevo['rechazado']['motivos'][0]}",
                  bool(nuevo["rechazado"]) and nuevo["rechazado"]["altura"] == 2)
    finally:
        redis(f"r.hset('bloque:{hash_}', 'transactions', {json.dumps(original)})\nprint(json.dumps(True))")


def reescrita(a, validadores):
    """Sacar una transferencia y reminar: con el NCT apagado, la punta se reemplaza por otro bloque válido."""
    nct(0)
    reemplazo = None
    try:
        tip = punta()
        reemplazo = bloque_minado(tip["previous_hash"], tip["timestamp"] + 1, tip["altura"], [], tip["raiz_estado"],
                                  CUENTA["operador"], "0" * piso(a.backend))
        guardar_bloque(reemplazo, en=tip["altura"])
        anotar(f"Bloque {tip['altura']} reemplazado: {tip['block_hash']} → {reemplazo['block_hash']} (sin sus transacciones)")
        vistos = esperar_que(lambda: all(e["conflicto"] for e in nodos_del_cluster(a.backend)) and
                             nodos_del_cluster(a.backend), 60, descripcion="el conflicto en los nodos del cluster")
        comprobar("reescrita", "los nodos del cluster ven la historia reescrita", "conflicto en la altura de la punta",
                  f"{[e['conflicto'] and e['conflicto']['altura'] for e in vistos or nodos_del_cluster(a.backend)]}",
                  bool(vistos))
        if validadores:
            conflictos = esperar_que(lambda: all(v.estado()["conflicto"] for v in validadores), 60,
                                     descripcion="el conflicto en los validadores")
            comprobar("reescrita", "los validadores no firman la cadena nueva", "conflicto y ninguna firma nueva",
                      f"conflicto en {sum(bool(v.estado()['conflicto']) for v in validadores)} de {len(validadores)}",
                      bool(conflictos))
    finally:
        if reemplazo:
            guardar_bloque(tip, en=tip["altura"])
            redis(f"r.delete('bloque:{reemplazo['block_hash']}')\nprint(json.dumps(True))")
        nct(2)
        reiniciar_nodos_del_cluster()
        for v in validadores:
            v.parar()
            v.arrancar()


def epoca(a):
    """Inflar los shares de un minero en la lista publicada: no da la raíz que quedó en la cadena."""
    epocas = redis("print(json.dumps(sorted(int(k.split(':')[1]) for k in r.scan_iter('epoca:*') "
                   "if r.hget(k, 'detalle') not in (None, '{}'))))")
    if not epocas:
        comprobar("epoca", "inflar una lista de shares publicada", "—", "no hay épocas pagadas con shares", False)
        return
    e = epocas[0]
    original = redis(f"print(json.dumps(r.hget('epoca:{e}', 'detalle')))")
    detalle = json.loads(original)
    minero = sorted(detalle)[0]
    detalle[minero]["shares"] += 50
    redis(f"r.hset('epoca:{e}', 'detalle', {json.dumps(json.dumps(detalle))})\nprint(json.dumps(True))")
    try:
        nuevo = verificador_nuevo(a.backend)
        motivo = nuevo["rechazado"] and nuevo["rechazado"]["motivos"][0]
        comprobar("epoca", f"la lista de la época {e} con 50 shares más para un minero", "un nodo nuevo la rechaza",
                  f"rechazado en {nuevo['rechazado'] and nuevo['rechazado']['altura']}: {motivo}",
                  bool(motivo) and "no da la raíz" in motivo)
    finally:
        redis(f"r.hset('epoca:{e}', 'detalle', {json.dumps(original)})\nprint(json.dumps(True))")


def invalido(a, validadores):
    """Un bloque con una carga firmada por otra clave, minado y agregado a la punta con el NCT apagado."""
    nct(0)
    malo = None
    try:
        tip = punta()
        raiz = get(f"{a.backend}/nodos/0/api/estado")["raiz"]  # el estado después de la punta, según un nodo
        emision = get(f"{a.backend}/api/cuentas/emision")["nonce"]
        trucha = contrato.carga(PRIVADA["alice"], emision + 1, CUENTA["operador"], 10**9, tip["timestamp"],
                                cadena=genesis(a))
        malo = bloque_minado(tip["block_hash"], tip["timestamp"] + 1, tip["altura"] + 1, [trucha], raiz,
                             CUENTA["operador"], "0" * piso(a.backend))
        guardar_bloque(malo)
        anotar(f"Bloque {malo['altura']} inválido agregado: {malo['block_hash']}")
        rechazos = esperar_que(lambda: all(e["rechazado"] for e in nodos_del_cluster(a.backend)) and
                               nodos_del_cluster(a.backend), 60, descripcion="el rechazo en los nodos")
        comprobar("invalido", "los nodos rechazan el bloque", "rechazado con el motivo",
                  f"{[(e['rechazado'] or {}).get('motivos', ['—'])[0] for e in rechazos or []]}", bool(rechazos))
        if validadores:
            time.sleep(10)
            final = get(f"{a.backend}/api/finalidad")["altura"]
            comprobar("invalido", "el bloque inválido no se finaliza", f"finalizada < {malo['altura']}",
                      f"finalizada {final}", final < malo["altura"])
    finally:
        if malo:
            redis(f"r.rpop('cadena')\nr.delete('bloque:{malo['block_hash']}')\nprint(json.dumps(True))")
        nct(2)


def deshacer(a, validadores):
    """Reemplazar un bloque finalizado: los validadores honestos no lo firman, y uno que lo firma deja la prueba."""
    if len(validadores) < 3:
        comprobar("deshacer", "deshacer lo finalizado", "—", "hacen falta 3 validadores", False)
        return
    final = esperar_que(lambda: (f := get(f"{a.backend}/api/finalidad"))["altura"] >= 1 and f, 300,
                        descripcion="un bloque finalizado")
    if not final:
        comprobar("deshacer", "deshacer lo finalizado", "—", "no se finalizó ningún bloque", False)
        return
    nct(0)
    reemplazo = None
    try:
        altura = final["altura"]
        original = redis(f"h = r.lindex('cadena', {altura})\nb = r.hgetall('bloque:' + h)\n"
                         "print(json.dumps({**b, 'transactions': json.loads(b['transactions']), "
                         "'altura': int(b['altura']), 'timestamp': int(b['timestamp']), 'nonce': int(b['nonce'])}))")
        reemplazo = bloque_minado(original["previous_hash"], original["timestamp"] + 1, altura, [],
                                  original["raiz_estado"], CUENTA["operador"])
        guardar_bloque(reemplazo, en=altura)
        firma = contrato.firma_bloque(validadores[0].privada(), altura, reemplazo["block_hash"])
        codigo, respuesta = trampas.Backend(a.backend).pedir("POST", "/api/firmas", firma)
        dobles = get(f"{a.backend}/api/finalidad")["dobles"]
        comprobar("deshacer", f"un validador que firma otro bloque en la altura finalizada {altura}",
                  "409 y queda la prueba de la doble firma", f"{codigo}, {len(dobles)} prueba(s)",
                  codigo == 409 and any(d["altura"] == altura for d in dobles))
        time.sleep(10)
        honestos = [v.estado() for v in validadores[1:]]
        comprobar("deshacer", "los otros dos validadores no firman la historia nueva", "conflicto",
                  f"{[bool(e['conflicto']) for e in honestos]}", all(e["conflicto"] for e in honestos))
        despues = get(f"{a.backend}/api/finalidad")
        comprobar("deshacer", "la finalidad no retrocede", f"sigue en {altura} o más", f"{despues['altura']}",
                  despues["altura"] >= altura)
    finally:
        if reemplazo:
            guardar_bloque(original, en=altura)
            redis(f"r.delete('bloque:{reemplazo['block_hash']}')\nprint(json.dumps(True))")
        nct(2)
        reiniciar_nodos_del_cluster()
        for v in validadores:
            v.parar()
            v.arrancar()


def sin_validadores(a, validadores):
    """Los validadores son un extra: sin ellos los bloques siguen saliendo; al volver, finalizan lo que verifican."""
    for v in validadores:
        v.parar()
    inicio = get(f"{a.backend}/api/bloques?limite=1")[0]["altura"]
    final_antes = get(f"{a.backend}/api/finalidad")["altura"]
    subio = esperar_que(lambda: get(f"{a.backend}/api/bloques?limite=1")[0]["altura"] >= inicio + 2, 420,
                        descripcion="dos bloques sin validadores")
    comprobar("sin-validadores", "con los validadores apagados salen bloques", "2 bloques o más",
              f"de {inicio} a {get(f'{a.backend}/api/bloques?limite=1')[0]['altura']}; finalizada sigue en "
              f"{get(f'{a.backend}/api/finalidad')['altura']}", bool(subio)
              and get(f"{a.backend}/api/finalidad")["altura"] == final_antes)
    for v in validadores:
        v.arrancar()
    alcanzo = esperar_que(lambda: get(f"{a.backend}/api/finalidad")["altura"] >= inicio + 2, 180,
                          descripcion="la finalidad al volver")
    comprobar("sin-validadores", "al volver, se ponen al día y finalizan", f"finalizada ≥ {inicio + 2}",
              f"{get(f'{a.backend}/api/finalidad')['altura']}", bool(alcanzo))


# ── v4 (BC-96): lo que los verificadores todavía no verificaban ──

def reminada(a):
    """Rehacer la historia desde el génesis con prefijo 1 (gratis): un nodo que arranca de cero no la acepta."""
    nct(0)
    original = barato = None
    try:
        original = bloque_en(1)
        barato = reminar(original, "0")
        guardar_bloque(barato, en=1)
        anotar(f"Bloque 1 reminado con prefijo 1: {original['block_hash']} → {barato['block_hash']}")
        nuevo = verificador_nuevo(a.backend)
        motivo = nuevo["rechazado"] and nuevo["rechazado"]["motivos"][0]
        comprobar("reminada", "un nodo nuevo baja la historia reminada con prefijo 1",
                  f"rechaza el bloque 1 (piso {piso(a.backend)})",
                  f"rechazado en {nuevo['rechazado'] and nuevo['rechazado']['altura']}: {motivo}",
                  bool(motivo) and nuevo["rechazado"]["altura"] == 1 and "piso de la cadena" in motivo)
    finally:
        if original:
            guardar_bloque(original, en=1)
        if barato:
            redis(f"r.delete('bloque:{barato['block_hash']}')\nprint(json.dumps(True))")
        nct(2)


def pago_epoca(a):
    """Inflar un pago de época, reminar el bloque y borrar la lista publicada (sin ella antes sólo quedaba un aviso)."""
    alturas = redis("print(json.dumps([a for a in range(10, r.llen('cadena'), 10) if any("
                    "t['id'].startswith('part-') for t in json.loads(r.hget('bloque:' + r.lindex('cadena', a), "
                    "'transactions')))]))")
    if not alturas:
        comprobar("pago-epoca", "pagar más que el fondo", "—", "no hay épocas pagadas todavía", False)
        return
    altura = alturas[0]
    epoca = altura // contrato.BLOQUES_POR_EPOCA - 1
    nct(0)
    original = detalle = inflado = None
    try:
        original = bloque_en(altura)
        detalle = redis(f"print(json.dumps(r.hget('epoca:{epoca}', 'detalle')))")
        txs = [dict(t) for t in original["transactions"]]
        pago = next(t for t in txs if t["id"].startswith("part-"))
        pago["monto"] += 10**6
        inflado = reminar({**original, "transactions": txs}, original["prefijo"])
        guardar_bloque(inflado, en=altura)
        redis(f"r.hdel('epoca:{epoca}', 'detalle')\nprint(json.dumps(True))")  # /api/epocas/<n> sin la lista
        anotar(f"Bloque {altura}: {pago['id']} con 10^6 más y la lista de la época {epoca} borrada")
        nuevo = verificador_nuevo(a.backend)
        motivo = nuevo["rechazado"] and nuevo["rechazado"]["motivos"][0]
        comprobar("pago-epoca", f"la época {epoca} paga 10^6 más que el fondo, sin la lista publicada",
                  f"un nodo nuevo rechaza el bloque {altura}",
                  f"rechazado en {nuevo['rechazado'] and nuevo['rechazado']['altura']}: {motivo}",
                  bool(motivo) and nuevo["rechazado"]["altura"] == altura and "más que el fondo" in motivo)
    finally:
        if original:
            guardar_bloque(original, en=altura)
        if inflado:
            redis(f"r.delete('bloque:{inflado['block_hash']}')\nprint(json.dumps(True))")
        if detalle:
            redis(f"r.hset('epoca:{epoca}', 'detalle', {json.dumps(detalle)})\nprint(json.dumps(True))")
        nct(2)


def otra_cadena(a):
    """Lo firmado para otra cadena (como lo de antes de un reinicio, con los nonces de nuevo en 1) no entra."""
    vieja = contrato.md5("la cadena de antes del reinicio")
    backend = trampas.Backend(a.backend)
    nonce = backend.nonce(CUENTA["carol"]) + 1
    transferencia = contrato.transaccion(PRIVADA["carol"], CUENTA["bob"], 1, nonce, cadena=vieja)
    recarga = contrato.carga(PRIVADA["organizador"], backend.nonce(contrato.EMISION) + 1, CUENTA["operador"], 10**9,
                             int(time.time()), cadena=vieja)
    for que, tx in (("una transferencia de carol", transferencia), ("una carga del organizador", recarga)):
        codigo, respuesta = backend.mandar(tx)
        comprobar("otra-cadena", f"{que} firmada para otra cadena, con el nonce siguiente", "400: la firma no verifica",
                  f"{codigo} {respuesta}", codigo == 400 and "firma" in json.dumps(respuesta, ensure_ascii=False))
    buena = contrato.transaccion(PRIVADA["carol"], CUENTA["bob"], 1, nonce, cadena=genesis(a))
    codigo, _ = backend.mandar(buena)
    comprobar("otra-cadena", "la misma transferencia firmada para esta cadena", "202", str(codigo), codigo == 202)


class Trafico(trampas.Trafico):
    """Transferencias de alice y bob (carol queda libre para el escenario del saldo inventado)."""

    def run(self):
        privadas = [PRIVADA[n] for n in ("alice", "bob")]
        while not self.parar.is_set():
            try:
                trampas.rafaga(self.backend, privadas, self.cantidad, esperar=0, salida=lambda *_: None)
            except Exception as error:  # noqa: BLE001
                anotar(f"Tráfico: {error!r}")
            self.parar.wait(self.cada)


ESCENARIOS = ("saldo", "alta", "editado", "reescrita", "epoca", "invalido", "deshacer", "sin-validadores", "reminada",
              "pago-epoca", "otra-cadena")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--backend", default="http://localhost")
    parser.add_argument("--gateway", default="http://localhost:8081")
    parser.add_argument("--solo", default=",".join(ESCENARIOS))
    a = parser.parse_args()
    a.backend = a.backend.rstrip("/")
    os.environ.setdefault("SISTEMA_URL", a.backend)  # para pilar2/k8s/aprobar.sh
    solo = a.solo.split(",")
    minero = trampas.Minero("honesto-operador", a.gateway, procesos=4)
    trafico = Trafico(a.backend, cantidad=4, cada=20)
    trafico.start()
    validadores = []
    try:
        if {"reescrita", "invalido", "deshacer", "sin-validadores"} & set(solo):
            validadores = [NodoLocal(f"v{i}", a.backend, 8091 + i, validador=True).arrancar() for i in range(3)]
            for i, v in enumerate(validadores):
                subprocess.run([str(PILAR2 / "k8s" / "aprobar.sh"), v.cuenta(), "validador", f"v{i}-prueba"],
                               check=True, capture_output=True)
            anotar(f"Validadores: {[v.cuenta()[:12] for v in validadores]} (sus altas van a la cadena)")
            esperar_que(lambda: get(f"{a.backend}/api/finalidad")["altura"] >= 0, 300, descripcion="la primera finalidad")
        for nombre in ESCENARIOS:
            if nombre not in solo:
                continue
            anotar(f"── {nombre} ──")
            funcion = {"saldo": saldo, "alta": alta, "editado": editado, "epoca": epoca, "reminada": reminada,
                       "pago-epoca": pago_epoca, "otra-cadena": otra_cadena}.get(nombre)
            if funcion:
                funcion(a)
            else:
                {"reescrita": reescrita, "invalido": invalido, "deshacer": deshacer,
                 "sin-validadores": sin_validadores}[nombre](a, validadores)
    finally:
        trafico.parar.set()
        for v in validadores:  # con el minero todavía andando: si no, las bajas quedan pendientes
            subprocess.run([str(PILAR2 / "k8s" / "aprobar.sh"), v.cuenta(), "baja"], capture_output=True)
            v.borrar()
        if validadores:
            esperar_que(lambda: not get(f"{a.backend}/api/finalidad")["validadores"], 300,
                        descripcion="las bajas de los validadores de prueba")
        minero.parar()
        RESULTADOS.mkdir(exist_ok=True)
        with open(RESULTADOS / "operador.csv", "w", newline="") as archivo:
            escritor = csv.writer(archivo)
            escritor.writerow(["escenario", "comprobacion", "esperado", "observado", "ok"])
            escritor.writerows(trampas.REGISTRO)
        (RESULTADOS / "operador.log").write_text("\n".join(trampas.LOG) + "\n")
        bien = sum(1 for fila in trampas.REGISTRO if fila[4] == "sí")
        anotar(f"{bien} de {len(trampas.REGISTRO)} comprobaciones bien")


if __name__ == "__main__":
    main()
