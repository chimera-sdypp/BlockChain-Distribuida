"""Gráfico de ajuste dinámico de dificultad (BC-60): prefijo y tiempo por bloque desde CSV.

    python pilar3/pruebas/grafico_dificultad.py   # lee resultados/dificultad.csv, escribe resultados/dificultad.png
"""

import argparse
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RESULTADOS = Path(__file__).parent / "resultados"
FONDO, TEXTO, TEXTO_2, GRILLA = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
AZUL, NARANJA, VERDE = "#2a78d6", "#eb6834", "#199e70"


def leer(ruta):
    filas = []
    with open(ruta, newline="") as archivo:
        for f in csv.DictReader(archivo):
            filas.append({
                "bloque": int(f["bloque"]),
                "fase": f.get("fase", "convergencia"),
                "prefijo": int(f["prefijo"]),
                "segundos": float(f["segundos"]),
                "gpus": int(f.get("gpus_vivas", 0)),
            })
    return filas


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--csv", default=str(RESULTADOS / "dificultad.csv"))
    p.add_argument("--salida", default=str(RESULTADOS / "dificultad.png"))
    p.add_argument("--esperado", type=float, default=60.0)
    args = p.parse_args()

    filas = leer(args.csv)
    if not filas:
        raise SystemExit("el CSV no tiene filas")

    xs = [f["bloque"] for f in filas]
    prefijos = [f["prefijo"] for f in filas]
    tiempos = [f["segundos"] for f in filas]
    corte_gpu = next((f["bloque"] for f in filas if f["fase"] == "sin_gpu"), None)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 6.2), dpi=150, sharex=True)
    fig.patch.set_facecolor(FONDO)
    for ax in (ax1, ax2):
        ax.set_facecolor(FONDO)
        ax.grid(axis="y", color=GRILLA, linewidth=0.8)
        ax.tick_params(colors=TEXTO_2)
        for lado in ("top", "right"):
            ax.spines[lado].set_visible(False)
        for lado in ("left", "bottom"):
            ax.spines[lado].set_color(GRILLA)

    ax1.step(xs, prefijos, where="post", color=AZUL, linewidth=2.2, label="Prefijo elegido")
    ax1.scatter(xs, prefijos, color=AZUL, s=28, zorder=3)
    ax1.set_ylabel("Largo del prefijo (hex)", color=TEXTO_2)
    ax1.set_title("Ajuste de dificultad por tiempo (ventana) y techo por potencia viva (GPU)",
                  color=TEXTO, fontsize=11, loc="left")

    ax2.plot(xs, tiempos, color=NARANJA, linewidth=1.8, marker="o", markersize=4.5, label="Tiempo real")
    ax2.axhline(args.esperado, color=VERDE, linestyle="--", linewidth=1.5,
                label=f"Tiempo esperado ({args.esperado:.0f} s)")
    ax2.set_xlabel("Bloque de la corrida", color=TEXTO_2)
    ax2.set_ylabel("Tiempo por bloque (s)", color=TEXTO_2)

    if corte_gpu is not None:
        for ax in (ax1, ax2):
            ax.axvline(corte_gpu - 0.5, color=TEXTO_2, linestyle=":", linewidth=1.2)
        ax1.text(corte_gpu - 0.35, max(prefijos), "Se desconecta GPU", color=TEXTO_2, fontsize=8.5, va="top")

    ax1.legend(loc="lower right", frameon=False, fontsize=9, labelcolor=TEXTO)
    ax2.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=TEXTO)
    fig.tight_layout()
    fig.savefig(args.salida, facecolor=FONDO)
    print(args.salida)


if __name__ == "__main__":
    main()
