"""Prueba de prefijos (BC-39): fija el prefijo del TrP en 1, 2, … y mide el tiempo por bloque.

Tiempo por bloque: desde que el NCT publica la tarea hasta que confirma el bloque (los mismos dos
puntos que `blockchain_tiempo_bloque_segundos`), sacado de los logs del NCT. Si la tarea vuelve sin
solución (con 8 caracteres pasa en ~37 % de los bloques: 16^8 = 2^32 = ESPACIO_NONCES) y el NCT la
rearma, cuenta desde la primera publicación. Por bloque manda una transacción y espera la ventana.

    python pilar3/pruebas/prefijos.py --api http://<traefik> --salida gpu.csv
    python pilar3/pruebas/prefijos.py --api http://<traefik> --salida cpu.csv --tipo cpu --maximo 600

Sólo biblioteca estándar; usa el `kubectl` del cluster (KUBECONFIG) para cambiar el prefijo y leer
logs. Al terminar (o con Ctrl+C) deja el TrP como estaba: PREFIJO_BASE=6 y el ajuste prendido.
"""

import argparse
import csv
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

NS = "blockchain"
PUBLICADA = re.compile(r"Tarea (\w+) publicada")
CONFIRMADO = re.compile(r"Bloque (\d+) confirmado: (\w+) \(minó (\S+),")
COLUMNAS = ["prefijo", "bloque", "segundos", "rearmados", "minero", "gpus_vivas", "cpus_vivas", "block_hash", "publicada"]


def api(base, ruta, cuerpo=None, intentos=5):
    """Reintenta los errores de paso (un 502 de Traefik, un timeout): la prueba dura media hora."""
    datos = None if cuerpo is None else json.dumps(cuerpo).encode()
    pedido = urllib.request.Request(base + ruta, data=datos, headers={"Content-Type": "application/json"})
    for intento in range(intentos):
        try:
            with urllib.request.urlopen(pedido, timeout=15) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code < 500 or intento == intentos - 1:
                raise
            print(f"   {ruta}: {error}; reintento", flush=True)
            time.sleep(2)


def kubectl(*args):
    return subprocess.run(["kubectl", "-n", NS, *args], check=True, capture_output=True, text=True).stdout


def fijar_prefijo(largo):
    """None: vuelve al prefijo del sistema (6, ajustado a la potencia viva)."""
    if largo is None:
        kubectl("set", "env", "deploy/trp", "PREFIJO_BASE=6", "AJUSTAR_PREFIJO-")
    else:
        kubectl("set", "env", "deploy/trp", f"PREFIJO_BASE={largo}", "AJUSTAR_PREFIJO=0")
    kubectl("rollout", "status", "deploy/trp", "--timeout=300s")


def punta(base):
    bloques = api(base, "/api/bloques?limite=1")
    return bloques[0]["block_hash"] if bloques else None


def vivos(base):
    mineros = api(base, "/api/mineros")
    return (sum(m["vivo"] and m["tipo"] == "gpu" for m in mineros),
            sum(m["vivo"] and m["tipo"] == "cpu" for m in mineros))


def logs_nct(desde):
    """Líneas (instante, texto) de las dos réplicas del NCT desde `desde`, en orden."""
    salida = kubectl("logs", "-l", "app=nct", "--timestamps", "--tail=-1", "--max-log-requests=4",
                     "--since-time=" + desde.strftime("%Y-%m-%dT%H:%M:%SZ"))
    lineas = []
    for linea in salida.splitlines():
        marca, _, texto = linea.partition(" ")
        try:
            instante = datetime.fromisoformat(marca[:26].rstrip("Z") + "+00:00")
        except ValueError:
            continue
        lineas.append((instante, texto))
    return sorted(lineas)


def medir(desde, block_hash):
    """De los logs: (segundos, rearmados, minero, primera publicación) del bloque `block_hash`."""
    publicadas, lineas = [], logs_nct(desde)
    for instante, texto in lineas:
        if PUBLICADA.search(texto):
            publicadas.append(instante)
        confirmado = CONFIRMADO.search(texto)
        if confirmado and confirmado.group(2) == block_hash and publicadas:
            return (instante - publicadas[0]).total_seconds(), len(publicadas) - 1, confirmado.group(3), publicadas[0]
    return None


