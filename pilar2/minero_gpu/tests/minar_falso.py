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
    corto, shares = (prefijo[:-1] if len(prefijo) >= 2 else None), []  # como minar.cu: hasta 64 por pedido
    base = hashlib.md5(texto)
    for nonce in range(desde, hasta + 1):
        h = base.copy()
        h.update(str(nonce).encode())
        digest = h.hexdigest()
        if corto and digest.startswith(corto) and len(shares) < 64:
            shares.append(nonce)
        if digest.startswith(prefijo):
            salida.write(f"OK nonce={nonce} hash={digest} evaluados={nonce - desde + 1} segundos=0.1 hps=1 "
                         f"shares={','.join(map(str, shares))}\n")
            break
    else:
        salida.write(f"SIN_SOLUCION evaluados={hasta - desde + 1} segundos=0.1 hps=1 shares={','.join(map(str, shares))}\n")
    salida.flush()
