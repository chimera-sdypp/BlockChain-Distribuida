"""Prueba del criterio de potencia (BC-59): con los mismos mineros, el tiempo por bloque si el TrP elige
el prefijo con la suma, el promedio, el mínimo o el máximo del bench de los mineros vivos.

El TrP ajusta el prefijo (AJUSTAR_PREFIJO=1) con CRITERIO_POTENCIA, hasta PREFIJO_BASE. Por bloque:
- el tiempo, igual que en prefijos.py (BC-39), de los logs del NCT;
- de los logs del TrP: el criterio, el prefijo y la potencia con que se eligió;
- los mineros vivos y el ganador.

Para comparar con la tabla teórica (potencia_teorica.py) y armar el gráfico (grafico_potencia.py).

    python pilar3/pruebas/potencia.py --api http://<traefik> --salida pilar3/pruebas/resultados/potencia.csv

Grupo de la prueba: 1 GPU (k3s) y 2 VMs CPU fijas (preparar.sh fijar-vms 2). Con prefijo 8 hacen falta
16^8 = 2^32 intentos: con ESPACIO_NONCES=2^34, ~1,8 % de las tareas quedan sin solución (e^-4) en vez
de ~37 %. Usa el kubectl del cluster (KUBECONFIG). Al terminar (o con Ctrl+C) deja PREFIJO_BASE,
AJUSTAR_PREFIJO, FRAGMENTO_PORCENTAJE, ESPACIO_NONCES y CRITERIO_POTENCIA del TrP como estaban.
Correrlo con el Python de pilar2 (pilar2/.venv/bin/python): firma con la cuenta de prueba alice.
"""

import argparse
import csv
import re
import sys
import time

from fragmentos import env_trp, fijar_trp, logs, nombres_mineros, tareas_del_bloque, un_bloque
from prefijos import vivos

VARIABLES = ["PREFIJO_BASE", "AJUSTAR_PREFIJO", "FRAGMENTO_PORCENTAJE", "ESPACIO_NONCES", "CRITERIO_POTENCIA"]
CRITERIOS = ["suma", "promedio", "minimo", "maximo"]
# La línea de cada tarea del TrP (trp/app.py), con el criterio y la potencia al final.
PARTIDA = re.compile(r"Tarea (\w+): \d+ transacciones, prefijo (0+), \d+ rangos del \d+ %, "
                     r"criterio (\w+), potencia ([\d,]+) (\w?H/s)")
UNIDADES = {"H/s": 1, "kH/s": 1e3, "MH/s": 1e6, "GH/s": 1e9}
COLUMNAS = ["criterio", "bloque", "prefijo", "potencia", "segundos", "rearmados", "minero",
            "gpus_vivas", "cpus_vivas", "block_hash", "publicada"]


def analizar(desde, block_hash, nombres):
    """Una fila del CSV (sin criterio ni bloque) para el bloque `block_hash`, y el criterio que aplicó el TrP."""
    del_nct = tareas_del_bloque(desde, block_hash)
    if del_nct is None:
        sys.exit(f"el bloque {block_hash} no aparece en los logs del NCT")
    tareas, primera, confirmado, minero = del_nct
    partida = None
    for _, texto in logs("trp", desde):
        if (m := PARTIDA.search(texto)) and m.group(1) == tareas[-1]:
            partida = m
    if partida is None:
        sys.exit(f"la tarea {tareas[-1]} no aparece en los logs del TrP con su criterio: ¿el TrP es de BC-59?")
    potencia = float(partida.group(4).replace(",", ".")) * UNIDADES[partida.group(5)]
    return {"prefijo": len(partida.group(2)), "potencia": round(potencia),
            "segundos": round((confirmado - primera).total_seconds(), 3), "rearmados": len(tareas) - 1,
            "minero": nombres.get(minero, minero[:8]), "block_hash": block_hash,
            "publicada": primera.isoformat(timespec="milliseconds")}, partida.group(3)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--api", required=True, help="URL de Traefik (frontend y API)")
    p.add_argument("--salida", required=True, help="CSV de resultados (se agregan filas)")
    p.add_argument("--criterios", default=",".join(CRITERIOS), help="CRITERIO_POTENCIA a probar, separados por coma")
    p.add_argument("--tope", type=int, default=8, help="PREFIJO_BASE: el máximo que puede elegir el TrP")
    p.add_argument("--espacio", type=int, default=2**34, help="ESPACIO_NONCES del TrP")
    p.add_argument("--porcentaje", type=int, default=10, help="FRAGMENTO_PORCENTAJE del TrP")
    p.add_argument("--bloques", type=int, default=5, help="bloques por criterio")
    p.add_argument("--maximo", type=int, default=300, help="segundos máximos por bloque; si se pasa, sigue con el próximo criterio")
    p.add_argument("--ventana", type=int, default=30, help="VENTANA_SEGUNDOS del NCT")
    p.add_argument("--gpus", type=int, default=1, help="mineros GPU que tienen que estar vivos")
    p.add_argument("--cpus", type=int, default=2, help="mineros CPU que tienen que estar vivos")
    args = p.parse_args()
    criterios = args.criterios.split(",")
    if not set(criterios) <= set(CRITERIOS):
        sys.exit(f"los criterios son {', '.join(CRITERIOS)}")
    base = args.api.rstrip("/")

    gpus, cpus = vivos(base)
    if (gpus, cpus) != (args.gpus, args.cpus):
        sys.exit(f"tienen que estar vivos {args.gpus} GPU y {args.cpus} CPU: hay {gpus} GPU y {cpus} CPU")

    originales = {nombre: env_trp(nombre) for nombre in VARIABLES}
    print(f"== TrP antes de la prueba: {originales}", flush=True)

    with open(args.salida, "a", newline="") as archivo:
        escritor = csv.DictWriter(archivo, COLUMNAS)
        if archivo.tell() == 0:
            escritor.writeheader()
        try:
            for criterio in criterios:
                print(f"== criterio {criterio}, tope {args.tope}: fijando en el TrP…", flush=True)
                fijar_trp({"PREFIJO_BASE": args.tope, "AJUSTAR_PREFIJO": 1, "FRAGMENTO_PORCENTAJE": args.porcentaje,
                           "ESPACIO_NONCES": args.espacio, "CRITERIO_POTENCIA": criterio})
                for numero in range(1, args.bloques + 1):
                    fila = {"criterio": criterio, "bloque": numero}
                    resultado = un_bloque(base, args.maximo, args.ventana)
                    if resultado is None:
                        print(f"   bloque {numero}: no salió en {args.maximo} s; sigo con el próximo criterio", flush=True)
                        escritor.writerow({**fila, "minero": f"sin bloque en {args.maximo} s"})
                        archivo.flush()
                        break
                    desde, block_hash = resultado
                    time.sleep(2)  # que el log del TrP y el del NCT ya estén
                    medido, aplicado = analizar(desde, block_hash, nombres_mineros(base))
                    if aplicado != criterio:
                        sys.exit(f"el TrP usó el criterio {aplicado}, no {criterio}: ¿tomó el cambio?")
                    gpus, cpus = vivos(base)
                    escritor.writerow({**fila, **medido, "gpus_vivas": gpus, "cpus_vivas": cpus})
                    archivo.flush()
                    print(f"   bloque {numero}: prefijo {medido['prefijo']}, {medido['segundos']:7.2f} s "
                          f"({medido['potencia'] / 1e6:,.1f} MH/s según {criterio}; ganó {medido['minero']})", flush=True)
        finally:
            print(f"== devuelvo el TrP a {originales}", flush=True)
            fijar_trp(originales)


if __name__ == "__main__":
    main()
