"""Gráfico del criterio de potencia (BC-59): segundos por bloque según cómo elige el prefijo el TrP.

- Izquierda: el grupo de la prueba (RTX 4060, 2 T4 y 2 CPU, tope 9). Barras: lo teórico; puntos: lo
  medido (potencia.csv). Con la medición, lo teórico sale de ella: 16^p (el prefijo que eligió el TrP)
  dividido la potencia total (la que registró el TrP con la suma). Sin la medición, de la tabla teórica,
  con la T4 estimada.
- Derecha: lo teórico sin tope para todos los grupos de potencia-teorica.csv. Muestra cuándo se separan
  los criterios: el escalón de ×16 por carácter los junta casi siempre.

    python pilar3/pruebas/grafico_potencia.py   # lee resultados/potencia{-teorica,}.csv, escribe resultados/potencia.png
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
CRITERIOS = ["suma", "promedio", "minimo", "maximo"]
NOMBRES = {"suma": "suma", "promedio": "promedio", "minimo": "mínimo", "maximo": "máximo"}
COLORES = {"suma": "#2a78d6", "promedio": "#199e70", "minimo": "#eb6834", "maximo": "#8a5cc2"}
GRUPO_PRUEBA, TOPE_PRUEBA, SIN_TOPE, OBJETIVO = "RTX, 2 T4 y 2 CPU", 9, 32, 30


def leer(nombre):
    ruta = RESULTADOS / nombre
    if not ruta.exists():
        return []
    with open(ruta) as archivo:
        return list(csv.DictReader(archivo))


def estilo(ax, titulo):
    ax.set_facecolor(FONDO)
    ax.set_yscale("log")
    ax.set_ylim(3e-3, 300)
    ax.axhline(OBJETIVO, color=TEXTO_2, linewidth=1, linestyle="--", zorder=1)
    ax.text(ax.get_xlim()[1], OBJETIVO * 1.15, "objetivo: 30 s", color=TEXTO_2, fontsize=8, ha="right")
    ax.set_title(titulo, color=TEXTO, fontsize=10, loc="left")
    ax.grid(axis="y", color=GRILLA, linewidth=0.8)
    ax.tick_params(colors=TEXTO_2, labelsize=8)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    for lado in ("left", "bottom"):
        ax.spines[lado].set_color(GRILLA)


def teorico_medido(medida):
    """{criterio: (prefijo, segundos)} con lo que registró el TrP en la corrida; None si falta la suma."""
    totales = [float(f["potencia"]) for f in medida if f["criterio"] == "suma" and f["potencia"]]
    if not totales:
        return None
    total, resultado = statistics.mean(totales), {}
    for criterio in CRITERIOS:
        prefijos = [int(f["prefijo"]) for f in medida if f["criterio"] == criterio and f["prefijo"]]
        if prefijos:
            prefijo = statistics.mode(prefijos)
            resultado[criterio] = (prefijo, 16**prefijo / total)
    return resultado


def prueba(ax, teorica, medida):
    teoricos = teorico_medido(medida) if medida else None
    origen = "potencia medida"
    if teoricos is None:
        teoricos = {f["criterio"]: (int(f["prefijo"]), float(f["segundos"])) for f in teorica
                    if f["grupo"] == GRUPO_PRUEBA and int(f["tope"]) == TOPE_PRUEBA}
        origen = "T4 estimada"
    medidos = defaultdict(list)
    for fila in medida:
        if fila["segundos"]:
            medidos[fila["criterio"]].append(float(fila["segundos"]))
    for x, criterio in enumerate(CRITERIOS):
        if criterio not in teoricos:
            continue
        prefijo, segundos = teoricos[criterio]
        ax.bar(x, segundos, width=0.6, color=COLORES[criterio], alpha=0.35, zorder=2)
        ax.text(x, 4e-3, f"prefijo {prefijo}", color=TEXTO, fontsize=8, ha="center")
        if medidos[criterio]:
            ax.scatter([x] * len(medidos[criterio]), medidos[criterio], s=22, color=COLORES[criterio],
                       linewidths=0, zorder=3)
            media = statistics.mean(medidos[criterio])
            ax.plot([x - 0.25, x + 0.25], [media, media], color=COLORES[criterio], linewidth=2.5, zorder=3)
    ax.set_xticks(range(len(CRITERIOS)), [NOMBRES[c] for c in CRITERIOS])
    ax.set_ylabel("Segundos por bloque (escala logarítmica)", color=TEXTO_2)
    detalle = f"barras: teórico, con la {origen}\npuntos y raya: medido" if any(medidos.values()) else \
        f"teórico, con la {origen}; falta la medición"
    estilo(ax, f"{GRUPO_PRUEBA}, tope de {TOPE_PRUEBA} caracteres\n{detalle}")


def grupos(ax, teorica):
    filas = [f for f in teorica if int(f["tope"]) == SIN_TOPE]
    nombres = list(dict.fromkeys(f["grupo"] for f in filas))
    ancho = 0.8 / len(CRITERIOS)
    for i, criterio in enumerate(CRITERIOS):
        valores = {f["grupo"]: float(f["segundos"]) for f in filas if f["criterio"] == criterio}
        xs = [x - 0.4 + ancho * (i + 0.5) for x in range(len(nombres))]
        ax.bar(xs, [valores[n] for n in nombres], width=ancho, color=COLORES[criterio], label=NOMBRES[criterio],
               zorder=2)
    ax.set_xticks(range(len(nombres)), nombres, rotation=20, ha="right")
    ax.legend(loc="upper left", frameon=False, fontsize=8, labelcolor=TEXTO, ncols=4)
    estilo(ax, "Teórico sin tope, según el grupo de mineros")


def main():
    teorica, medida = leer("potencia-teorica.csv"), leer("potencia.csv")
    if not teorica:
        raise SystemExit("falta resultados/potencia-teorica.csv: correr antes potencia_teorica.py")
    fig, (izquierda, derecha) = plt.subplots(1, 2, figsize=(12, 5.5), dpi=150, width_ratios=[1, 1.5])
    fig.patch.set_facecolor(FONDO)
    prueba(izquierda, teorica, medida)
    grupos(derecha, teorica)
    fig.suptitle("Tiempo por bloque según el criterio con que el TrP junta la potencia de los mineros vivos",
                 color=TEXTO, fontsize=11, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(RESULTADOS / "potencia.png", facecolor=FONDO)
    print(RESULTADOS / "potencia.png")


if __name__ == "__main__":
    main()
