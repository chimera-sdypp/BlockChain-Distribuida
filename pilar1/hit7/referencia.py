#!/usr/bin/env python3
"""
Pilar 1 — Hit 7: Oráculo de referencia en Python (Proof of Work con límites)

Calcula el hash MD5(cadena + nonce) en CPU usando la librería estándar hashlib.
Busca el menor nonce en el rango [min, max] tal que su hash hexadecimal
comience con el prefijo indicado.

Uso:
    python3 referencia.py <cadena> <prefijo_hex> <min> <max>

Ejemplo:
    python3 referencia.py "hit7_chimera" "000" 0 20000

Formato de salida (stdout, exactamente una línea):
    OK nonce=<u64> hash=<32 hex minúsculas> evaluados=<u64> segundos=<%.6f> hps=<%.0f>
    SIN_SOLUCION evaluados=<u64> segundos=<%.6f> hps=<%.0f>

Códigos de retorno:
    0 = Solución encontrada (OK)
    2 = Sin solución dentro del rango (SIN_SOLUCION)
    1 = Error de argumentos o validación
"""

import hashlib
import re
import sys
import time

MAX_U64 = 18446744073709551615
CADENA_MAX_BYTES = 120
PREFIJO_MAX_LEN = 32


def error(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    args = sys.argv[1:]
    if len(args) != 4:
        error(f"se esperan exactamente 4 argumentos (se recibieron {len(args)}).\n"
              f"Uso: {sys.argv[0]} <cadena> <prefijo_hex> <min> <max>")

    cadena_str, prefijo_str, min_str, max_str = args

    # Validar cadena
    cadena_bytes = cadena_str.encode("utf-8")
    if len(cadena_bytes) > CADENA_MAX_BYTES:
        error(f"la cadena base no puede superar los {CADENA_MAX_BYTES} bytes (tiene {len(cadena_bytes)}).")

    # Validar prefijo (1 a 32 caracteres hexadecimales)
    if not (1 <= len(prefijo_str) <= PREFIJO_MAX_LEN) or not re.fullmatch(r"[0-9a-fA-F]{1,32}", prefijo_str):
        error(f"el prefijo debe tener entre 1 y {PREFIJO_MAX_LEN} caracteres hexadecimales.")
    prefijo = prefijo_str.lower()

    # Validar min y max (solo dígitos, sin signos ni espacios, rango u64)
    if not re.fullmatch(r"[0-9]+", min_str):
        error(f"min inválido \"{min_str}\" (solo dígitos, 0 a {MAX_U64}).")
    min_val = int(min_str)
    if min_val > MAX_U64:
        error(f"min inválido \"{min_str}\" (supera {MAX_U64}).")

    if not re.fullmatch(r"[0-9]+", max_str):
        error(f"max inválido \"{max_str}\" (solo dígitos, 0 a {MAX_U64}).")
    max_val = int(max_str)
    if max_val > MAX_U64:
        error(f"max inválido \"{max_str}\" (supera {MAX_U64}).")

    if min_val > max_val:
        error(f"min ({min_val}) es mayor que max ({max_val}).")

    if min_val == 0 and max_val == MAX_U64:
        error(f"no se admite el rango completo [0, {MAX_U64}]; partirlo en sub-rangos.")

    # Búsqueda determinística secuencial del menor nonce
    inicio = time.perf_counter()
    n = min_val
    encontrado = False
    hash_encontrado = ""

    while n <= max_val:
        h = hashlib.md5(f"{cadena_str}{n}".encode("ascii")).hexdigest()
        if h.startswith(prefijo):
            encontrado = True
            hash_encontrado = h
            break
        n += 1

    fin = time.perf_counter()
    duracion = fin - inicio

    if encontrado:
        evaluados = n - min_val + 1
        hps = evaluados / duracion if duracion > 0 else 0.0
        print(f"OK nonce={n} hash={hash_encontrado} evaluados={evaluados} segundos={duracion:.6f} hps={hps:.0f}")
        sys.exit(0)
    else:
        evaluados = max_val - min_val + 1
        hps = evaluados / duracion if duracion > 0 else 0.0
        print(f"SIN_SOLUCION evaluados={evaluados} segundos={duracion:.6f} hps={hps:.0f}")
        sys.exit(2)


if __name__ == "__main__":
    main()
