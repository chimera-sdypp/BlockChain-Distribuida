"""Prueba de entrada y salida de GPU (BC-41): apaga los mineros GPU del k3s, mide cómo el sistema
pasa a mineros CPU en VMs, los vuelve a prender y mide cómo las VMs se borran.

Cada ciclo:
  1. Espera el reposo: GPU viva, ninguna VM `minero-cpu-*` y el escalador en 0 pods.
  2. `kubectl scale --replicas=0` del StatefulSet del minero GPU (k3s) → t = 0.
  3. Mide hasta el primer bloque minado por una CPU: GPU dada por muerta, alerta SinMinerosGPU,
     escalador prendido por KEDA, VM creada y corriendo, minero CPU vivo, primer bloque CPU.
  4. Vuelve a subir el StatefulSet → t = 0 de la vuelta.
  5. Mide la vuelta: GPU viva, primer bloque GPU, alerta apagada, VM borrada y CPU dada por muerta.
  6. Deja de mandar transferencias y mide hasta que KEDA apaga el escalador.

Desde que sale la GPU hasta que se borra la VM, manda una transferencia de alice cada vez que se
confirma la anterior: siempre hay trabajo pendiente, sin saltear nonces. Cada `--periodo` s anota una muestra (mineros vivos por tipo,
VMs, pods del escalador, alerta) y cada bloque nuevo.

    python pilar3/pruebas/gpu_ciclos.py --api https://<ingreso> --ciclos 3 --salida pilar3/pruebas/resultados/gpu-ciclos

Deja `<salida>-eventos.csv`, `<salida>-muestras.csv` y `<salida>-bloques.csv` (se agregan filas).
Necesita: KUBECONFIG apuntando a GKE, `--kube-gpu` al k3s del minero GPU, y `gcloud` con acceso al
proyecto (PROYECTO o `--proyecto`). Los únicos GPU vivos tienen que ser los del StatefulSet: un
Colab prendido mantiene la red con GPU y el escalador no levanta nada. Al terminar (o con Ctrl+C)
deja el StatefulSet con las réplicas que tenía. Correrlo con el Python de pilar2 (firma con alice).
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone

from firmar import CUENTA, PRIVADA, contrato
from prefijos import api, kubectl

EVENTOS = ["ciclo", "fase", "evento", "segundos", "instante"]
MUESTRAS = ["instante", "ciclo", "fase", "segundos", "gpus_vivas", "cpus_vivas", "vms", "vms_corriendo",
            "escalador", "alerta", "altura", "prefijo"]
BLOQUES = ["altura", "instante", "ciclo", "fase", "prefijo", "duracion", "minero", "tipo", "transacciones"]

# Lo que se espera en cada fase, en orden. La fase termina con el último.
SALIDA = ["gpu_muerta", "alerta_pendiente", "alerta_disparada", "escalador_arriba", "vm_creada",
          "vm_corriendo", "cpu_viva", "primer_bloque_cpu"]
VUELTA = ["gpu_viva", "primer_bloque_gpu", "alerta_apagada", "cpu_muerta", "vm_borrada"]
# Con transferencias llegando, KEDA deja el escalador prendido (hay tx en cajas): al borrarse las VMs
# se dejan de mandar y se mide cuánto tarda en apagarse.
APAGADO = ["escalador_abajo"]


def ahora():
    return datetime.now(timezone.utc)


class Sistema:
    """Lo que la prueba mira y toca: la API pública, GKE, el k3s del minero GPU y las VMs."""

    def __init__(self, args):
        self.base = args.api.rstrip("/")
        self.kube_gpu, self.ns_gpu, self.sts = args.kube_gpu, args.ns_gpu, args.sts
        self.proyecto = args.proyecto
        self.tipos = {}  # cuenta → tipo, también de los mineros que ya se dieron de baja

    def k3s(self, *args):
        return subprocess.run(["kubectl", "--kubeconfig", self.kube_gpu, "-n", self.ns_gpu, *args],
                              check=True, capture_output=True, text=True).stdout

    def replicas_gpu(self):
        return int(self.k3s("get", "sts", self.sts, "-o", "jsonpath={.spec.replicas}") or 0)

    def escalar_gpu(self, replicas):
        self.k3s("scale", "sts", self.sts, f"--replicas={replicas}")

    def mineros(self):
        lista = api(self.base, "/api/mineros")
        for m in lista:
            self.tipos[m["id"]] = m["tipo"]
        return lista

    def vms(self):
        """(total, corriendo) de las VMs minero-cpu-*."""
        salida = subprocess.run(
            ["gcloud", "compute", "instances", "list", f"--project={self.proyecto}",
             "--filter=name~^minero-cpu", "--format=value(name,status)"],
            check=True, capture_output=True, text=True).stdout.split("\n")
        filas = [linea.split() for linea in salida if linea.strip()]
        return len(filas), sum(f[-1] == "RUNNING" for f in filas)

    def escalador(self):
        salida = kubectl("get", "deploy/escalador", "-o", "jsonpath={.status.readyReplicas}")
        return int(salida or 0)

    def alerta(self):
        """'' (inactiva), 'pending' o 'firing' de SinMinerosGPU, preguntándole a Prometheus."""
        consulta = 'ALERTS{alertname="SinMinerosGPU"}'
        ruta = ("/api/v1/namespaces/monitoring/services/monitoreo-prometheus:9090/proxy/api/v1/query?query="
                + urllib.parse.quote(consulta))
        datos = json.loads(subprocess.run(["kubectl", "get", "--raw", ruta], check=True,
                                          capture_output=True, text=True).stdout)
        estados = {r["metric"].get("alertstate") for r in datos["data"]["result"]}
        return "firing" if "firing" in estados else "pending" if "pending" in estados else ""

    def bloques(self, limite=20):
        return api(self.base, f"/api/bloques?limite={limite}")


class Transferencias(threading.Thread):
    """Una transferencia de alice a bob cada vez que se confirma la anterior."""

    def __init__(self, base):
        super().__init__(daemon=True)
        self.base, self.parar, self.activo, self.enviadas = base, threading.Event(), threading.Event(), 0

    def run(self):
        ultima = None
        while not self.parar.is_set():
            if not self.activo.is_set():
                self.parar.wait(1)
                continue
            try:
                confirmado = api(self.base, f"/api/cuentas/{CUENTA['alice']}")["nonce"]
                if ultima is None or confirmado >= ultima:
                    ultima = confirmado + 1
                    api(self.base, "/api/transacciones",
                        contrato.transaccion(PRIVADA["alice"], CUENTA["bob"], 1, ultima))
                    self.enviadas += 1
            except (urllib.error.URLError, TimeoutError, KeyError) as error:
                print(f"   transferencia: {error}", flush=True)
                ultima = None
            self.parar.wait(3)


class Registro:
    def __init__(self, prefijo):
        self.archivos, self.escritores = {}, {}
        for nombre, columnas in (("eventos", EVENTOS), ("muestras", MUESTRAS), ("bloques", BLOQUES)):
            archivo = open(f"{prefijo}-{nombre}.csv", "a", newline="")
            escritor = csv.DictWriter(archivo, columnas)
            if archivo.tell() == 0:
                escritor.writeheader()
            self.archivos[nombre], self.escritores[nombre] = archivo, escritor

    def fila(self, nombre, **datos):
        self.escritores[nombre].writerow(datos)
        self.archivos[nombre].flush()

    def cerrar(self):
        for archivo in self.archivos.values():
            archivo.close()


def fase(sistema, registro, ciclo, nombre, esperados, limite, periodo, altura_vista):
    """Muestrea hasta ver todos los eventos de `esperados` (o hasta `limite` s). Devuelve la altura vista."""
    inicio, vistos = time.monotonic(), {}
    print(f"== ciclo {ciclo}, {nombre}", flush=True)

    def marcar(evento):
        if evento in esperados and evento not in vistos:
            segundos = time.monotonic() - inicio
            vistos[evento] = segundos
            registro.fila("eventos", ciclo=ciclo, fase=nombre, evento=evento, segundos=round(segundos, 1),
                          instante=ahora().isoformat(timespec="seconds"))
            print(f"   {segundos:6.1f} s  {evento}", flush=True)

    t0_unix = time.time()
    while any(e not in vistos for e in esperados):
        segundos = time.monotonic() - inicio
        if segundos > limite:
            print(f"   {limite} s sin {', '.join(e for e in esperados if e not in vistos)}; sigo", flush=True)
            registro.fila("eventos", ciclo=ciclo, fase=nombre, evento="limite", segundos=limite,
                          instante=ahora().isoformat(timespec="seconds"))
            break
        try:
            mineros = sistema.mineros()
            gpus = sum(m["vivo"] and m["tipo"] == "gpu" for m in mineros)
            cpus = sum(m["vivo"] and m["tipo"] == "cpu" for m in mineros)
            vms, corriendo = sistema.vms()
            escalador, alerta = sistema.escalador(), sistema.alerta()
            nuevos = [b for b in sistema.bloques() if b["altura"] > altura_vista]
        except (subprocess.CalledProcessError, urllib.error.URLError, TimeoutError) as error:
            print(f"   muestra: {error}", flush=True)
            time.sleep(periodo)
            continue
        for b in sorted(nuevos, key=lambda b: b["altura"]):
            tipo = sistema.tipos.get(b["minero"], "?")
            registro.fila("bloques", altura=b["altura"], ciclo=ciclo, fase=nombre, prefijo=len(b["prefijo"]),
                          instante=datetime.fromtimestamp(b["timestamp"], timezone.utc).isoformat(),
                          duracion=b.get("duracion", ""), minero=b["minero"][:12], tipo=tipo,
                          transacciones=len(b["transactions"]))
            print(f"   bloque {b['altura']}: prefijo {len(b['prefijo'])}, {b.get('duracion')} s, {tipo}", flush=True)
            if b["timestamp"] >= t0_unix - 1:
                marcar(f"primer_bloque_{tipo}")
        altura_vista = max([altura_vista, *(b["altura"] for b in nuevos)])
        ultimo = max(nuevos, key=lambda b: b["altura"]) if nuevos else None
        registro.fila("muestras", instante=ahora().isoformat(timespec="seconds"), ciclo=ciclo, fase=nombre,
                      segundos=round(segundos, 1), gpus_vivas=gpus, cpus_vivas=cpus, vms=vms,
                      vms_corriendo=corriendo, escalador=escalador, alerta=alerta, altura=altura_vista,
                      prefijo=len(ultimo["prefijo"]) if ultimo else "")
        for evento, ocurrio in (("gpu_muerta", gpus == 0), ("gpu_viva", gpus > 0),
                                ("alerta_pendiente", alerta in ("pending", "firing")),
                                ("alerta_disparada", alerta == "firing"), ("alerta_apagada", alerta == ""),
                                ("escalador_arriba", escalador > 0), ("escalador_abajo", escalador == 0),
                                ("vm_creada", vms > 0), ("vm_corriendo", corriendo > 0), ("vm_borrada", vms == 0),
                                ("cpu_viva", cpus > 0), ("cpu_muerta", cpus == 0)):
            if ocurrio:
                marcar(evento)
        time.sleep(max(0, periodo - (time.monotonic() - inicio - segundos)))
    return altura_vista


def reposo(sistema, limite):
    """Espera GPU viva, sin VMs y el escalador en 0 (lo deja así el ciclo anterior)."""
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        mineros = sistema.mineros()
        gpus = sum(m["vivo"] and m["tipo"] == "gpu" for m in mineros)
        if gpus and sistema.vms()[0] == 0 and sistema.escalador() == 0:
            return
        time.sleep(10)
    sys.exit(f"después de {limite} s el sistema no volvió al reposo (GPU viva, sin VMs, escalador en 0)")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--api", required=True, help="URL del Ingress (frontend y API)")
    p.add_argument("--salida", required=True, help="prefijo de los tres CSV")
    p.add_argument("--ciclos", type=int, default=3)
    p.add_argument("--periodo", type=float, default=5, help="segundos entre muestras")
    p.add_argument("--limite", type=int, default=900, help="segundos máximos por fase")
    p.add_argument("--kube-gpu", default=os.path.expanduser("~/.kube/config"), help="kubeconfig del k3s")
    p.add_argument("--ns-gpu", default="mineros-gpu")
    p.add_argument("--sts", default="minero-gpu")
    p.add_argument("--proyecto", default=os.environ.get("PROYECTO"), help="proyecto de GCP de las VMs")
    args = p.parse_args()
    if not args.proyecto:
        sys.exit("falta --proyecto (o PROYECTO)")

    sistema = Sistema(args)
    replicas = sistema.replicas_gpu()
    pods = {f"{args.sts}-{n}" for n in range(replicas)}
    ajenos = [m["alias"] or m["id"][:12] for m in sistema.mineros()
              if m["vivo"] and m["alias"] not in pods and not (m["alias"] or "").startswith("minero-cpu-")]
    if not replicas or ajenos:
        sys.exit(f"tienen que estar vivos sólo los GPU de {args.sts} ({replicas} réplicas); también: {ajenos}")

    registro, envios = Registro(args.salida), Transferencias(sistema.base)
    altura = max((b["altura"] for b in sistema.bloques(1)), default=0)
    envios.start()
    try:
        for ciclo in range(1, args.ciclos + 1):
            print(f"== ciclo {ciclo}: esperando el reposo", flush=True)
            envios.activo.clear()
            reposo(sistema, args.limite)
            envios.activo.set()
            sistema.escalar_gpu(0)
            altura = fase(sistema, registro, ciclo, "salida", SALIDA, args.limite, args.periodo, altura)
            sistema.escalar_gpu(replicas)
            altura = fase(sistema, registro, ciclo, "vuelta", VUELTA, args.limite, args.periodo, altura)
            envios.activo.clear()
            altura = fase(sistema, registro, ciclo, "apagado", APAGADO, args.limite, args.periodo, altura)
    finally:
        envios.parar.set()
        print(f"== {envios.enviadas} transferencias; el StatefulSet vuelve a {replicas} réplicas", flush=True)
        sistema.escalar_gpu(replicas)
        registro.cerrar()


if __name__ == "__main__":
    main()
