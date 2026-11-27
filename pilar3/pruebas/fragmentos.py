"""Prueba de fragmentación (BC-40): fija el % del TrP en 1, 5, 10, 25 y 50 y mide cada bloque.

Por bloque, con el mismo prefijo y los mismos mineros:
- el tiempo, igual que en prefijos.py (BC-39): desde que el NCT publica la tarea hasta que confirma
  el bloque, sacado de los logs del NCT, y desde la primera publicación si la tarea se rearmó;
- de los logs del TrP: en cuántos rangos partió la tarea y qué nonce ganó (de ahí, en qué rango cayó);
- de los logs del gateway: los rangos prestados (en total y hasta que llegó el ganador), cuántos se
  cortaron a medio minar, cuántos se descartaron sin prestar, cuántos vencieron y cuántos tuvo cada minero.

`prestados_hasta_ganar - 1` es el trabajo que no sirvió: rangos que algún minero tenía mientras otro
encontraba la solución. Es por rango, no por hash: de un rango cortado no se sabe cuánto se llegó a minar.

    python pilar3/pruebas/fragmentos.py --api http://<traefik> --salida resultados/fragmentos-gpu.csv --prefijo 7
    python pilar3/pruebas/fragmentos.py --api http://<traefik> --salida resultados/fragmentos-cpu.csv --tipo cpu

Usa el `kubectl` del cluster (KUBECONFIG). Al terminar (o con Ctrl+C) deja PREFIJO_BASE, AJUSTAR_PREFIJO,
FRAGMENTO_PORCENTAJE y ESPACIO_NONCES del TrP como estaban. Correrlo con el Python de pilar2
(pilar2/.venv/bin/python): firma las transacciones con la cuenta de prueba alice (firmar.py).
"""

import argparse
import csv
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone

from firmar import CUENTA, PRIVADA, contrato
from prefijos import CONFIRMADO, PUBLICADA, api, gateway_url, kubectl, lista_mineros, punta, vivos

VARIABLES = ["PREFIJO_BASE", "AJUSTAR_PREFIJO", "FRAGMENTO_PORCENTAJE", "ESPACIO_NONCES"]
PARTIDA = re.compile(r"Tarea (\w+): \d+ transacciones, prefijo (0+), (\d+) rangos del (\d+) %")
GANO = re.compile(r"Tarea (\w+): ganó (\S+) con el nonce (\d+)")
PRESTADO = re.compile(r"Rango (\w+):(\d+) prestado a (\S+)")
CORTADO = re.compile(r"A (\S+): cortá el rango (\w+):(\d+)")
DESCARTADO = re.compile(r"Descarto el rango (\d+) de la tarea (\w+), que ya se cerró")
VENCIDO = re.compile(r"El préstamo (\w+):(\d+) de (\S+) venció")
COLUMNAS = ["porcentaje", "prefijo", "bloque", "segundos", "rearmados", "rangos", "prestados",
            "prestados_hasta_ganar", "rango_ganador", "cortados", "descartados", "vencidos",
            "mineros_con_rango", "por_minero", "minero", "gpus_vivas", "cpus_vivas", "block_hash", "publicada"]


def env_trp(nombre):
    return kubectl("get", "deploy/trp", "-o",
                   f"jsonpath={{.spec.template.spec.containers[?(@.name=='trp')].env[?(@.name=='{nombre}')].value}}")


def fijar_trp(valores):
    """{variable: valor}; un valor vacío o None saca la variable (vuelve al defecto del TrP)."""
    kubectl("set", "env", "deploy/trp", *(f"{k}-" if v in ("", None) else f"{k}={v}" for k, v in valores.items()))
    kubectl("rollout", "status", "deploy/trp", "--timeout=300s")


