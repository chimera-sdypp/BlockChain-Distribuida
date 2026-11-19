"""Prueba de los mineros que mienten (BC-76): cada trampa del epic BC-72 contra el sistema desplegado.

Corre mineros CPU locales contra el gateway (uno honesto y los tramposos de `minero_cpu/trampas.py`), manda
transacciones al backend para que haya bloques que minar, y mira en Redis y en los logs qué hizo el sistema.
Las cuentas de los tramposos se aprueban con `pilar2/k8s/aprobar.sh` y se dan de baja al final.

    pilar2/.venv/bin/python pilar3/pruebas/trampas.py --gateway http://<IP>:8081 --backend http://<Ingress>
        [--solo juntos,gpu-falsa,bench-bajo] [--segundos 300]

Escenarios:
- juntos: el honesto con bench-alto, acaparar, retener, sin-solucion y guardar, a la vez.
- retener: retener y acaparar solos: sin nadie que mine, sólo el plazo les puede sacar el rango.
- gpu-falsa: sólo la «GPU» falsa, con trabajo apilado: el escalador tiene que pedir VMs y el prefijo bajar por emergencia.
- bench-bajo: sólo el que declara la centésima parte, con PREFIJO_BASE=8: el ajuste por tiempo tiene que subir
  el prefijo por encima del techo que da ese bench.

Escribe resultados/trampas.csv (una fila por comprobación) y resultados/trampas.log (lo que pasó).
Usa el KUBECONFIG del entorno (k3s o GKE) y el namespace `blockchain`.
"""

import argparse
import csv
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
PILAR2 = RAIZ / "pilar2"
RESULTADOS = Path(__file__).resolve().parent / "resultados"
sys.path.insert(0, str(PILAR2))

from claves.herramienta import Backend, rafaga  # noqa: E402
from comun import contrato  # noqa: E402
from comun.tests.claves import PRIVADA  # noqa: E402

NS = "blockchain"
REGISTRO = []  # (escenario, comprobación, esperado, observado, ok)
LOG = []


def anotar(texto):
    linea = f"{time.strftime('%H:%M:%S')} {texto}"
    print(linea, flush=True)
    LOG.append(linea)


def comprobar(escenario, que, esperado, observado, ok):
    REGISTRO.append((escenario, que, esperado, observado, "sí" if ok else "NO"))
    anotar(f"[{'OK' if ok else 'MAL'}] {escenario}: {que} → {observado}")


def kubectl(*args, entrada=None):
    return subprocess.run(["kubectl", "-n", NS, *args], input=entrada, capture_output=True, text=True, check=True).stdout


def redis(script):
    """Corre `script` (Python, con `r` conectado al Redis del sistema) en un pod del NCT y devuelve su JSON."""
    codigo = "import json\nfrom comun import config, redisdb\nr = redisdb.conectar(config.cargar('trampas'))\n" + script
    return json.loads(kubectl("exec", "-i", "deploy/nct", "--", "python", "-", entrada=codigo).strip().splitlines()[-1])


def aprobar(cuenta, alias):
    subprocess.run([str(PILAR2 / "k8s" / "aprobar.sh"), cuenta, "minero", alias], check=True, capture_output=True)


def dar_de_baja(cuenta):
    subprocess.run([str(PILAR2 / "k8s" / "aprobar.sh"), cuenta, "baja"], check=True, capture_output=True)
    redis(f"r.hdel('suspendidos', '{cuenta}')\nprint(json.dumps(True))")


