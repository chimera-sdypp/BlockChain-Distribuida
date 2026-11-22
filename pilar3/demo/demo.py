"""Los pasos de la demo (BC-66), uno por subcomando, contra el sistema desplegado: nada se tipea en vivo.

    pilar2/.venv/bin/python pilar3/demo/demo.py <paso> [opciones]

    estado                     altura, prefijo, nodos, finalidad, mineros, pods y VMs
    transferencia [--monto N]  alice → bob firmada, de punta a punta, con su recorrido por los servicios
    rechazos                   firma falsa, reenvío, sin saldo, cuenta sin alta y minero sin aprobar
    rafaga [N]                 N transferencias firmadas en ronda entre alice, bob y carol (20 por defecto)
    gpu sacar | volver         apaga o prende el minero GPU del k3s y muestra qué hace el escalador
    matar nct|trp|gateway|redis  borra un pod (en redis, el primario) y confirma una transferencia enseguida
    operador                   el operador infla un saldo en Redis: el NCT lo rechaza y lo corrige desde la cadena

Variables: SISTEMA (https://blockchain-chimera.chimera-sdypp.workers.dev), GATEWAY (http://136.80.87.183:8081), KUBE_GKE
(~/.kube/tpi-gke.yaml), KUBE_K3S (~/.kube/config, el k3s con la RTX 4060) y PROYECTO (el de GCP, para ver las VMs). alice, bob y carol son las cuentas de
prueba del génesis (pilar2/comun/tests/claves.py).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PILAR2 = Path(__file__).resolve().parents[2] / "pilar2"
sys.path.insert(0, str(PILAR2))

from claves.herramienta import Backend  # noqa: E402
from comun import contrato  # noqa: E402
from comun.tests.claves import CUENTA, PRIVADA  # noqa: E402

SISTEMA = os.environ.get("SISTEMA", "https://blockchain-chimera.chimera-sdypp.workers.dev").rstrip("/")
GATEWAY = os.environ.get("GATEWAY", "http://136.80.87.183:8081").rstrip("/")
KUBE_GKE = os.path.expanduser(os.environ.get("KUBE_GKE", "~/.kube/tpi-gke.yaml"))
KUBE_K3S = os.path.expanduser(os.environ.get("KUBE_K3S", "~/.kube/config"))
PROYECTO = os.environ.get("PROYECTO", "project-cbf8aae8-a3f0-4ebd-9fd")
NS = "blockchain"
backend = Backend(SISTEMA, timeout=20)
INICIO = time.monotonic()


def decir(texto=""):
    print(f"[{time.monotonic() - INICIO:6.1f} s] {texto}" if texto else "", flush=True)


def get(url):
    pedido = urllib.request.Request(url, headers={"User-Agent": "chimera-demo"})  # el de urllib lo frena Cloudflare
    with urllib.request.urlopen(pedido, timeout=20) as respuesta:
        return json.loads(respuesta.read())


def kubectl(*args, kube=KUBE_GKE, ns=NS, check=False, entrada=None):
    resultado = subprocess.run(["kubectl", "--kubeconfig", kube, "-n", ns, *args], capture_output=True, text=True,
                               input=entrada)
    if check and resultado.returncode:
        raise SystemExit(f"kubectl {' '.join(args)}: {resultado.stderr.strip()}")
    return resultado.stdout


def redis(script):
    """Corre `script` con `r` conectado al Redis del sistema, en un pod del TrP (como lo haría el operador)."""
    codigo = "import json\nfrom comun import config, contrato, redisdb\nr = redisdb.conectar(config.cargar('demo'))\n"
    salida = kubectl("exec", "-i", "deploy/trp", "--", "python", "-", entrada=codigo + script, check=True)
    return json.loads(salida.strip().splitlines()[-1])


def punta():
    return get(f"{SISTEMA}/api/bloques?limite=1")[0]


def corto(cuenta):
    return f"{cuenta[:12]}…" if cuenta and len(cuenta) > 12 else cuenta


# ── Seguir una transferencia ──

def seguir(tx, segundos=600):
    """Muestra cada cambio de estado hasta que se confirma o se rechaza. Devuelve (estado, transacción)."""
    anterior, limite = None, time.monotonic() + segundos
    while time.monotonic() < limite:
        actual = backend.estado(tx["id"]) or {}
        if actual.get("estado") != anterior:
            anterior = actual.get("estado")
            extra = f" ({actual['motivo']})" if actual.get("motivo") else ""
            decir(f"  {tx['id']}: {anterior}{extra}")
            if anterior in ("confirmada", "rechazada"):
                return anterior, actual
        time.sleep(0.3)
    decir(f"  {tx['id']}: sigue «{anterior}» después de {segundos} s")
    return anterior, {}


def transferir(origen="alice", destino="bob", monto=1, reintentar=60):
    """Firma y manda. Mientras el backend conteste 503 (Redis cambiando de primario) reintenta, hasta `reintentar` s."""
    limite = time.monotonic() + reintentar
    while True:
        codigo, datos = backend.pedir("GET", f"/api/cuentas/{CUENTA[origen]}")
        if codigo == 200:
            tx = contrato.transaccion(PRIVADA[origen], CUENTA[destino], monto, datos["nonce"] + 1)
            codigo, datos = backend.mandar(tx)
            decir(f"{origen} → {destino}, {monto}, nonce {tx['nonce']}: POST /api/transacciones → {codigo}")
            if codigo == 202:
                return tx
        if codigo != 503 or time.monotonic() > limite:
            raise SystemExit(f"El backend no la aceptó: {codigo} {datos}")
        decir(f"  el backend contesta 503 ({datos}): reintento en 2 s")
        time.sleep(2)


def bloque_de(hash_):
    for bloque in get(f"{SISTEMA}/api/bloques?limite=5"):
        if bloque["block_hash"] == hash_:
            return bloque
    return None


def logs(app, desde="10m"):
    return kubectl("logs", "-l", f"app={app}", "--since", desde, "--tail=-1", "--prefix", "--max-log-requests=4")


def recorrido(tx, bloque):
    """Las líneas de log de cada servicio con el id de la transacción o de su tarea (lo mismo que se ve en Loki)."""
    tareas = re.findall(rf"Tarea (\w+) publicada: bloque {bloque['altura']} ", logs("nct"))
    claves = [tx["id"], *tareas[-1:], f"Bloque {bloque['altura']} confirmado"]
    decir(f"Recorrido por los servicios (tarea {tareas[-1] if tareas else '—'}):")
    lineas, descartados = [], []
    for app in ("backend", "nct", "trp", "gateway"):
        for linea in logs(app).splitlines():
            if any(clave in linea for clave in claves) and "GET /" not in linea and "POST /" not in linea:
                hora, texto = linea.split("] ", 1)[-1][:19], linea.split(" | ", 2)[-1]
                if texto.startswith("Descarto el rango"):  # uno por rango: se resumen en una línea
                    descartados.append((hora, texto.split()[3]))
                    continue
                lineas.append((hora, app, texto[:150]))
    if descartados:
        lineas.append((descartados[0][0], "gateway", f"Descarta los rangos {', '.join(r for _, r in descartados)}: "
                                                     "la tarea ya se cerró"))
    for hora, app, texto in sorted(lineas):
        print(f"           {hora}  {app:<8} {texto}")
    filtro = "|".join(claves[:2])
    print(f"\n  En Grafana → Explore → Loki:  {{app=~\"backend|nct|trp|gateway\"}} |~ \"{filtro}\"")


# ── Pasos ──

def estado(_):
    tip = punta()
    decir(f"Cadena: altura {tip['altura']}, prefijo {len(tip['prefijo'])} ({tip['prefijo']}), punta {tip['block_hash']}")
    nodos = [get(f"{SISTEMA}/nodos/{n}/api/estado") for n in (0, 1)]
    for n, e in enumerate(nodos):
        decir(f"Nodo {n}: altura {e['altura']}, raíz {e['raiz'][:12]}…, conflicto {e['conflicto']}, "
              f"rechazado {e['rechazado'] and e['rechazado']['altura']}")
    decir("Los nodos coinciden con la cadena" if all(e["punta"] == tip["block_hash"] for e in nodos)
          else "Algún nodo todavía no llegó a la punta (verifican cada pocos segundos)")
    final = get(f"{SISTEMA}/api/finalidad")
    decir(f"Finalidad: altura {final['altura']} con {len(final['validadores'])} validadores (umbral {final['umbral']})")
    for m in get(f"{GATEWAY}/mineros"):
        if m.get("vivo"):
            potencia = f"{float(m['potencia']) / 1e6:,.0f}".replace(",", ".")
            decir(f"Minero vivo: {m['alias']} ({m['tipo']}, {m.get('placa', '')}), potencia {potencia} MH/s, "
                  f"{m['bloques']} bloques")
    pods = kubectl("get", "pods", "--no-headers")
    cuenta = {}
    for linea in pods.splitlines():
        nombre = re.sub(r"-[0-9a-z]{8,10}-[0-9a-z]{5}$|-\d+$", "", linea.split()[0])
        cuenta[nombre] = cuenta.get(nombre, 0) + 1
    decir("Pods: " + ", ".join(f"{n} ×{c}" for n, c in sorted(cuenta.items())))
    decir(f"VMs minero-cpu: {vms() or 'ninguna'}")


def transferencia(a):
    tx = transferir(monto=a.monto)
    final, datos = seguir(tx)
    if final != "confirmada":
        return
    bloque = bloque_de(datos.get("bloque")) or punta()
    coinbase = bloque["transactions"][0]
    decir(f"Bloque {bloque['altura']} ({bloque['block_hash']}), prefijo {len(bloque['prefijo'])}, "
          f"{len(bloque['transactions'])} transacciones; la coinbase paga {coinbase['monto']} a {corto(coinbase['destino'])}")
    recorrido(tx, bloque)


def mandar_y_seguir(tx):
    codigo, respuesta = backend.mandar(tx)
    decir(f"   → {codigo}" + ("" if codigo == 202 else f" {respuesta}"))
    if codigo == 202:
        seguir(tx, 30)


def rechazos(_):
    nonce = backend.nonce(CUENTA["alice"]) + 1
    buena = contrato.transaccion(PRIVADA["alice"], CUENTA["bob"], 1, nonce)
    decir("1. Firma falsa: la transferencia firmada por alice, con el monto cambiado después de firmar")
    codigo, respuesta = backend.mandar({**buena, "monto": 1000})
    decir(f"   → {codigo} {respuesta}")
    decir("2. Reenvío: la misma transferencia dos veces")
    decir(f"   → primera {backend.mandar(buena)[0]}, segunda {backend.mandar(buena)[0]}")
    seguir(buena, 30)
    decir("3. Sin saldo: bob manda más de lo que tiene")
    bob = backend.cuenta(CUENTA["bob"])
    mandar_y_seguir(contrato.transaccion(PRIVADA["bob"], CUENTA["carol"], bob["saldo"] + 1, bob["nonce"] + 1))
    decir("4. Una cuenta que nadie aprobó")
    privada, cuenta = contrato.nueva_clave()
    mandar_y_seguir(contrato.transaccion(privada, CUENTA["bob"], 1, 1))
    decir("5. Un minero sin aprobar se presenta al gateway (POST /hello firmado con una clave nueva)")
    cuerpo = json.dumps({"minero": cuenta, "tipo": "cpu", "placa": "demo", "alias": "minero-sin-aprobar"}).encode()
    pedido = urllib.request.Request(f"{GATEWAY}/hello", data=cuerpo, method="POST", headers={
        "Content-Type": "application/json", **contrato.firmar_pedido(privada, "POST", "/hello", cuerpo)})
    try:
        urllib.request.urlopen(pedido, timeout=20)
        decir("   → lo aceptó (¿estaba aprobado?)")
    except urllib.error.HTTPError as error:
        decir(f"   → {error.code} {error.read().decode()[:120]}")
    decir(f"   Queda como pedido de alta de {corto(cuenta)}: el organizador lo ve en la billetera y lo rechaza.")
    print('\n  En Loki:  {app=~"backend|nct|gateway"} |~ "rechaz|no es un minero aprobado|firma"')


def rafaga(a):
    decir(f"Ráfaga de {a.cantidad} transferencias firmadas (alice → bob → carol → alice…)")
    print('  En Grafana: «Transacciones apiladas en Redis», «Mensajes por cola de RabbitMQ», «Bloques por minuto»')
    subprocess.run([str(Path(__file__).resolve().parents[1] / "pruebas" / "rafaga.sh"), SISTEMA, str(a.cantidad)],
                   env={**os.environ, "PYTHON": sys.executable})


def vms():
    salida = subprocess.run(["gcloud", "compute", "instances", "list", f"--project={PROYECTO}", "--filter=name~^minero-cpu",
                             "--format=value(name,status)"], capture_output=True, text=True).stdout
    return ", ".join(" ".join(linea.split()) for linea in salida.splitlines())


def mirar_escalado(minutos, hasta):
    """Cada 10 s: GPU vivas, el escalador, las VMs y los bloques, hasta que `hasta` se cumple o pasan los minutos."""
    limite = time.monotonic() + minutos * 60
    while time.monotonic() < limite:
        mineros = [m for m in get(f"{GATEWAY}/mineros") if m.get("vivo")]
        gpus = [m["alias"] for m in mineros if m["tipo"] == "gpu"]
        cpus = [m["alias"] for m in mineros if m["tipo"] == "cpu"]
        escalador = kubectl("get", "deploy", "escalador", "-o", "jsonpath={.spec.replicas}") or "0"
        tip = punta()
        decir(f"GPU vivas {gpus or '—'} · CPU vivas {cpus or '—'} · escalador {escalador} pod · VMs {vms() or '—'} · "
              f"bloque {tip['altura']} (prefijo {len(tip['prefijo'])})")
        if hasta(gpus, cpus):
            return True
        time.sleep(10)
    return False


def gpus_vivas():
    return [m["alias"] for m in get(f"{GATEWAY}/mineros") if m.get("vivo") and m["tipo"] == "gpu"]


def gpu(a):
    replicas = 0 if a.accion == "sacar" else 1
    antes = gpus_vivas()
    kubectl("scale", "sts", "minero-gpu", f"--replicas={replicas}", kube=KUBE_K3S, ns="mineros-gpu", check=True)
    decir(f"Minero GPU del k3s en {replicas} réplica(s)")
    if a.accion == "sacar":
        decir("Sin GPU y con trabajo, KEDA prende el escalador y éste pide VMs (30 s de espera para no oscilar)")
        print('  En Grafana: «Mineros vivos por tipo», «VMs CPU», «Prefijo actual» y la alerta SinMinerosGPU')
        otras = [g for g in antes if not g.startswith("minero-gpu-")]
        if otras:
            decir(f"Ojo: siguen vivas otras GPU ({', '.join(otras)}); mientras minen, el escalador no pide VMs")
        transferir("carol", "alice", 1)  # trabajo apilado: sin transacciones el escalador no pide nada
        mirar_escalado(a.minutos, lambda gpus, cpus: not gpus and cpus)
    else:
        decir("Con la GPU de vuelta (tarda ~1 min en arrancar y medirse), el escalador espera 120 s y borra la VM")
        mirar_escalado(a.minutos, lambda gpus, cpus: len(gpus) > len(antes) and not vms())


def primario_de_redis(preguntar_a):
    salida = kubectl("exec", preguntar_a, "-c", "sentinel", "--", "redis-cli", "-p", "26379", "sentinel",
                     "get-master-addr-by-name", "mymaster")
    return salida.split()[0].split(".")[0] if salida.strip() else "?"


def matar(a):
    if a.servicio == "redis":
        pod = primario_de_redis("redis-0")
        decir(f"El primario de Redis es {pod}")
    else:
        pod = kubectl("get", "pods", "-l", f"app={a.servicio}", "-o", "name").split()[0].removeprefix("pod/")
    kubectl("delete", "pod", pod, "--wait=false", check=True)
    decir(f"Borré {pod}; mando una transferencia enseguida")
    tx = transferir()
    final, _ = seguir(tx)
    decir(f"La transferencia quedó {final}")
    if a.servicio == "redis":
        otro = next(p for p in ("redis-0", "redis-1", "redis-2") if p != pod)
        decir(f"Primario ahora: {primario_de_redis(otro)} (Sentinel promovió una réplica)")
    print(kubectl("get", "pods", "-l", f"app={a.servicio}"))


def operador(_):
    carol = CUENTA["carol"]
    antes = backend.cuenta(carol)["saldo"]
    decir(f"Saldo de carol según la API: {antes}")
    redis(f"r.hincrby('saldos', '{carol}', 1000000)\nprint(json.dumps(True))")
    decir(f"El operador hace HINCRBY saldos carol +1.000.000 en Redis: la API ahora dice "
          f"{backend.cuenta(carol)['saldo']}")
    gasto = contrato.transaccion(PRIVADA["carol"], CUENTA["bob"], antes + 500_000, backend.nonce(carol) + 1)
    decir(f"carol intenta gastar {antes + 500_000}: POST → {backend.mandar(gasto)[0]}")
    seguir(gasto, 60)
    decir("Cada 60 s el NCT compara el caché con su estado (que sale de la cadena) y lo corrige:")
    limite = time.monotonic() + 120
    while time.monotonic() < limite and backend.cuenta(carol)["saldo"] != antes:
        time.sleep(3)
    decir(f"Saldo de carol según la API: {backend.cuenta(carol)['saldo']}")
    for linea in logs("nct", "5m").splitlines():
        if "Caché distinto" in linea:
            print("           " + linea.split(" | ", 2)[-1][:160])
    for n in (0, 1):
        e = get(f"{SISTEMA}/nodos/{n}/api/estado")
        decir(f"Nodo {n}: altura {e['altura']}, rechazado {e['rechazado']} (ningún bloque malo llegó a la cadena)")
    print('\n  En Grafana: alerta CacheDistintoDeLaCadena · En Loki: {app="nct"} |= "Caché distinto"')
    print(f"  En la billetera (o `python -m claves saldo --cuenta {carol} --url {SISTEMA} --verificar`) "
          "el saldo de carol nunca cambió: lo calcula desde los bloques.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    pasos = parser.add_subparsers(dest="paso", required=True)
    pasos.add_parser("estado").set_defaults(funcion=estado)
    p = pasos.add_parser("transferencia")
    p.add_argument("--monto", type=int, default=1)
    p.set_defaults(funcion=transferencia)
    pasos.add_parser("rechazos").set_defaults(funcion=rechazos)
    p = pasos.add_parser("rafaga")
    p.add_argument("cantidad", type=int, nargs="?", default=20)
    p.set_defaults(funcion=rafaga)
    p = pasos.add_parser("gpu")
    p.add_argument("accion", choices=("sacar", "volver"))
    p.add_argument("--minutos", type=float, default=5)
    p.set_defaults(funcion=gpu)
    p = pasos.add_parser("matar")
    p.add_argument("servicio", choices=("nct", "trp", "gateway", "redis"))
    p.set_defaults(funcion=matar)
    pasos.add_parser("operador").set_defaults(funcion=operador)
    a = parser.parse_args()
    a.funcion(a)


if __name__ == "__main__":
    main()
