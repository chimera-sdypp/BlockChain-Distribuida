"""Cierre del Pilar 1 (BC-10): tablas (en Markdown) y gráfico de la comparativa CPU vs GPU.

    python3 graficar.py [resultados.csv]      # imprime las tablas y guarda comparativa.png (necesita matplotlib)

Usa solo las corridas correctas. Dos medidas, cada una de su tipo de caso:
- hashes/s: rangos SIN solución, que se recorren enteros (suma de evaluados / suma de segundos).
- tiempo hasta encontrar: rangos CON solución, desde el nonce 0 (promedio de las corridas).
"""

import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

AQUI = Path(__file__).resolve().parent
NOMBRES = {"gpu": "GPU (CUDA)", "cpu": "CPU C++ (1 hilo)", "python": "Python (1 proceso)"}
# Paleta categórica validada (orden fijo: azul, naranja, aqua); marcador distinto por serie.
COLOR = {"gpu": "#2a78d6", "cpu": "#eb6834", "python": "#1baf7a"}
MARCADOR = {"gpu": "o", "cpu": "s", "python": "^"}
TINTA, SECUNDARIA, APAGADA, GRILLA, EJE, FONDO = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"


def miles(numero, decimales=0):
    texto = f"{numero:,.{decimales}f}"
    return texto.replace(",", "_").replace(".", ",").replace("_", ".")


def leer(ruta):
    with Path(ruta).open(newline="") as archivo:
        filas = [f for f in csv.DictReader(archivo) if f["correcto"] == "si"]
    if not filas:
        sys.exit("No hay corridas correctas en el CSV.")
    return filas


def rendimiento(filas, clave):
    """hashes/s agregados (suma evaluados / suma segundos) de los rangos sin solución, por clave."""
    evaluados, segundos = defaultdict(int), defaultdict(float)
    for f in filas:
        if f["tipo"] == "sin_solucion":
            k = clave(f)
            evaluados[k] += int(f["evaluados"])
            segundos[k] += float(f["segundos"])
    return {k: evaluados[k] / segundos[k] for k in evaluados if segundos[k] > 0}


def tiempo_hasta_solucion(filas):
    tiempos = defaultdict(list)
    for f in filas:
        if f["tipo"] == "con_solucion":
            tiempos[(f["implementacion"], int(f["largo"]))].append(float(f["segundos"]))
    return {k: statistics.mean(v) for k, v in tiempos.items()}


def con_unidad(unidades):
    """Formatea las marcas del eje log con unidades legibles (p. ej. 1 MH/s, 10 ms)."""
    def formato(valor, _):
        for factor, sufijo in unidades:
            if valor >= factor * 0.999:
                numero = valor / factor
                return f"{numero:g} {sufijo}".strip() if sufijo else f"×{numero:g}"
        factor, sufijo = unidades[-1]
        return f"{valor / factor:g} {sufijo}"
    return FuncFormatter(formato)


HPS = con_unidad([(1e9, "GH/s"), (1e6, "MH/s"), (1e3, "kH/s"), (1, "H/s")])
SEGUNDOS = con_unidad([(1, "s"), (1e-3, "ms"), (1e-6, "µs")])
VECES = FuncFormatter(lambda valor, _: f"×{valor:g}")


def estilo(eje, titulo, ylabel, formato):
    eje.set_facecolor(FONDO)
    eje.set_yscale("log")
    eje.yaxis.set_major_locator(LogLocator(base=10))
    eje.yaxis.set_major_formatter(formato)
    eje.yaxis.set_minor_formatter(NullFormatter())
    eje.set_title(titulo, color=TINTA, loc="left", fontsize=11)
    eje.set_xlabel("Largo del prefijo (ceros hexadecimales)", color=SECUNDARIA)
    eje.set_ylabel(ylabel, color=SECUNDARIA)
    eje.grid(True, axis="y", which="major", color=GRILLA, linewidth=0.8)
    for borde in ("top", "right"):
        eje.spines[borde].set_visible(False)
    for borde in ("left", "bottom"):
        eje.spines[borde].set_color(EJE)
    eje.tick_params(colors=APAGADA)


def serie(eje, xs, ys, impl, etiqueta):
    """Dibuja la línea (nombre completo en la leyenda) y devuelve su final con el nombre corto."""
    eje.plot(xs, ys, color=COLOR[impl], linewidth=2, marker=MARCADOR[impl], markersize=8,
             markeredgecolor=FONDO, markeredgewidth=1.5, label=etiqueta, zorder=3)
    return (xs[-1], ys[-1], etiqueta.split(" (")[0])


def etiquetar(eje, finales, separacion=13):
    """Etiqueta directa al final de cada línea, en tinta (no en el color de la serie).
    Si dos finales quedan muy cerca en pantalla, separa las etiquetas en vertical."""
    puntos = sorted((eje.transData.transform((x, y))[1], x, y, texto) for x, y, texto in finales)
    dpi = eje.figure.dpi
    anterior = None
    for y_pantalla, x, y, texto in puntos:
        destino = y_pantalla if anterior is None else max(y_pantalla, anterior + separacion * dpi / 72)
        eje.annotate(texto, (x, y), xytext=(9, (destino - y_pantalla) * 72 / dpi), textcoords="offset points",
                     va="center", fontsize=8.5, color=SECUNDARIA)
        anterior = destino