def un_bloque(base, largo, maximo, ventana):
    """Manda una transacción y espera su bloque. None si no salió en `maximo` s desde la publicación."""
    antes, desde = punta(base), datetime.now(timezone.utc).replace(microsecond=0)
    time.sleep(1)  # que el --since-time no corte la primera línea
    api(base, "/api/transacciones", {"origen": "alice", "destino": "bob", "monto": 1})
    limite = time.time() + ventana + maximo + 15
    while time.time() < limite:
        time.sleep(1)
        nueva = punta(base)
        if nueva != antes:
            medido = medir(desde, nueva)
            if medido is None:
                sys.exit(f"el bloque {nueva} no aparece en los logs del NCT")
            if not nueva.startswith("0" * largo):
                sys.exit(f"el bloque {nueva} no tiene el prefijo de {largo}: ¿el TrP tomó el cambio?")
            return nueva, medido
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--api", required=True, help="URL de Traefik (frontend y API)")
    p.add_argument("--salida", required=True, help="CSV de resultados (se agregan filas)")
    p.add_argument("--prefijos", default="1-8", help="rango de largos, p. ej. 1-8 o 6-8")
    p.add_argument("--bloques", type=int, default=5, help="bloques por largo")
    p.add_argument("--maximo", type=int, default=300, help="segundos máximos por bloque; si se pasa, no sigue con más largos")
    p.add_argument("--ventana", type=int, default=30, help="VENTANA_SEGUNDOS del NCT")
    p.add_argument("--tipo", choices=["gpu", "cpu"], default="gpu", help="los mineros que tienen que estar vivos (y sólo esos)")
    args = p.parse_args()
    desde_largo, _, hasta_largo = args.prefijos.partition("-")
    largos = range(int(desde_largo), int(hasta_largo or desde_largo) + 1)
    base = args.api.rstrip("/")

    gpus, cpus = vivos(base)
    if (args.tipo == "gpu" and (not gpus or cpus)) or (args.tipo == "cpu" and (not cpus or gpus)):
        sys.exit(f"para la corrida {args.tipo} tienen que estar vivos sólo mineros {args.tipo}: hay {gpus} GPU y {cpus} CPU")

    with open(args.salida, "a", newline="") as archivo:
        escritor = csv.DictWriter(archivo, COLUMNAS)
        if archivo.tell() == 0:
            escritor.writeheader()
        try:
            for largo in largos:
                print(f"== prefijo de {largo}: fijando en el TrP…", flush=True)
                fijar_prefijo(largo)
                for numero in range(1, args.bloques + 1):
                    resultado = un_bloque(base, largo, args.maximo, args.ventana)
                    if resultado is None:
                        print(f"   bloque {numero}: no salió en {args.maximo} s; corto acá", flush=True)
                        escritor.writerow({"prefijo": largo, "bloque": numero, "segundos": "", "rearmados": "",
                                           "minero": f"sin bloque en {args.maximo} s"})
                        return
                    block_hash, (segundos, rearmados, minero, publicada) = resultado
                    gpus, cpus = vivos(base)
                    escritor.writerow({"prefijo": largo, "bloque": numero, "segundos": round(segundos, 3),
                                       "rearmados": rearmados, "minero": minero, "gpus_vivas": gpus,
                                       "cpus_vivas": cpus, "block_hash": block_hash,
                                       "publicada": publicada.isoformat(timespec="milliseconds")})
                    archivo.flush()
                    print(f"   bloque {numero}: {segundos:7.2f} s, {rearmados} rearmados, {minero}", flush=True)
        finally:
            print("== devuelvo el TrP a PREFIJO_BASE=6 con ajuste", flush=True)
            fijar_prefijo(None)


if __name__ == "__main__":
    main()
