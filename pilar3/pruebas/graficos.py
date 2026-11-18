"""Gráficos comparativos de las pruebas del Pilar 3 (BC-42), a partir de los CSV de cada una.

    python pilar3/pruebas/graficos.py            # todos los que tengan datos
    python pilar3/pruebas/graficos.py bulks gpu  # sólo esos

| Gráfico | Lee | Escribe |
|---|---|---|
| bulks | bulks/resultados.csv (BC-38) | resultados/bulks.png |
| prefijos | resultados/prefijos-{gpu,cpu}.csv (BC-39) | resultados/prefijos.png (grafico_prefijos.py) |
| fragmentos | resultados/fragmentos-{gpu,cpu}.csv (BC-40) | resultados/fragmentos.png |
| gpu | resultados/gpu-ciclos-{muestras,eventos,bloques}.csv (BC-41) | resultados/gpu-ciclos.png |

Si falta el CSV de uno, lo saltea y lo avisa: se rehace cada gráfico al repetir su prueba.
"""

import csv
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

AQUI = Path(__file__).parent
RESULTADOS = AQUI / "resultados"
FONDO, TEXTO, TEXTO_2, GRILLA = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
AZUL, NARANJA, VERDE, VIOLETA, GRIS = "#2a78d6", "#eb6834", "#2e9e6a", "#8a5cc2", "#9a9893"


def leer(ruta):
    with open(ruta) as archivo:
        return list(csv.DictReader(archivo))


def estilo(ax, titulo, x, y):
    ax.set_facecolor(FONDO)
    ax.set_title(titulo, color=TEXTO, fontsize=10.5, loc="left")
    ax.set_xlabel(x, color=TEXTO_2)
    ax.set_ylabel(y, color=TEXTO_2)
    ax.grid(axis="y", color=GRILLA, linewidth=0.8)
    ax.tick_params(colors=TEXTO_2)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    for lado in ("left", "bottom"):
        ax.spines[lado].set_color(GRILLA)


def guardar(fig, nombre):
    fig.tight_layout()
    fig.savefig(RESULTADOS / nombre, facecolor=FONDO)
    plt.close(fig)
    print(RESULTADOS / nombre)


def bulks():
    """BC-38: latencia HTTP por lote y tiempo hasta confirmar todo, contra el tamaño del bulk."""
    filas = leer(AQUI / "bulks" / "resultados.csv")
    completas = [f for f in filas if f["completa"] == "si"]
    fig, (izq, der) = plt.subplots(1, 2, figsize=(13, 5.2), dpi=150)
    fig.patch.set_facecolor(FONDO)

    por_tamano = defaultdict(list)
    for f in completas:
        por_tamano[int(f["tamano"])].append(f)
    tamanos = sorted(por_tamano)
    for campo, color, nombre in (("p50_ms", AZUL, "p50"), ("p95_ms", VIOLETA, "p95"), ("p99_ms", NARANJA, "p99")):
        medias = [statistics.mean(float(f[campo]) / 1000 for f in por_tamano[t]) for t in tamanos]
        izq.plot(tamanos, medias, color=color, marker="o", linewidth=2, label=nombre)
    izq.set_xscale("log")
    izq.set_yscale("log")
    estilo(izq, "Respuesta HTTP de cada lote de hasta 1.000 (10 VUs hasta 1.000; 1 VU desde 10.000)", "Transacciones del bulk", "Segundos")
    izq.legend(frameon=False, fontsize=9)

    medias = [statistics.mean(float(f["segundos_confirmacion"]) for f in por_tamano[t]) for t in tamanos]
    der.plot(tamanos, medias, color=AZUL, marker="o", linewidth=2, label="completas (media)")
    for f in completas:
        der.scatter(int(f["tamano"]), float(f["segundos_confirmacion"]), color=AZUL, alpha=0.35, s=20, linewidths=0)
    fallidas = [f for f in filas if f["completa"] != "si"]
    if fallidas:
        der.scatter([int(f["tamano"]) for f in fallidas], [float(f["segundos_confirmacion"]) for f in fallidas],
                    marker="x", color=NARANJA, s=40, label="incompletas (10 VUs: nonce salteado)")
    for t, m in zip(tamanos, medias):
        der.annotate(f"{t / m:.{0 if t / m >= 10 else 2}f} tx/s".replace(".", ","), (t, m), textcoords="offset points", xytext=(6, -12),
                     color=TEXTO_2, fontsize=8)
    der.axhline(30, color=GRIS, linestyle=":", linewidth=1)
    der.text(1, 32, "ventana del NCT (30 s)", color=TEXTO_2, fontsize=8)
    der.set_xscale("log")
    der.set_yscale("log")
    estilo(der, "Hasta confirmar todo el bulk en bloques", "Transacciones del bulk", "Segundos")
    der.legend(frameon=False, fontsize=9, loc="upper left")
    guardar(fig, "bulks.png")