def main():
    filas = leer(sys.argv[1] if len(sys.argv) > 1 else AQUI / "resultados.csv")
    impls = [i for i in NOMBRES if any(f["implementacion"] == i for f in filas)]
    hardware = {f["implementacion"]: f["hardware"] for f in filas}
    largos = sorted({int(f["largo"]) for f in filas})
    hps = rendimiento(filas, lambda f: (f["implementacion"], int(f["largo"])))
    hps_cadena = rendimiento(filas, lambda f: (f["implementacion"], f["cadena_nombre"]))
    tiempos = tiempo_hasta_solucion(filas)

    print("Hardware: " + " · ".join(f"{NOMBRES[i]}: {hardware[i]}" for i in impls) + "\n")
    print("### Hashes/s por largo de prefijo (rangos sin solución, recorridos enteros)\n")
    cab = " | ".join(f"MH/s {NOMBRES[i]}" for i in impls)
    speed = [o for o in ("cpu", "python") if "gpu" in impls and o in impls]
    print(f"| Largo | Nonces recorridos | {cab} |" + "".join(f" Speedup GPU/{NOMBRES[o].split(' (')[0]} |" for o in speed))
    print("|---" * (2 + len(impls) + len(speed)) + "|")
    for largo in largos:
        recorridos = sum(int(f["evaluados"]) for f in filas if f["tipo"] == "sin_solucion"
                         and int(f["largo"]) == largo and f["implementacion"] == impls[0])
        celdas = [miles(hps[(i, largo)] / 1e6, 2) if (i, largo) in hps else "—" for i in impls]
        sp = [f"×{miles(hps[('gpu', largo)] / hps[(o, largo)], 1)}" if ("gpu", largo) in hps and (o, largo) in hps
              else "—" for o in speed]
        print(f"| {largo} | {miles(recorridos)} | " + " | ".join(celdas) + " |" + "".join(f" {s} |" for s in sp))

    print("\n### Tiempo promedio hasta encontrar la solución (rangos con solución, desde el nonce 0)\n")
    print("| Largo | " + " | ".join(f"Tiempo {NOMBRES[i]}" for i in impls) + " |")
    print("|---" * (1 + len(impls)) + "|")
    for largo in largos:
        celdas = [f"{miles(tiempos[(i, largo)], 6)} s" if (i, largo) in tiempos else "—" for i in impls]
        print(f"| {largo} | " + " | ".join(celdas) + " |")

    print("\n### Hashes/s según la cadena (todos los rangos sin solución)\n")
    cadenas = sorted({f["cadena_nombre"] for f in filas})
    print("| Cadena | " + " | ".join(f"MH/s {NOMBRES[i]}" for i in impls) + " |")
    print("|---" * (1 + len(impls)) + "|")
    for cadena in cadenas:
        celdas = [miles(hps_cadena[(i, cadena)] / 1e6, 2) if (i, cadena) in hps_cadena else "—" for i in impls]
        print(f"| {cadena} | " + " | ".join(celdas) + " |")

    figura, ejes = plt.subplots(1, 3, figsize=(16, 5), facecolor=FONDO)
    finales = [[], [], []]
    for impl in impls:
        xs = [l for l in largos if (impl, l) in hps]
        if xs:
            finales[0].append(serie(ejes[0], xs, [hps[(impl, l)] for l in xs], impl, NOMBRES[impl]))
        xs = [l for l in largos if (impl, l) in tiempos]
        if xs:
            finales[1].append(serie(ejes[1], xs, [tiempos[(impl, l)] for l in xs], impl, NOMBRES[impl]))
    for otro in speed:
        xs = [l for l in largos if ("gpu", l) in hps and (otro, l) in hps]
        if xs:
            finales[2].append(serie(ejes[2], xs, [hps[("gpu", l)] / hps[(otro, l)] for l in xs], otro,
                                    f"GPU / {NOMBRES[otro].split(' (')[0]}"))
    estilo(ejes[0], "Hashes por segundo (rangos sin solución)", "Hashes/s (escala log)", HPS)
    estilo(ejes[1], "Tiempo hasta encontrar (rangos con solución)", "Tiempo (escala log)", SEGUNDOS)
    estilo(ejes[2], "Speedup de la GPU (hashes/s)", "Veces más rápida (escala log)", VECES)
    for eje, fin in zip(ejes, finales):
        eje.set_xticks(largos)
        eje.set_xlim(min(largos) - 0.3, max(largos) + 1.6)  # lugar para las etiquetas directas
        if fin:
            eje.legend(frameon=False, loc="best", fontsize=8.5, labelcolor=SECUNDARIA)
    figura.tight_layout(rect=(0, 0, 1, 0.94))
    for eje, fin in zip(ejes, finales):
        etiquetar(eje, fin)  # después del layout: necesita las posiciones finales en pantalla
    titulo = " · ".join(f"{NOMBRES[i].split(' (')[0]}: {hardware[i]}" for i in impls if i != "python")
    figura.suptitle(f"Comparativa CPU vs GPU del minero MD5 · {titulo}", color=TINTA, x=0.01, ha="left")
    figura.savefig(AQUI / "comparativa.png", dpi=150)
    print(f"\nGráfico en {AQUI / 'comparativa.png'}")


if __name__ == "__main__":
    main()
