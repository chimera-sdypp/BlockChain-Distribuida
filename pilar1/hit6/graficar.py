"""Hit 6: arma la tabla (en Markdown) y el gráfico de tiempo contra largo del prefijo a partir de mediciones.csv.

    python3 graficar.py [mediciones.csv]      # necesita matplotlib
"""

import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

AQUI = Path(__file__).resolve().parent
AZUL, GRIS, TINTA, FONDO = "#2a78d6", "#52514e", "#0b0b0b", "#fcfcfb"


def miles(numero):
    return f"{numero:,.0f}".replace(",", ".")


def leer(ruta):
    corridas = defaultdict(list)
    with Path(ruta).open() as archivo:
        for fila in csv.DictReader(archivo):
            corridas[int(fila["largo"])].append(fila)
    return dict(sorted(corridas.items())), fila["gpu"]


def main():
    corridas, gpu = leer(sys.argv[1] if len(sys.argv) > 1 else AQUI / "mediciones.csv")
    # Rendimiento sostenido: el de las corridas largas, donde el arranque de cada tanda no pesa.
    mhs = statistics.median(float(f["mhs"]) for largo, filas in corridas.items() if largo >= 8 for f in filas)

    print(f"GPU: {gpu} · rendimiento sostenido: {miles(mhs)} MH/s\n")
    print("| Largo | Corridas | Intentos esperados (16^n) | Intentos promedio | Tiempo promedio | Mínimo | Máximo |")
    print("|---|---|---|---|---|---|---|")
    for largo, filas in corridas.items():
        segundos = [float(f["segundos"]) for f in filas]
        intentos = statistics.mean(int(f["intentos"]) for f in filas)
        print(f"| {largo} | {len(filas)} | {miles(16 ** largo)} | {miles(intentos)} | {statistics.mean(segundos):.4f} s "
              f"| {min(segundos):.4f} s | {max(segundos):.4f} s |")

    largos = list(corridas)
    figura, eje = plt.subplots(figsize=(8, 5), facecolor=FONDO)
    eje.set_facecolor(FONDO)
    for largo, filas in corridas.items():
        eje.scatter([largo] * len(filas), [float(f["segundos"]) for f in filas], s=40, color=AZUL,
                    alpha=0.35, edgecolors=FONDO, linewidths=1, zorder=3)
    promedios = [statistics.mean(float(f["segundos"]) for f in corridas[l]) for l in largos]
    eje.plot(largos, promedios, color=AZUL, linewidth=2, marker="o", markersize=7, zorder=4,
             label="Promedio medido (puntos claros: cada corrida)")
    eje.plot(largos, [16 ** l / (mhs * 1e6) for l in largos], color=GRIS, linewidth=2, linestyle="--", zorder=2,
             label=f"Esperado: 16^n intentos a {miles(mhs)} MH/s")
    eje.set_yscale("log")
    eje.set_xticks(largos)
    eje.set_xlabel("Largo del prefijo (caracteres hexadecimales)", color=TINTA)
    eje.set_ylabel("Tiempo hasta encontrar el nonce (s, escala log)", color=TINTA)
    eje.set_title(f"Tiempo de minado según el largo del prefijo · {gpu}", color=TINTA, loc="left")
    eje.grid(True, axis="y", color="#e4e3df", linewidth=0.8)
    for borde in ("top", "right"):
        eje.spines[borde].set_visible(False)
    eje.tick_params(colors=GRIS)
    eje.legend(frameon=False, loc="upper left")
    figura.tight_layout()
    figura.savefig(AQUI / "tiempo-vs-largo.png", dpi=150)


if __name__ == "__main__":
    main()