def prefijos():
    import grafico_prefijos
    grafico_prefijos.main()


def fragmentos():
    """BC-40: tiempo por bloque y rangos prestados según FRAGMENTO_PORCENTAJE."""
    series = [(n, c, RESULTADOS / f"fragmentos-{n}.csv") for n, c in (("gpu", AZUL), ("cpu", NARANJA))]
    series = [(n, c, r) for n, c, r in series if r.exists()]
    if not series:
        raise FileNotFoundError("resultados/fragmentos-{gpu,cpu}.csv")
    fig, (izq, der) = plt.subplots(1, 2, figsize=(13, 5.2), dpi=150)
    fig.patch.set_facecolor(FONDO)
    for nombre, color, ruta in series:
        por = defaultdict(list)
        for f in leer(ruta):
            if f["segundos"]:
                por[int(f["porcentaje"])].append(f)
        pcts = sorted(por)
        izq.plot(pcts, [statistics.mean(float(f["segundos"]) for f in por[p]) for p in pcts],
                 color=color, marker="o", linewidth=2, label=nombre.upper())
        der.plot(pcts, [statistics.mean(int(f["prestados"]) for f in por[p]) for p in pcts],
                 color=color, marker="o", linewidth=2, label=f"{nombre.upper()}: prestados")
        der.plot(pcts, [statistics.mean(int(f["prestados_hasta_ganar"]) for f in por[p]) for p in pcts],
                 color=color, marker="s", linewidth=1.2, linestyle="--", label=f"{nombre.upper()}: hasta el ganador")
    for ax in (izq, der):
        ax.set_xscale("log")
        ax.set_xticks([1, 5, 10, 25, 50], ["1 %", "5 %", "10 %", "25 %", "50 %"])
    estilo(izq, "Tiempo por bloque", "FRAGMENTO_PORCENTAJE (tamaño de cada rango)", "Segundos (media)")
    estilo(der, "Rangos prestados por bloque", "FRAGMENTO_PORCENTAJE (tamaño de cada rango)", "Rangos (media)")
    izq.legend(frameon=False, fontsize=9)
    der.legend(frameon=False, fontsize=9)
    guardar(fig, "fragmentos.png")