class Minero:
    """Un minero CPU local (python -m minero_cpu), honesto o con su TRAMPA."""

    def __init__(self, alias, gateway, trampa=None, procesos=2):
        self.alias, self.trampa = alias, trampa
        self.privada = secrets.token_hex(32)
        self.cuenta = contrato.clave_publica(self.privada)
        aprobar(self.cuenta, alias)
        entorno = {**os.environ, "GATEWAY_URL": gateway, "CLAVE_PRIVADA": self.privada, "MINERO_ID": alias,
                   "PROCESOS": str(procesos), "LOGS_DIR": str(RESULTADOS / "trampas-logs")}
        entorno.pop("TRAMPA", None)
        if trampa:
            entorno["TRAMPA"] = trampa
        RESULTADOS.joinpath("trampas-logs").mkdir(parents=True, exist_ok=True)
        self.salida = open(RESULTADOS / "trampas-logs" / f"{alias}.txt", "w")
        self.proceso = subprocess.Popen([sys.executable, "-m", "minero_cpu"], cwd=PILAR2, env=entorno,
                                        stdout=self.salida, stderr=subprocess.STDOUT)
        anotar(f"Minero {alias} ({trampa or 'honesto'}) arrancó: {self.cuenta[:12]}")

    def log(self):
        self.salida.flush()
        return (RESULTADOS / "trampas-logs" / f"{self.alias}.txt").read_text()

    def parar(self):
        self.proceso.terminate()
        try:
            self.proceso.wait(15)
        except subprocess.TimeoutExpired:
            self.proceso.kill()
        self.salida.close()
        dar_de_baja(self.cuenta)


class Trafico(threading.Thread):
    """Transferencias de alice, bob y carol cada `cada` segundos, para que siempre haya bloques que minar."""

    def __init__(self, backend, cantidad=30, cada=20):
        super().__init__(daemon=True)
        self.backend, self.cantidad, self.cada = Backend(backend), cantidad, cada
        self.parar = threading.Event()

    def run(self):
        privadas = [PRIVADA[n] for n in ("alice", "bob", "carol")]
        while not self.parar.is_set():
            try:
                rafaga(self.backend, privadas, self.cantidad, esperar=0, salida=lambda *_: None)
            except Exception as error:  # noqa: BLE001 — un corte del backend no frena la prueba
                anotar(f"Tráfico: {error!r}")
            self.parar.wait(self.cada)


def nodo(cuenta):
    return redis(f"print(json.dumps(r.hgetall('nodo:{cuenta}')))")


def suspension(cuenta):
    return redis(f"d = r.hget('suspendidos', '{cuenta}')\nprint(d or 'null')")


def elecciones_trp(desde):
    """(prefijo, motivo) de cada tarea partida desde `desde` (segundos Unix), del log del TrP, en orden de hora.

    Con `-l` kubectl junta los logs de un pod y después los del otro: se ordenan por la marca de tiempo."""
    salida = kubectl("logs", "-l", "app=trp", "--since", f"{int(time.time() - desde) + 5}s", "--tail=-1",
                     "--timestamps")
    lineas = sorted(l for l in salida.splitlines() if "motivo" in l)
    return [m.groups() for l in lineas if (m := re.search(r"prefijo (0+), .*motivo (\w+)", l))]


def esperar(segundos, motivo):
    anotar(f"Espero {segundos} s: {motivo}")
    time.sleep(segundos)


# ── Escenarios ──

