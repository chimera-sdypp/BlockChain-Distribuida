"""Gráfico de la prueba de prefijos (BC-39): tiempo por bloque según el largo del prefijo, GPU y CPU.

    python pilar3/pruebas/grafico_prefijos.py   # lee resultados/prefijos-{gpu,cpu}.csv, escribe resultados/prefijos.png
"""

import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RESULTADOS = Path(__file__).parent / "resultados"
FONDO, TEXTO, TEXTO_2, GRILLA = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
# (archivo, nombre, color, hashes/s del bench: para la curva de 16^p intentos sin el resto del sistema)
SERIES = [("prefijos-gpu.csv", "1 GPU (RTX 4060)", "#2a78d6", 1.78e9),
          ("prefijos-cpu.csv", "3 VMs CPU (e2-highcpu-2)", "#eb6834", 3 * 1.44e6)]


def leer(nombre):
    tiempos, cortes = defaultdict(list), {}
    with open(RESULTADOS / nombre) as archivo:
        for fila in csv.DictReader(archivo):
            if fila["segundos"]:
                tiempos[int(fila["prefijo"])].append(float(fila["segundos"]))
            else:
                cortes[int(fila["prefijo"])] = fila["minero"]
    return tiempos, cortes


def main():
    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=150)
    fig.patch.set_facecolor(FONDO)
    ax.set_facecolor(FONDO)
    for archivo, nombre, color, hps in SERIES:
        tiempos, cortes = leer(archivo)
        largos = sorted(tiempos)
        medias = [statistics.mean(tiempos[p]) for p in largos]
        for p in largos:
            ax.scatter([p] * len(tiempos[p]), tiempos[p], s=22, color=color, alpha=0.35, linewidths=0, zorder=2)
        ax.plot(largos, medias, color=color, linewidth=2, marker="o", markersize=7,
                markeredgecolor=FONDO, markeredgewidth=2, label=f"{nombre}: media de 5 bloques", zorder=3)
        teoricos = list(range(1, 9))
        ax.plot(teoricos, [16 ** p / hps for p in teoricos], color=color, linewidth=1.2, linestyle=":",
                label=f"{nombre}: sólo minar 16^p hashes", zorder=1)
        for p, motivo in cortes.items():
            ax.annotate("", xy=(p, 1500), xytext=(p, 600), zorder=3,
                        arrowprops={"arrowstyle": "-|>", "color": color, "linewidth": 2})
            ax.text(p - 0.12, 600, motivo, color=TEXTO_2, fontsize=9, ha="right", va="center")
        ax.text(largos[-1] + 0.12, medias[-1], f"{medias[-1]:.1f} s", color=TEXTO, fontsize=9, va="center")

    ax.set_yscale("log")
    ax.set_ylim(1e-3, 3e3)
    ax.set_xlim(0.6, 8.9)
    ax.set_xticks(range(1, 9))
    ax.set_xlabel("Largo del prefijo (caracteres hexadecimales en 0)", color=TEXTO_2)
    ax.set_ylabel("Segundos por bloque (escala logarítmica)", color=TEXTO_2)
    ax.set_title("Tiempo por bloque según el prefijo: desde que el NCT publica la tarea hasta que confirma el bloque",
                 color=TEXTO, fontsize=11, loc="left")
    ax.grid(axis="y", color=GRILLA, linewidth=0.8)
    ax.tick_params(colors=TEXTO_2)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    for lado in ("left", "bottom"):
        ax.spines[lado].set_color(GRILLA)
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=TEXTO)
    fig.tight_layout()
    fig.savefig(RESULTADOS / "prefijos.png", facecolor=FONDO)
    print(RESULTADOS / "prefijos.png")


if __name__ == "__main__":
    main()
