"""Cierre del Pilar 1 (BC-10): corre la batería fija en GPU y en CPU y guarda todo en un CSV.

    python3 bateria.py                          # GPU, CPU C++ y Python, 3 repeticiones (unos minutos)
    python3 bateria.py --impl gpu,cpu --repeticiones 1 --largos 1-5

Usa los tres mineros del Hit 7, que tienen la misma interfaz (cadena, prefijo, mínimo, máximo):
  gpu    -> ../hit7/pow_rango        (CUDA, un hilo por nonce)
  cpu    -> ../hit7/pow_rango_cpu    (C++, un hilo)
  python -> ../hit7/referencia.py    (hashlib, un proceso; el mismo bucle que el minero CPU del Pilar 2)
Si falta un binario (o su fuente es más nueva) lo compila. Si no hay GPU, la saltea con un aviso.

Cada corrida se compara con el resultado esperado de casos.csv (calculado con hashlib) y se
vuelve a hashear con hashlib. Termina con código 1 si alguna no coincide.
"""

import argparse
import csv
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
HIT7 = AQUI.parent / "hit7"
EXE = ".exe" if os.name == "nt" else ""
LINEA = re.compile(
    r"^(?:OK nonce=(?P<nonce>\d+) hash=(?P<hash>[0-9a-f]{32}) |SIN_SOLUCION )"
    r"evaluados=(?P<evaluados>\d+) segundos=(?P<segundos>\d+\.\d{6}) hps=(?P<hps>\d+)$")
CAMPOS = ["implementacion", "hardware", "caso", "cadena_nombre", "largo", "prefijo", "tipo", "min", "max",
          "repeticion", "resultado", "nonce", "hash", "evaluados", "segundos", "hps", "correcto", "detalle"]


def rango(texto):
    desde, _, hasta = texto.partition("-")
    return set(range(int(desde), int(hasta or desde) + 1))


def aviso(texto):
    print(f"AVISO: {texto}", file=sys.stderr, flush=True)


def nvidia_smi(*argumentos):
    """Corre nvidia-smi (por su ruta completa) y devuelve su stdout, o None si no está o falla."""
    ruta = shutil.which("nvidia-smi")
    if not ruta:
        return None
    try:
        proceso = subprocess.run([ruta, *argumentos], capture_output=True, text=True)
    except OSError:
        return None
    return proceso.stdout if proceso.returncode == 0 else None


def hay_gpu():
    return nvidia_smi("-L") is not None


def nombre_gpu():
    salida = nvidia_smi("--query-gpu=name", "--format=csv,noheader") or ""
    return salida.splitlines()[0].strip() if salida.strip() else "GPU desconocida"


def nombre_cpu():
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for linea in cpuinfo.read_text().splitlines():
            if linea.startswith("model name"):
                return linea.split(":", 1)[1].strip()
    return platform.processor() or "CPU desconocida"


def compilar(binario, fuente, comando):
    """Compila si el binario falta o es más viejo que la fuente. Devuelve True si queda listo."""
    if binario.exists() and binario.stat().st_mtime >= fuente.stat().st_mtime:
        return True
    if not shutil.which(comando[0]):
        if binario.exists():
            aviso(f"{binario.name} es más viejo que {fuente.name} y no hay {comando[0]}; se usa igual")
            return True
        aviso(f"falta {binario.name} y no hay {comando[0]} para compilarlo")
        return False
    print(f"Compilando {fuente.name}: {' '.join(comando)}", flush=True)
    return subprocess.run(comando, cwd=HIT7).returncode == 0


def preparar(pedidas):
    """Devuelve {implementación: (comando, hardware)} con las que se pueden correr."""
    listas = {}
    if "gpu" in pedidas:
        binario = HIT7 / f"pow_rango{EXE}"
        if not hay_gpu():
            aviso("no se detectó GPU (nvidia-smi); se saltea gpu")
        elif compilar(binario, HIT7 / "pow_rango.cu",
                      ["nvcc", "-O3", "-arch=native", "pow_rango.cu", "-o", "pow_rango"]):
            listas["gpu"] = ([str(binario)], nombre_gpu())
    if "cpu" in pedidas:
        binario = HIT7 / f"pow_rango_cpu{EXE}"
        if compilar(binario, HIT7 / "pow_rango_cpu.cpp",
                    ["g++", "-O3", "-std=c++17", "pow_rango_cpu.cpp", "-o", "pow_rango_cpu"]):
            listas["cpu"] = ([str(binario)], nombre_cpu())
    if "python" in pedidas:
        listas["python"] = ([sys.executable, str(HIT7 / "referencia.py")], nombre_cpu())
    return listas


