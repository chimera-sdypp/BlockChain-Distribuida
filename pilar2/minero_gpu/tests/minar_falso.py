#!/usr/bin/env python3
"""El protocolo de minar.cu, con hashlib: para probar el minero GPU donde no hay GPU (el CI)."""

import hashlib
import sys

entrada, salida = sys.stdin.buffer, sys.stdout
salida.write("LISTO GPU de mentira\n")
salida.flush()
while linea := entrada.readline():
    prefijo, desde, hasta, largo = linea.split()
    texto = entrada.read(int(largo))
    if prefijo == b"muere":
        sys.exit(1)
    desde, hasta, prefijo = int(desde), int(hasta), prefijo.decode()
    base = hashlib.md5(texto)
    for nonce in range(desde, hasta + 1):
        h = base.copy()
        h.update(str(nonce).encode())
        if h.hexdigest().startswith(prefijo):
            salida.write(f"OK nonce={nonce} hash={h.hexdigest()} evaluados={nonce - desde + 1} segundos=0.1 hps=1\n")
            break
    else:
        salida.write(f"SIN_SOLUCION evaluados={hasta - desde + 1} segundos=0.1 hps=1\n")
    salida.flush()