def logs(app, desde):
    """Líneas (instante, texto) de todas las réplicas de `app` desde `desde`, en orden."""
    salida = kubectl("logs", "-l", f"app={app}", "--timestamps", "--tail=-1", "--max-log-requests=6",
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


def tareas_del_bloque(desde, block_hash):
    """De los logs del NCT: ([ids de las tareas publicadas, en orden], instante de la primera, minero)."""
    tareas, primera = [], None
    for instante, texto in logs("nct", desde):
        if publicada := PUBLICADA.search(texto):
            tareas.append(publicada.group(1))
            primera = primera or instante
        confirmado = CONFIRMADO.search(texto)
        if confirmado and confirmado.group(2) == block_hash and tareas:
            return tareas, primera, instante, confirmado.group(3)
    return None


def analizar(desde, block_hash, espacio, nombres):
    """Una fila del CSV (sin porcentaje, prefijo ni bloque) para el bloque `block_hash`."""
    del_nct = tareas_del_bloque(desde, block_hash)
    if del_nct is None:
        sys.exit(f"el bloque {block_hash} no aparece en los logs del NCT")
    tareas, primera, confirmado, minero = del_nct
    final, ids = tareas[-1], set(tareas)

    rangos = porcentaje = ganador = nonce = None
    for instante, texto in logs("trp", desde):
        if (partida := PARTIDA.search(texto)) and partida.group(1) == final:
            rangos, porcentaje = int(partida.group(3)), int(partida.group(4))
        if (gano := GANO.search(texto)) and gano.group(1) == final and ganador is None:
            ganador, nonce = instante, int(gano.group(3))
    if rangos is None or nonce is None:
        sys.exit(f"la tarea {final} (bloque {block_hash}) no aparece partida o ganada en los logs del TrP")
    tamano = -(-espacio * porcentaje // 100)  # como trp/particion.py

    prestados, hasta_ganar, por_minero = 0, 0, Counter()
    cortados = descartados = vencidos = 0
    for instante, texto in logs("gateway", desde):
        if (m := PRESTADO.search(texto)) and m.group(1) in ids:
            prestados += 1
            hasta_ganar += instante <= ganador
            por_minero[nombres.get(m.group(3), m.group(3)[:8])] += 1
        elif (m := CORTADO.search(texto)) and m.group(2) in ids:
            cortados += 1
        elif (m := DESCARTADO.search(texto)) and m.group(2) in ids:
            descartados += 1
        elif (m := VENCIDO.search(texto)) and m.group(1) in ids:
            vencidos += 1

    return {"segundos": round((confirmado - primera).total_seconds(), 3), "rearmados": len(tareas) - 1,
            "rangos": rangos, "prestados": prestados, "prestados_hasta_ganar": hasta_ganar,
            "rango_ganador": nonce // tamano, "cortados": cortados, "descartados": descartados,
            "vencidos": vencidos, "mineros_con_rango": len(por_minero),
            "por_minero": "|".join(f"{n}:{c}" for n, c in sorted(por_minero.items())),
            "minero": nombres.get(minero, minero[:8]), "block_hash": block_hash,
            "publicada": primera.isoformat(timespec="milliseconds")}, porcentaje


def un_bloque(base, maximo, ventana):
    """Manda una transacción y espera su bloque: (desde, block_hash), o None si no salió a tiempo."""
    antes, desde = punta(base), datetime.now(timezone.utc).replace(microsecond=0)
    time.sleep(1)  # que el --since-time no corte la primera línea
    nonce = api(base, f"/api/cuentas/{CUENTA['alice']}")["nonce"] + 1
    api(base, "/api/transacciones", contrato.transaccion(PRIVADA["alice"], CUENTA["bob"], 1, nonce,
                                                         cadena=api(base, "/api/cadena")["genesis"]))
    limite = time.time() + ventana + maximo + 15
    while time.time() < limite:
        time.sleep(1)
        nueva = punta(base)
        if nueva != antes:
            return desde, nueva
    return None


def nombres_mineros(gateway):
    return {m["id"]: m.get("alias") or m["id"][:8] for m in lista_mineros(gateway)}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--api", required=True, help="URL de Traefik (frontend y API)")
    p.add_argument("--gateway", help="URL del gateway, que lista los mineros (GET /mineros); por defecto GATEWAY_URL")
    p.add_argument("--salida", required=True, help="CSV de resultados (se agregan filas)")
    p.add_argument("--porcentajes", default="1,5,10,25,50", help="FRAGMENTO_PORCENTAJE a probar, separados por coma")
    p.add_argument("--prefijo", type=int, default=6, help="PREFIJO_BASE fijo (sin ajuste) para todas las corridas")
    p.add_argument("--espacio", type=int, help="ESPACIO_NONCES del TrP (por defecto, el que tenga: 2^32). "
                                               "Ojo: sin solución en un e^(-espacio/16^prefijo) de las tareas")
    p.add_argument("--bloques", type=int, default=5, help="bloques por porcentaje")
    p.add_argument("--maximo", type=int, default=300, help="segundos máximos por bloque; si se pasa, sigue con el próximo %%")
    p.add_argument("--ventana", type=int, default=30, help="VENTANA_SEGUNDOS del NCT")
    p.add_argument("--espera-logs", type=float, default=8,
                   help="segundos después del bloque para que el gateway corte los rangos (keep-alive de 5 s)")
    p.add_argument("--tipo", choices=["gpu", "cpu"], default="gpu", help="los mineros que tienen que estar vivos (y sólo esos)")
    args = p.parse_args()
    porcentajes = [int(x) for x in args.porcentajes.split(",")]
    if not all(1 <= x <= 50 for x in porcentajes):
        sys.exit("los porcentajes van de 1 a 50")
    base = args.api.rstrip("/")
    gateway = gateway_url(args.gateway)

    gpus, cpus = vivos(gateway)
    if (args.tipo == "gpu" and (not gpus or cpus)) or (args.tipo == "cpu" and (not cpus or gpus)):
        sys.exit(f"para la corrida {args.tipo} tienen que estar vivos sólo mineros {args.tipo}: hay {gpus} GPU y {cpus} CPU")

    originales = {nombre: env_trp(nombre) for nombre in VARIABLES}
    print(f"== TrP antes de la prueba: {originales}", flush=True)
    espacio = args.espacio or int(originales["ESPACIO_NONCES"] or 2**32)

    with open(args.salida, "a", newline="") as archivo:
        escritor = csv.DictWriter(archivo, COLUMNAS)
        if archivo.tell() == 0:
            escritor.writeheader()
        try:
            for porcentaje in porcentajes:
                print(f"== {porcentaje} % con prefijo de {args.prefijo}: fijando en el TrP…", flush=True)
                fijar_trp({"PREFIJO_BASE": args.prefijo, "AJUSTAR_PREFIJO": 0, "FRAGMENTO_PORCENTAJE": porcentaje,
                           "ESPACIO_NONCES": args.espacio or originales["ESPACIO_NONCES"]})
                for numero in range(1, args.bloques + 1):
                    fila = {"porcentaje": porcentaje, "prefijo": args.prefijo, "bloque": numero}
                    resultado = un_bloque(base, args.maximo, args.ventana)
                    if resultado is None:
                        print(f"   bloque {numero}: no salió en {args.maximo} s; sigo con el próximo %", flush=True)
                        escritor.writerow({**fila, "minero": f"sin bloque en {args.maximo} s"})
                        archivo.flush()
                        break
                    desde, block_hash = resultado
                    if not block_hash.startswith("0" * args.prefijo):
                        sys.exit(f"el bloque {block_hash} no tiene el prefijo de {args.prefijo}: ¿el TrP tomó el cambio?")
                    time.sleep(args.espera_logs)
                    medido, aplicado = analizar(desde, block_hash, espacio, nombres_mineros(gateway))
                    if aplicado != porcentaje:
                        sys.exit(f"el TrP partió la tarea al {aplicado} %, no al {porcentaje} %: ¿tomó el cambio?")
                    gpus, cpus = vivos(gateway)
                    escritor.writerow({**fila, **medido, "gpus_vivas": gpus, "cpus_vivas": cpus})
                    archivo.flush()
                    print(f"   bloque {numero}: {medido['segundos']:7.2f} s, {medido['rangos']} rangos, "
                          f"{medido['prestados_hasta_ganar']} prestados hasta ganar (ganó el rango "
                          f"{medido['rango_ganador']}), {medido['por_minero']}", flush=True)
        finally:
            print(f"== devuelvo el TrP a {originales}", flush=True)
            fijar_trp(originales)


if __name__ == "__main__":
    main()