def juntos(a):
    honesto = Minero("honesto", a.gateway, procesos=4)
    tramposos = {t: Minero(f"trampa-{t}", a.gateway, t) for t in ("bench-alto", "acaparar", "retener", "sin-solucion",
                                                                   "guardar")}
    trafico = Trafico(a.backend)
    trafico.start()
    try:
        esperar(a.segundos, "que cada trampa haga lo suyo")
        for t in ("sin-solucion", "guardar"):
            s = suspension(tramposos[t].cuenta)
            comprobar("juntos", f"{t}: queda suspendido", "suspendido por estadística",
                      f"{s['motivo']}, P = {s['evidencia'].get('probabilidad', 0):.1e}" if s else "no suspendido",
                      bool(s) and s["motivo"] == "estadistica")
        dados = re.findall(r"me dieron (\d+) distintos", tramposos["acaparar"].log())
        comprobar("juntos", "acaparar: pidió 10 rangos seguidos", "1 rango cada vez",
                  f"{len(dados)} veces, con {sorted(set(dados)) or '?'} rango(s)", bool(dados) and set(dados) == {"1"})
        alto = nodo(tramposos["bench-alto"].cuenta)
        potencia = contrato.potencia_efectiva(alto)
        comprobar("juntos", "bench-alto: cuenta lo medido, no lo declarado", "potencia ≪ bench declarado",
                  f"bench {float(alto.get('bench', 0)):.3g} H/s, {alto.get('medido_estado')} {float(alto.get('medido') or 0):.3g} "
                  f"H/s → potencia {potencia:.3g} H/s", alto.get("medido_estado") == "medido"
                  and potencia < float(alto.get("bench", 0)) / 10)
        h = nodo(honesto.cuenta)
        comprobar("juntos", "honesto: no se lo suspende y mide parecido a su bench", "no suspendido",
                  f"{h.get('medido_estado')}: medido {float(h.get('medido') or 0):.3g} H/s, bench "
                  f"{float(h.get('bench', 0)):.3g} H/s", suspension(honesto.cuenta) is None)
        bloques = redis("print(json.dumps(r.llen('cadena')))")
        anotar(f"Altura al final: {bloques}")
    finally:
        trafico.parar.set()
        for m in (honesto, *tramposos.values()):
            m.parar()


def retener(a):
    """Sin nadie que mine, la tarea queda abierta: lo único que le puede sacar el rango al que lo retiene es el plazo."""
    inicio = time.time()
    tramposos = [Minero(f"trampa-{t}", a.gateway, t) for t in ("retener", "acaparar")]
    trafico = Trafico(a.backend, cantidad=10, cada=60)
    trafico.start()
    try:
        esperar(300, "que se les pase el plazo (sin medición: 120 s)")
        salida = kubectl("logs", "-l", "app=gateway", "--since", f"{int(time.time() - inicio) + 5}s", "--tail=-1")
        for m in tramposos:
            sacados = [float(x) for x in re.findall(r"me sacaron el rango \S+ a los (\d+) s", m.log())]
            cortes = len(re.findall(f"A {m.cuenta} se le pasó el plazo", salida))
            comprobar("retener", f"{m.trampa}: el plazo le saca el rango", "a los ~120 s (sin medición)",
                      f"{cortes} corte(s) por plazo en el gateway; se lo sacaron a los "
                      f"{', '.join(f'{x:.0f}' for x in sacados) or '—'} s",
                      cortes >= 1 and any(100 <= x <= 135 for x in sacados))
    finally:
        trafico.parar.set()
        for m in tramposos:
            m.parar()


def gpu_falsa(a):
    inicio = time.time()
    falsa = Minero("trampa-gpu-falsa", a.gateway, "gpu-falsa")
    trafico = Trafico(a.backend, cantidad=20, cada=60)
    trafico.start()
    try:
        esperar(max(a.segundos, 330), "a que el escalador decida y venza la tarea (240 s)")
        vms = redis("print(json.dumps(r.get('escalador:vms')))")
        comprobar("gpu-falsa", "el escalador pide VMs aunque haya una «GPU» viva", "1 o más VMs",
                  f"{vms or 0} VM(s) pedidas", int(vms or 0) >= 1)
        elecciones = elecciones_trp(inicio)
        emergencias = [len(p) for p, motivo in elecciones if motivo == "emergencia"]
        comprobar("gpu-falsa", "sin bloque en 240 s, el prefijo baja", "motivo emergencia",
                  f"prefijos {[len(p) for p, _ in elecciones]}, motivos {[m for _, m in elecciones]}",
                  bool(emergencias))
    finally:
        trafico.parar.set()
        falsa.parar()


def prefijos_desde(altura):
    """Los prefijos de los bloques minados después de `altura`, en orden (de la cadena en Redis)."""
    return redis(f"""
hashes = r.lrange('cadena', {altura} + 1, -1)
print(json.dumps([len(r.hget('bloque:' + h, 'prefijo') or '') for h in hashes]))""")


