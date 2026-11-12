"""Hit 6: corre el minero del Hit 5 con prefijos de distinto largo y guarda cada corrida en un CSV.

    python3 medir.py --largos 1-9 --repeticiones 10
    python3 medir.py --largos 10 --repeticiones 3      # agrega al mismo CSV

El prefijo es de ceros ("0", "00", ...) y la cadena cambia en cada corrida, así cada una
es una búsqueda distinta. El tiempo es el que mide el propio minero (sin el arranque de CUDA).
"""

import argparse
import csv
import re
import subprocess
from pathlib import Path

AQUI = Path(__file__).resolve().parent
HIT5 = AQUI.parent / "hit5"
MINERO = HIT5 / "pow"
CAMPOS = ["gpu", "largo", "prefijo", "cadena", "nonce", "intentos", "segundos", "mhs"]
PATRONES = {
    "nonce": r"Nonce ganador:\s+(\d+)",
    "intentos": r"Hashes evaluados:\s+(\d+)",
    "segundos": r"Tiempo total:\s+([\d.]+) s",
    "mhs": r"Rendimiento:\s+([\d.]+) MH/s",
}


def rango(texto):
    desde, _, hasta = texto.partition("-")
    return range(int(desde), int(hasta or desde) + 1)


def nombre_gpu():
    salida = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                            capture_output=True, text=True, check=True).stdout
    return salida.splitlines()[0].strip()


def minar(cadena, prefijo):
    salida = subprocess.run([str(MINERO), cadena, prefijo], capture_output=True, text=True, check=True).stdout
    return {campo: re.search(patron, salida).group(1) for campo, patron in PATRONES.items()}


def main():
    argumentos = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    argumentos.add_argument("--largos", type=rango, default=rango("1-8"), help="por ejemplo 1-9 o 10")
    argumentos.add_argument("--repeticiones", type=int, default=5)
    argumentos.add_argument("--csv", type=Path, default=AQUI / "mediciones.csv")
    opciones = argumentos.parse_args()

    if not MINERO.exists():
        subprocess.run(["nvcc", "-O3", "-arch=native", "pow.cu", "-o", "pow"], cwd=HIT5, check=True)
    gpu = nombre_gpu()
    nuevo = not opciones.csv.exists()
    with opciones.csv.open("a", newline="") as archivo:
        escritor = csv.DictWriter(archivo, CAMPOS)
        if nuevo:
            escritor.writeheader()
        for largo in opciones.largos:
            prefijo = "0" * largo
            for repeticion in range(1, opciones.repeticiones + 1):
                cadena = f"hit6-{largo}-{repeticion}"
                fila = {"gpu": gpu, "largo": largo, "prefijo": prefijo, "cadena": cadena, **minar(cadena, prefijo)}
                escritor.writerow(fila)
                archivo.flush()
                print(f"largo {largo} #{repeticion}: {fila['segundos']} s, {int(fila['intentos']):,} intentos")


if __name__ == "__main__":
    main()
