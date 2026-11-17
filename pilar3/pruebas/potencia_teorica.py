"""Tabla teórica del criterio de potencia (BC-59): qué prefijo da cada criterio y cuánto tardaría el bloque.

El TrP elige el largo p del prefijo con la potencia de los mineros vivos juntada según CRITERIO_POTENCIA
(trp/particion.py). Pero el criterio sólo elige p: minan TODOS, así que el bloque tarda en promedio
16^p intentos dividido la potencia TOTAL. Esta tabla lo calcula para varios grupos, sin cluster, con
la misma función que usa el TrP.

    python pilar3/pruebas/potencia_teorica.py   # escribe resultados/potencia-teorica.csv
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pilar2"))

from trp.particion import CRITERIOS, largo_prefijo, potencia_viva  # noqa: E402

RESULTADOS = Path(__file__).parent / "resultados"
SEGUNDOS_OBJETIVO = 30  # SEGUNDOS_POR_BLOQUE de producción
# PREFIJO_BASE: 8 es el tope de la prueba en GKE; con 32 (el máximo) se ve el criterio sin el tope.
TOPES = (8, 32)
# Bench medidos en BC-39: la RTX 4060 del k3s y una VM e2-highcpu-2.
GPU, CPU = 1.78e9, 1.44e6
GRUPOS = [("1 CPU", 0, 1), ("3 CPU", 0, 3), ("1 GPU", 1, 0), ("1 GPU y 2 CPU", 1, 2),
          ("1 GPU y 15 CPU", 1, 15), ("2 GPU", 2, 0)]
COLUMNAS = ["tope", "grupo", "gpus", "cpus", "criterio", "potencia", "prefijo", "potencia_total", "segundos"]


def nodos(gpus, cpus):
    """Los nodo:<id> como los ve el TrP: todos vivos (avisaron en el mismo instante que se mira)."""
    return [{"bench": str(GPU), "ultimo_aviso": "0"}] * gpus + [{"bench": str(CPU), "ultimo_aviso": "0"}] * cpus


def filas():
    for tope in TOPES:
        for nombre, gpus, cpus in GRUPOS:
            yield from filas_del_grupo(tope, nombre, gpus, cpus)


def filas_del_grupo(tope, nombre, gpus, cpus):
    grupo = nodos(gpus, cpus)
    total = potencia_viva(grupo, 0, 15, "suma")
    for criterio in CRITERIOS:
        potencia = potencia_viva(grupo, 0, 15, criterio)
        prefijo = largo_prefijo(potencia, SEGUNDOS_OBJETIVO, tope)
        yield {"tope": tope, "grupo": nombre, "gpus": gpus, "cpus": cpus, "criterio": criterio,
               "potencia": round(potencia), "prefijo": prefijo, "potencia_total": round(total),
               "segundos": round(16**prefijo / total, 3)}


def main():
    RESULTADOS.mkdir(exist_ok=True)
    salida = RESULTADOS / "potencia-teorica.csv"
    with open(salida, "w", newline="") as archivo:
        escritor = csv.DictWriter(archivo, COLUMNAS)
        escritor.writeheader()
        for fila in filas():
            escritor.writerow(fila)
            print(f"tope {fila['tope']:2}  {fila['grupo']:16} {fila['criterio']:9} prefijo {fila['prefijo']}  {fila['segundos']:>10} s")
    print(f"→ {salida}")


if __name__ == "__main__":
    main()