def trp_con(base):
    kubectl("set", "env", "deploy/trp", f"PREFIJO_BASE={base}", f"ESPACIO_NONCES={max(2 ** 32, 16 ** base * 4)}")
    kubectl("rollout", "status", "deploy/trp", "--timeout=180s")


def bench_bajo(a):
    """El que declara la centésima parte: el techo que da su bench es 5, pero el tiempo tiene que subir el prefijo.

    Primero se mina con PREFIJO_BASE=5 (así todos los bloques arrancan en 5) y después se sube el tope a 8."""
    bajo = Minero("trampa-bench-bajo", a.gateway, "bench-bajo", procesos=6)
    trafico = Trafico(a.backend, cantidad=100, cada=15)
    try:
        anotar("Fase 1: PREFIJO_BASE=5, hasta minar 3 bloques")
        trp_con(5)
        trafico.start()
        inicio = redis("print(json.dumps(r.llen('cadena') - 1))")
        limite = time.time() + 300
        while time.time() < limite and len(prefijos_desde(inicio)) < 3:
            time.sleep(10)
        anotar("Fase 2: PREFIJO_BASE=8 (y ESPACIO_NONCES 16^8 × 4): el tope ya no limita")
        trp_con(8)
        limite = time.time() + a.segundos_bench_bajo
        while time.time() < limite:
            time.sleep(20)
            prefijos = prefijos_desde(inicio)
            if prefijos.count(7) >= 2:
                break
        b = nodo(bajo.cuenta)
        techo = 1
        while techo < 8 and 16 ** (techo + 1) <= float(b.get("bench", 0)) * 60:
            techo += 1
        prefijos = prefijos_desde(inicio)
        comprobar("bench-bajo", "el ajuste por tiempo pasa el techo del bench declarado",
                  f"prefijo > {techo} sin cambiar la potencia",
                  f"bench declarado {float(b.get('bench', 0)):.3g} H/s (techo {techo}); prefijo de cada bloque, en orden: "
                  f"{prefijos}", bool(prefijos) and max(prefijos) > techo)
    finally:
        trafico.parar.set()
        bajo.parar()
        trp_con(6)


ESCENARIOS = {"juntos": juntos, "retener": retener, "gpu-falsa": gpu_falsa, "bench-bajo": bench_bajo}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", required=True, help="http://<IP del gateway>:8081")
    p.add_argument("--backend", required=True, help="http://<Ingress> (la API de la app)")
    p.add_argument("--solo", default=",".join(ESCENARIOS), help="escenarios separados por coma")
    p.add_argument("--segundos", type=int, default=240, help="cuánto dura cada escenario")
    p.add_argument("--segundos-bench-bajo", type=int, default=900, help="tope de la fase 2 de bench-bajo")
    a = p.parse_args()
    RESULTADOS.mkdir(exist_ok=True)
    try:
        for nombre in a.solo.split(","):
            anotar(f"── Escenario {nombre} ──")
            ESCENARIOS[nombre](a)
    finally:
        # Los escenarios que no se corrieron ahora conservan su resultado anterior.
        corridos, previos = {r[0] for r in REGISTRO}, []
        if (RESULTADOS / "trampas.csv").exists():
            with open(RESULTADOS / "trampas.csv") as archivo:
                previos = [tuple(f) for f in list(csv.reader(archivo))[1:] if f and f[0] not in corridos]
        with open(RESULTADOS / "trampas.csv", "w", newline="") as archivo:
            w = csv.writer(archivo)
            w.writerow(["escenario", "comprobacion", "esperado", "observado", "ok"])
            orden = list(ESCENARIOS)
            w.writerows(sorted(previos + REGISTRO, key=lambda f: orden.index(f[0]) if f[0] in orden else 99))
        with open(RESULTADOS / "trampas.log", "a") as archivo:
            archivo.write("\n".join(LOG) + "\n")
    malos = [r for r in REGISTRO if r[-1] != "sí"]
    print(f"\n{len(REGISTRO) - len(malos)} de {len(REGISTRO)} comprobaciones bien")
    sys.exit(1 if malos else 0)


if __name__ == "__main__":
    main()
