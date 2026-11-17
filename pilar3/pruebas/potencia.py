"""Prueba del criterio de potencia (BC-59): con los mismos mineros, el tiempo por bloque si el TrP elige
el prefijo con la suma, el promedio, el mínimo o el máximo del bench de los mineros vivos.

Dos modos:
- Normal: el TrP de BC-59 ajusta el prefijo (AJUSTAR_PREFIJO=1) con CRITERIO_POTENCIA, hasta
  PREFIJO_BASE, y el criterio, el prefijo y la potencia salen de su log.
- --emular: para un TrP sin CRITERIO_POTENCIA (el de main). Lee el bench de los mineros vivos de
  /api/mineros, calcula con trp/particion.py el prefijo que elegiría cada criterio y lo fija
  (PREFIJO_BASE=p, AJUSTAR_PREFIJO=0). Mide lo mismo: cuánto tarda el bloque con ese prefijo, minando todos.

Por bloque, el tiempo sale de los logs del NCT, igual que en prefijos.py (BC-39). Para comparar con la
tabla teórica (potencia_teorica.py) y armar el gráfico (grafico_potencia.py).

    python pilar3/pruebas/potencia.py --api http://<traefik> --salida pilar3/pruebas/resultados/potencia.csv
    python pilar3/pruebas/potencia.py --api http://<traefik> --salida … --emular

Grupo de la prueba: 3 GPU de potencias distintas (la RTX 4060 y 2 T4 de Colab) y 2 mineros CPU. Así los
criterios se separan: la suma da 9, el máximo y el promedio 8, y el mínimo (una CPU) 6. Con prefijo 9
hacen falta 16^9 = 2^36 intentos: con ESPACIO_NONCES=2^38, ~1,8 % de las tareas quedan sin solución
(e^-4) en vez de ~37 %. Usa el kubectl del cluster (KUBECONFIG). Al terminar (o con Ctrl+C) deja las
variables del TrP como estaban. Correrlo con el Python de pilar2 (pilar2/.venv/bin/python): firma con
la cuenta de prueba alice.
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path

from fragmentos import env_trp, fijar_trp, logs, nombres_mineros, tareas_del_bloque, un_bloque
from prefijos import api, vivos

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pilar2"))

from trp.particion import largo_prefijo, potencia_viva  # noqa: E402

VARIABLES = ["PREFIJO_BASE", "AJUSTAR_PREFIJO", "FRAGMENTO_PORCENTAJE", "ESPACIO_NONCES", "CRITERIO_POTENCIA"]
VARIABLES_EMULAR = VARIABLES[:-1]  # el TrP de main no tiene CRITERIO_POTENCIA
CRITERIOS = ["suma", "promedio", "minimo", "maximo"]
# La línea de cada tarea del TrP de BC-59 (trp/app.py), con el criterio y la potencia al final.
PARTIDA = re.compile(r"Tarea (\w+): \d+ transacciones, prefijo (0+), \d+ rangos del \d+ %, "
                     r"criterio (\w+), potencia ([\d,]+) (\w?H/s)")
UNIDADES = {"H/s": 1, "kH/s": 1e3, "MH/s": 1e6, "GH/s": 1e9}
COLUMNAS = ["criterio", "bloque", "prefijo", "potencia", "segundos", "rearmados", "minero",
            "gpus_vivas", "cpus_vivas", "block_hash", "publicada", "modo"]


def del_nct(desde, block_hash, nombres):
    """Lo que sale de los logs del NCT: segundos, rearmados, ganador; y el id de la última tarea."""
    datos = tareas_del_bloque(desde, block_hash)
    if datos is None:
        sys.exit(f"el bloque {block_hash} no aparece en los logs del NCT")
    tareas, primera, confirmado, minero = datos
    return {"segundos": round((confirmado - primera).total_seconds(), 3), "rearmados": len(tareas) - 1,
            "minero": nombres.get(minero, minero[:8]), "block_hash": block_hash,
            "publicada": primera.isoformat(timespec="milliseconds")}, tareas[-1]


def analizar(desde, block_hash, nombres):
    """Modo normal: la fila (sin criterio ni bloque) y el criterio que aplicó el TrP, de su log."""
    fila, tarea = del_nct(desde, block_hash, nombres)
    partida = None
    for _, texto in logs("trp", desde):
        if (m := PARTIDA.search(texto)) and m.group(1) == tarea:
            partida = m
    if partida is None:
        sys.exit(f"la tarea {tarea} no aparece en los logs del TrP con su criterio: ¿el TrP es de BC-59? "
                 "Si no, correr con --emular")
    potencia = float(partida.group(4).replace(",", ".")) * UNIDADES[partida.group(5)]
    return {**fila, "prefijo": len(partida.group(2)), "potencia": round(potencia)}, partida.group(3)


def prefijos_emulados(base, segundos, tope, vivo):
    """{criterio: (prefijo, potencia)} con el bench de los mineros vivos, como lo haría el TrP de BC-59."""
    ahora = time.time()
    nodos = [{"bench": str(m.get("bench") or 0), "ultimo_aviso": str(m.get("ultimo_aviso") or 0)}
             for m in api(base, "/api/mineros") if m.get("vivo")]
    resultado = {}
    for criterio in CRITERIOS:
        potencia = potencia_viva(nodos, ahora, vivo, criterio)
        resultado[criterio] = (largo_prefijo(potencia, segundos, tope), potencia)
    return resultado


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--api", required=True, help="URL de Traefik (frontend y API)")
    p.add_argument("--salida", required=True, help="CSV de resultados (se agregan filas)")
    p.add_argument("--criterios", default=",".join(CRITERIOS), help="CRITERIO_POTENCIA a probar, separados por coma")
    p.add_argument("--emular", action="store_true",
                   help="para un TrP sin CRITERIO_POTENCIA: calcula el prefijo de cada criterio y lo fija")
    p.add_argument("--tope", type=int, default=9, help="PREFIJO_BASE: el máximo que puede elegir el TrP")
    p.add_argument("--espacio", type=int, default=2**38, help="ESPACIO_NONCES del TrP")
    p.add_argument("--porcentaje", type=int, default=10, help="FRAGMENTO_PORCENTAJE del TrP")
    p.add_argument("--bloques", type=int, default=5, help="bloques por criterio")
    p.add_argument("--maximo", type=int, default=300, help="segundos máximos por bloque; si se pasa, sigue con el próximo criterio")
    p.add_argument("--ventana", type=int, default=30, help="VENTANA_SEGUNDOS del NCT")
    p.add_argument("--vivo", type=int, default=15, help="VIVO_SEGUNDOS del TrP (con --emular)")
    p.add_argument("--gpus", type=int, default=3, help="mineros GPU que tienen que estar vivos")
    p.add_argument("--cpus", type=int, default=2, help="mineros CPU que tienen que estar vivos")
    args = p.parse_args()
    criterios = args.criterios.split(",")
    if not set(criterios) <= set(CRITERIOS):
        sys.exit(f"los criterios son {', '.join(CRITERIOS)}")
    base = args.api.rstrip("/")

    gpus, cpus = vivos(base)
    if (gpus, cpus) != (args.gpus, args.cpus):
        sys.exit(f"tienen que estar vivos {args.gpus} GPU y {args.cpus} CPU: hay {gpus} GPU y {cpus} CPU")

    variables = VARIABLES_EMULAR if args.emular else VARIABLES
    originales = {nombre: env_trp(nombre) for nombre in variables}
    print(f"== TrP antes de la prueba: {originales}", flush=True)
    emulados = None
    if args.emular:
        segundos = int(env_trp("SEGUNDOS_POR_BLOQUE") or 60)
        emulados = prefijos_emulados(base, segundos, args.tope, args.vivo)
        print(f"== emulando con {segundos} s por bloque: " + ", ".join(
            f"{c} → prefijo {pre} ({pot / 1e6:,.1f} MH/s)" for c, (pre, pot) in emulados.items()), flush=True)

    with open(args.salida, "a", newline="") as archivo:
        escritor = csv.DictWriter(archivo, COLUMNAS)
        if archivo.tell() == 0:
            escritor.writeheader()
        try:
            for criterio in criterios:
                comunes = {"FRAGMENTO_PORCENTAJE": args.porcentaje, "ESPACIO_NONCES": args.espacio}
                if args.emular:
                    prefijo, potencia = emulados[criterio]
                    print(f"== criterio {criterio} (emulado): prefijo fijo de {prefijo}…", flush=True)
                    fijar_trp({**comunes, "PREFIJO_BASE": prefijo, "AJUSTAR_PREFIJO": 0})
                else:
                    print(f"== criterio {criterio}, tope {args.tope}: fijando en el TrP…", flush=True)
                    fijar_trp({**comunes, "PREFIJO_BASE": args.tope, "AJUSTAR_PREFIJO": 1, "CRITERIO_POTENCIA": criterio})
                for numero in range(1, args.bloques + 1):
                    fila = {"criterio": criterio, "bloque": numero, "modo": "emulado" if args.emular else "trp"}
                    resultado = un_bloque(base, args.maximo, args.ventana)
                    if resultado is None:
                        print(f"   bloque {numero}: no salió en {args.maximo} s; sigo con el próximo criterio", flush=True)
                        escritor.writerow({**fila, "minero": f"sin bloque en {args.maximo} s"})
                        archivo.flush()
                        break
                    desde, block_hash = resultado
                    time.sleep(2)  # que el log del TrP y el del NCT ya estén
                    if args.emular:
                        if not block_hash.startswith("0" * prefijo):
                            sys.exit(f"el bloque {block_hash} no tiene el prefijo de {prefijo}: ¿el TrP tomó el cambio?")
                        medido, _ = del_nct(desde, block_hash, nombres_mineros(base))
                        medido.update(prefijo=prefijo, potencia=round(potencia))
                    else:
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
