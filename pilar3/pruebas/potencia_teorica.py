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
# PREFIJO_BASE: 9 es el tope de la prueba en GKE; con 32 (el máximo) se ve el criterio sin el tope.
TOPES = (9, 32)
# Bench medidos en BC-39 (la RTX 4060 del k3s y una VM e2-highcpu-2) y la T4 de Colab estimada con el
# kernel del Hit 7 (cierre del Pilar 1, 700 MH/s; el minero con midstate hace algo más).
RTX, T4, CPU = 1.78e9, 7e8, 1.44e6
GRUPOS = [  # (nombre, [bench de cada minero])
    ("1 CPU", [CPU]), ("3 CPU", [CPU] * 3), ("1 GPU", [RTX]), ("1 GPU y 2 CPU", [RTX] + [CPU] * 2),
    ("1 GPU y 15 CPU", [RTX] + [CPU] * 15), ("2 GPU", [RTX] * 2),
    ("RTX, 2 T4 y 2 CPU", [RTX] + [T4] * 2 + [CPU] * 2),  # el grupo de la prueba en GKE
]
COLUMNAS = ["tope", "grupo", "mineros", "criterio", "potencia", "prefijo", "potencia_total", "segundos"]


def nodos(benchs):
    """Los nodo:<id> como los ve el TrP: todos vivos (avisaron en el mismo instante que se mira)."""
    return [{"bench": str(bench), "ultimo_aviso": "0"} for bench in benchs]


def filas():
    for tope in TOPES:
        for nombre, benchs in GRUPOS:
            yield from filas_del_grupo(tope, nombre, benchs)


def filas_del_grupo(tope, nombre, benchs):
    grupo = nodos(benchs)
    total = potencia_viva(grupo, 0, 15, "suma")
    for criterio in CRITERIOS:
        potencia = potencia_viva(grupo, 0, 15, criterio)
        prefijo = largo_prefijo(potencia, SEGUNDOS_OBJETIVO, tope)
        yield {"tope": tope, "grupo": nombre, "mineros": len(benchs), "criterio": criterio,
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
            print(f"tope {fila['tope']:2}  {fila['grupo']:18} {fila['criterio']:9} prefijo {fila['prefijo']}  {fila['segundos']:>10} s")
    print(f"→ {salida}")


if __name__ == "__main__":
    main()
