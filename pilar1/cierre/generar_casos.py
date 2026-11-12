"""Cierre del Pilar 1 (BC-10): arma la batería fija de casos y su resultado esperado.

    python3 generar_casos.py            # escribe casos.csv (ya está en el repo; tarda unos minutos)

Para cada cadena y cada largo de prefijo (ceros, de 1 a 6) busca con hashlib las primeras
soluciones desde el nonce 0 y arma dos casos:

- con_solucion: [0, última solución hallada]. Contiene varias; la respuesta correcta es la menor.
- sin_solucion: el hueco más largo entre dos soluciones seguidas. No contiene ninguna, así que
  hay que recorrerlo entero (es el caso que mide los hashes/s sostenidos).

El resultado esperado sale de hashlib, no de los mineros que se prueban, así los verifica a todos.
"""

import csv
import hashlib
from pathlib import Path

AQUI = Path(__file__).resolve().parent
LARGOS = range(1, 7)
SOLUCIONES = 3  # soluciones a buscar por (cadena, largo): dan 2 huecos para elegir el sin_solucion


def md5(texto):
    return hashlib.md5(texto.encode()).hexdigest()


# Tres cadenas de distinto largo. Lo que pesa es cuántos bloques de 64 bytes de MD5 ocupa
# cadena + nonce: hasta 55 bytes es 1 bloque, desde 56 son 2 (el doble de trabajo por nonce).
CADENAS = {
    "corta": "bc10",  # 4 + nonce: 1 bloque
    "media": "chimera-pilar1-cierre-etapa-inicial",  # 35 + nonce: 1 bloque
    # El formato real de un bloque del Pilar 2 (contrato.texto_bloque): 77 caracteres, 2 bloques.
    "bloque": f"{md5('bloque-anterior')};1762905600;{md5('transacciones-de-prueba')};",
}

CAMPOS = ["caso", "cadena_nombre", "cadena", "largo", "prefijo", "tipo", "min", "max",
          "esperado", "nonce", "hash", "evaluados"]


def soluciones(cadena, prefijo, cantidad):
    """Las primeras `cantidad` soluciones desde 0 (con al menos un hueco no vacío entre ellas)."""
    base = hashlib.md5(cadena.encode())
    halladas, nonce = [], 0
    while True:
        h = base.copy()
        h.update(str(nonce).encode())
        if h.hexdigest().startswith(prefijo):
            halladas.append(nonce)
            hay_hueco = any(b - a > 1 for a, b in zip(halladas, halladas[1:]))
            if len(halladas) >= cantidad and hay_hueco:
                return halladas
        nonce += 1


def main():
    filas = []
    for nombre, cadena in CADENAS.items():
        for largo in LARGOS:
            prefijo = "0" * largo
            sol = soluciones(cadena, prefijo, SOLUCIONES)
            primera = sol[0]
            filas.append({
                "caso": f"{nombre}-L{largo}-con", "cadena_nombre": nombre, "cadena": cadena,
                "largo": largo, "prefijo": prefijo, "tipo": "con_solucion",
                "min": 0, "max": sol[-1], "esperado": "OK", "nonce": primera,
                "hash": md5(f"{cadena}{primera}"), "evaluados": primera + 1,
            })
            a, b = max(zip(sol, sol[1:]), key=lambda par: par[1] - par[0])
            filas.append({
                "caso": f"{nombre}-L{largo}-sin", "cadena_nombre": nombre, "cadena": cadena,
                "largo": largo, "prefijo": prefijo, "tipo": "sin_solucion",
                "min": a + 1, "max": b - 1, "esperado": "SIN_SOLUCION", "nonce": "",
                "hash": "", "evaluados": b - a - 1,
            })
            print(f"{nombre:6} L{largo}: soluciones {sol} -> sin_solucion [{a + 1}, {b - 1}]", flush=True)

    with (AQUI / "casos.csv").open("w", newline="") as archivo:
        escritor = csv.DictWriter(archivo, CAMPOS, lineterminator="\n")
        escritor.writeheader()
        escritor.writerows(filas)
    print(f"{len(filas)} casos en casos.csv")


if __name__ == "__main__":
    main()
