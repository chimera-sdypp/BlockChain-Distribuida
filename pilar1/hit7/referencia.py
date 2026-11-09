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
import os
import re
import sys
import time

MAX_U64 = 18446744073709551615
CADENA_MAX_BYTES = 120
PREFIJO_MAX_LEN = 32


def error(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def parsear_u64(texto: str):
    """Entero decimal sin signo de 64 bits, solo [0-9]+. Devuelve None si no es válido.

    Se sacan los ceros a la izquierda antes de convertir: así "0000...05" es válido
    (igual que en C) y un número con miles de dígitos no choca con el límite de
    dígitos de int() de Python 3.11+ (ValueError).
    """
    if not re.fullmatch(r"[0-9]+", texto):
        return None
    significativo = texto.lstrip("0") or "0"
    if len(significativo) > 20:
        return None
    valor = int(significativo)
    return valor if valor <= MAX_U64 else None


def formatear_metricas(evaluados: int, duracion: float) -> str:
    """segundos con 6 decimales y hps calculado con ESE mismo valor redondeado
    (si se imprime segundos=0.000000, hps=0), igual que cli_rango.h."""
    segundos = round(duracion, 6)
    hps = evaluados / segundos if segundos > 0 else 0.0
    return f"segundos={segundos:.6f} hps={hps:.0f}"


def main() -> None:
    args = sys.argv[1:]
    if len(args) != 4:
        error(f"se esperan exactamente 4 argumentos (se recibieron {len(args)}).\n"
              f"Uso: {sys.argv[0]} <cadena> <prefijo_hex> <min> <max>")

    cadena_str, prefijo_str, min_str, max_str = args

    # Validar cadena. os.fsencode devuelve los bytes EXACTOS que recibió el proceso
    # (los mismos que ven los binarios C en argv), también si no es ASCII.
    cadena_bytes = os.fsencode(cadena_str)
    if len(cadena_bytes) > CADENA_MAX_BYTES:
        error(f"la cadena base no puede superar los {CADENA_MAX_BYTES} bytes (tiene {len(cadena_bytes)}).")

    # Validar prefijo (1 a 32 caracteres hexadecimales)
    if not (1 <= len(prefijo_str) <= PREFIJO_MAX_LEN) or not re.fullmatch(r"[0-9a-fA-F]{1,32}", prefijo_str):
        error(f"el prefijo debe tener entre 1 y {PREFIJO_MAX_LEN} caracteres hexadecimales.")
    prefijo = prefijo_str.lower()

    # Validar min y max (solo dígitos, sin signos ni espacios, rango u64)
    min_val = parsear_u64(min_str)
    if min_val is None:
        error(f"min inválido \"{min_str}\" (solo dígitos, 0 a {MAX_U64}).")

    max_val = parsear_u64(max_str)
    if max_val is None:
        error(f"max inválido \"{max_str}\" (solo dígitos, 0 a {MAX_U64}).")

    if min_val > max_val:
        error(f"min ({min_val}) es mayor que max ({max_val}).")

    if min_val == 0 and max_val == MAX_U64:
        error(f"no se admite el rango completo [0, {MAX_U64}]; partirlo en sub-rangos.")

    # Búsqueda determinística secuencial del menor nonce
    inicio = time.perf_counter()
    n = min_val
    encontrado = False
    hash_encontrado = ""

    # El estado MD5 de la cadena se calcula una vez y se copia en cada intento.
    md5_base = hashlib.md5(cadena_bytes)
    while n <= max_val:
        m = md5_base.copy()
        m.update(str(n).encode("ascii"))  # el nonce en decimal sí es siempre ASCII
        h = m.hexdigest()
        if h.startswith(prefijo):
            encontrado = True
            hash_encontrado = h
            break
        n += 1

    fin = time.perf_counter()
    duracion = fin - inicio

    if encontrado:
        evaluados = n - min_val + 1
        print(f"OK nonce={n} hash={hash_encontrado} evaluados={evaluados} "
              f"{formatear_metricas(evaluados, duracion)}", flush=True)
        sys.exit(0)
    else:
        evaluados = max_val - min_val + 1
        print(f"SIN_SOLUCION evaluados={evaluados} {formatear_metricas(evaluados, duracion)}",
              flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()