def gpu():
    """BC-41: una fila por ciclo con cuándo estuvo prendido cada componente y cuándo se confirmó cada
    bloque, según el tipo de minero que lo encontró."""
    muestras = leer(RESULTADOS / "gpu-ciclos-muestras.csv")
    eventos = leer(RESULTADOS / "gpu-ciclos-eventos.csv")
    bloques = leer(RESULTADOS / "gpu-ciclos-bloques.csv")
    ciclos = sorted({int(m["ciclo"]) for m in muestras})
    fig, ejes = plt.subplots(len(ciclos), 2, figsize=(13, 3.2 * len(ciclos)), dpi=150, squeeze=False,
                             gridspec_kw={"width_ratios": [1, 0.0001]})
    fig.patch.set_facecolor(FONDO)
    instante = lambda texto: datetime.fromisoformat(texto).timestamp()  # noqa: E731
    carriles = [("gpus_vivas", AZUL, "minero GPU vivo"), ("escalador", VIOLETA, "escalador (KEDA)"),
                ("vms", VERDE, "VM minero-cpu"), ("cpus_vivas", NARANJA, "minero CPU vivo")]
    for fila, ciclo in zip(ejes, ciclos):
        ax = fila[0]
        fila[1].set_visible(False)
        propias = [m for m in muestras if int(m["ciclo"]) == ciclo]
        t0 = instante(propias[0]["instante"]) - float(propias[0]["segundos"])
        x = [instante(m["instante"]) - t0 for m in propias]
        fin = x[-1] + 5
        for carril, (campo, color, nombre) in enumerate(carriles):
            y = len(carriles) - carril
            tramos, inicio = [], None
            for xi, m in zip(x, propias):
                if int(m[campo]) > 0 and inicio is None:
                    inicio = xi
                elif int(m[campo]) == 0 and inicio is not None:
                    tramos.append((inicio, xi - inicio))
                    inicio = None
            if inicio is not None:
                tramos.append((inicio, fin - inicio))
            ax.broken_barh(tramos, (y - 0.32, 0.64), facecolors=color)
        ax.set_yticks(range(len(carriles), -1, -1), [n for _, _, n in carriles] + ["bloques confirmados"])
        vuelta = next((instante(m["instante"]) - t0 - float(m["segundos"]) for m in propias if m["fase"] == "vuelta"),
                      None)
        if vuelta is not None:
            ax.axvline(0, color=TEXTO, linewidth=1)
            ax.axvline(vuelta, color=TEXTO, linewidth=1)
            ax.text(2, len(carriles) + 0.55, "se apaga la GPU", fontsize=8, color=TEXTO)
            ax.text(vuelta + 2, len(carriles) + 0.55, "vuelve la GPU", fontsize=8, color=TEXTO)
        for e in eventos:
            if int(e["ciclo"]) == ciclo and e["evento"] in ("alerta_disparada", "alerta_apagada"):
                xe = instante(e["instante"]) - t0
                ax.axvline(xe, color=GRIS, linewidth=0.8, linestyle=":")
                ax.text(xe + 2, 0.45, e["evento"].replace("_", " "), fontsize=7, color=TEXTO_2)
        # Los bloques, en un carril más, en el momento en que se confirmaron (timestamp de la tarea + minado)
        for tipo, color in (("gpu", AZUL), ("cpu", NARANJA)):
            sel = [b for b in bloques if int(b["ciclo"]) == ciclo and b["tipo"] == tipo and b["duracion"]]
            ax.scatter([instante(b["instante"]) + float(b["duracion"]) - t0 for b in sel], [0] * len(sel),
                       marker="|", s=260, linewidths=2.2, color=color, label=f"bloque minado por {tipo.upper()}")
        ax.set_ylim(-0.6, len(carriles) + 0.9)
        ax.set_xlim(-10, fin)
        estilo(ax, f"Ciclo {ciclo}", "Segundos desde que se apaga la GPU", "")
        ax.grid(axis="x", color=GRILLA, linewidth=0.8)
        if ciclo == ciclos[0]:
            ax.legend(frameon=False, fontsize=8, loc="upper right", bbox_to_anchor=(1.0, 1.3), ncol=2)
    guardar(fig, "gpu-ciclos.png")


GRAFICOS = {"bulks": bulks, "prefijos": prefijos, "fragmentos": fragmentos, "gpu": gpu}


def main():
    pedidos = sys.argv[1:] or list(GRAFICOS)
    for nombre in pedidos:
        try:
            GRAFICOS[nombre]()
        except FileNotFoundError as error:
            print(f"{nombre}: sin datos ({error.filename or error})")


if __name__ == "__main__":
    main()