def verificar(caso, codigo, salida):
    """Devuelve (fila parcial con lo que imprimió el minero, motivo del error o "")."""
    lineas = salida.splitlines()
    m = LINEA.match(lineas[0]) if len(lineas) == 1 else None
    if not m:
        return {}, f"salida fuera de contrato (código {codigo}): {salida.strip()[:80]!r}"
    leido = {"resultado": lineas[0].split()[0], "nonce": m["nonce"] or "", "hash": m["hash"] or "",
             "evaluados": m["evaluados"], "segundos": m["segundos"], "hps": m["hps"]}
    esperado_codigo = 0 if caso["esperado"] == "OK" else 2
    if codigo != esperado_codigo:
        return leido, f"código {codigo}, se esperaba {esperado_codigo}"
    for campo in ("resultado", "nonce", "hash", "evaluados"):
        esperado = caso["esperado"] if campo == "resultado" else caso[campo]
        if leido[campo] != esperado:
            return leido, f"{campo}={leido[campo]!r}, se esperaba {esperado!r}"
    if leido["resultado"] == "OK":
        # Verificación independiente del hash que devolvió el minero.
        real = hashlib.md5(f"{caso['cadena']}{leido['nonce']}".encode()).hexdigest()
        if real != leido["hash"] or not real.startswith(caso["prefijo"]):
            return leido, f"hashlib da {real}"
    return leido, ""


def main():
    argumentos = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    argumentos.add_argument("--impl", default="gpu,cpu,python", help="lista separada por comas: gpu,cpu,python")
    argumentos.add_argument("--repeticiones", type=int, default=3)
    argumentos.add_argument("--largos", type=rango, default=rango("1-6"), help="por ejemplo 1-6 o 4")
    argumentos.add_argument("--casos", type=Path, default=AQUI / "casos.csv")
    argumentos.add_argument("--csv", type=Path, default=AQUI / "resultados.csv")
    opciones = argumentos.parse_args()

    with opciones.casos.open(newline="") as archivo:
        casos = [c for c in csv.DictReader(archivo) if int(c["largo"]) in opciones.largos]
    implementaciones = preparar({i.strip() for i in opciones.impl.split(",")})
    if not implementaciones:
        sys.exit("Error: no hay ninguna implementación para correr.")

    total = len(casos) * len(implementaciones) * opciones.repeticiones
    print(f"{len(casos)} casos x {len(implementaciones)} implementaciones "
          f"({', '.join(implementaciones)}) x {opciones.repeticiones} repeticiones = {total} corridas", flush=True)

    errores = {impl: 0 for impl in implementaciones}
    with opciones.csv.open("w", newline="") as archivo:
        escritor = csv.DictWriter(archivo, CAMPOS, lineterminator="\n")
        escritor.writeheader()
        for impl, (comando, hardware) in implementaciones.items():
            print(f"\n== {impl} ({hardware})", flush=True)
            for caso in casos:
                for repeticion in range(1, opciones.repeticiones + 1):
                    proceso = subprocess.run(comando + [caso["cadena"], caso["prefijo"], caso["min"], caso["max"]],
                                             capture_output=True, text=True)
                    leido, motivo = verificar(caso, proceso.returncode, proceso.stdout)
                    fila = {"implementacion": impl, "hardware": hardware, "repeticion": repeticion,
                            "correcto": "no" if motivo else "si", "detalle": motivo,
                            **{c: caso[c] for c in ("caso", "cadena_nombre", "largo", "prefijo", "tipo", "min", "max")},
                            **leido}
                    escritor.writerow(fila)
                    archivo.flush()
                    if motivo:
                        errores[impl] += 1
                    print(f"  {'OK ' if not motivo else 'MAL'} {caso['caso']:16} #{repeticion} "
                          f"{fila.get('segundos', '?'):>10} s {int(fila.get('hps') or 0):>14,} H/s"
                          + (f"  <- {motivo}" if motivo else ""), flush=True)

    print("\nResumen:")
    for impl in implementaciones:
        corridas = len(casos) * opciones.repeticiones
        print(f"  {impl:7} {corridas - errores[impl]}/{corridas} corridas correctas")
    print(f"Resultados en {opciones.csv}")
    sys.exit(1 if any(errores.values()) else 0)


if __name__ == "__main__":
    main()
