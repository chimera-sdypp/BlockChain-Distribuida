"""Prueba de ajuste de dificultad (BC-60): convergencia desde prefijo bajo y caída sin GPU.

    python pilar3/pruebas/dificultad.py --api http://<traefik> --gateway http://<IP>:8081 --salida resultados/dificultad.csv

Arranca el TrP con un prefijo bajo (`PREFIJO_INICIAL`, p. ej. 2), va midiendo cómo sube cada
`VENTANA_AJUSTE` bloques hasta converger, y opcionalmente espera a que se saque la GPU (`--esperar-gpu`)
para registrar cómo baja el prefijo (desde BC-73: en el acto si la potencia cae 4 veces o más, o por la
bajada de emergencia a los 240 s sin bloque).

Ojo: `PREFIJO_INICIAL` sólo vale con la cadena sin bloques minados (recién creada o reiniciada). Con una
cadena que ya tiene bloques, el ajuste sigue desde el prefijo del último: para ver la subida sin reiniciar
la cadena está el escenario `bench-bajo` de `trampas.py` (BC-76).
Al terminar (o con Ctrl+C) restaura los valores por defecto del TrP.
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timezone

from firmar import CUENTA, PRIVADA, contrato
from prefijos import api, gateway_url, kubectl, medir, vivos

COLUMNAS = ["bloque", "altura", "fase", "prefijo", "segundos", "rearmados", "minero",
            "gpus_vivas", "cpus_vivas", "block_hash", "publicada"]


def configurar_trp(inicial, base, esperado, ventana):
    # BC-73: el TrP no arranca si ESPACIO_NONCES < 16^PREFIJO_BASE × 4 (con 2^32 alcanza hasta el 6).
    if inicial is None:
        kubectl("set", "env", "deploy/trp", "PREFIJO_BASE=6", "SEGUNDOS_POR_BLOQUE=60", "ESPACIO_NONCES=4294967296",
                "VENTANA_AJUSTE=5", "PREFIJO_INICIAL-", "AJUSTAR_PREFIJO-")
    else:
        kubectl("set", "env", "deploy/trp", f"PREFIJO_BASE={base}", f"SEGUNDOS_POR_BLOQUE={esperado}",
                f"ESPACIO_NONCES={max(2 ** 32, 16 ** base * 4)}", f"VENTANA_AJUSTE={ventana}",
                f"PREFIJO_INICIAL={inicial}", "AJUSTAR_PREFIJO=1")
    kubectl("rollout", "status", "deploy/trp", "--timeout=300s")


def ultimo_bloque(base):
    bloques = api(base, "/api/bloques?limite=1")
    return bloques[0] if bloques else None


def mandar_y_medir(base, maximo, ventana):
    previo = ultimo_bloque(base)
    antes = previo["block_hash"] if previo else None
    desde = datetime.now(timezone.utc).replace(microsecond=0)
    time.sleep(1)
    nonce = api(base, f"/api/cuentas/{CUENTA['alice']}")["nonce"] + 1
    api(base, "/api/transacciones", contrato.transaccion(PRIVADA["alice"], CUENTA["bob"], 1, nonce))
    limite = time.time() + ventana + maximo + 15
    while time.time() < limite:
        time.sleep(1)
        actual = ultimo_bloque(base)
        if actual and actual["block_hash"] != antes:
            medido = medir(desde, actual["block_hash"])
            if medido is None:
                sys.exit(f"el bloque {actual['block_hash']} no aparece en los logs del NCT")
            return actual, medido
    return None


def registrar(escritor, archivo, numero, fase, actual, medido, gateway):
    segundos, rearmados, minero, publicada = medido
    # La duración la mide el NCT (desde que se armó la caja, con los rearmados: BC-73): los logs pueden confundir la
    # publicación de una tarea con la de la anterior si caen en el mismo segundo.
    if actual.get("duracion") not in (None, ""):
        segundos = float(actual["duracion"])
    gpus, cpus = vivos(gateway)  # los mineros se listan en el gateway (BC-54), no en la API de la app
    prefijo = len(actual.get("prefijo", ""))
    escritor.writerow({
        "bloque": numero,
        "altura": actual["altura"],
        "fase": fase,
        "prefijo": prefijo,
        "segundos": round(segundos, 3),
        "rearmados": rearmados,
        "minero": minero,
        "gpus_vivas": gpus,
        "cpus_vivas": cpus,
        "block_hash": actual["block_hash"],
        "publicada": publicada.isoformat(timespec="milliseconds"),
    })
    archivo.flush()
    print(f"   [{fase}] bloque {numero} (altura {actual['altura']}): "
          f"prefijo={prefijo}, {segundos:6.2f} s, gpu={gpus}, cpu={cpus}", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--api", required=True, help="URL de Traefik (frontend y API)")
    p.add_argument("--gateway", help="URL del gateway, que lista los mineros (GET /mineros); por defecto GATEWAY_URL")
    p.add_argument("--salida", required=True, help="CSV de resultados")
    p.add_argument("--inicial", type=int, default=2, help="PREFIJO_INICIAL para ver la convergencia")
    p.add_argument("--base", type=int, default=8, help="PREFIJO_BASE máximo durante la prueba")
    p.add_argument("--esperado", type=int, default=60, help="SEGUNDOS_POR_BLOQUE")
    p.add_argument("--ventana", type=int, default=5, help="VENTANA_AJUSTE")
    p.add_argument("--bloques-subida", type=int, default=20, help="bloques en fase de convergencia")
    p.add_argument("--bloques-sin-gpu", type=int, default=5, help="bloques después de sacar la GPU")
    p.add_argument("--maximo", type=int, default=300, help="segundos máximos por bloque")
    p.add_argument("--ventana-nct", type=int, default=30, help="VENTANA_SEGUNDOS del NCT")
    p.add_argument("--esperar-gpu", action="store_true",
                   help="pausa tras la fase de subida para desconectar manualmente la GPU y seguir midiendo")
    args = p.parse_args()
    base = args.api.rstrip("/")
    gateway = gateway_url(args.gateway)

    with open(args.salida, "w", newline="") as archivo:
        escritor = csv.DictWriter(archivo, COLUMNAS)
        escritor.writeheader()
        try:
            print(f"== configurando TrP: inicial={args.inicial}, base={args.base}, "
                  f"esperado={args.esperado}s, ventana={args.ventana}", flush=True)
            configurar_trp(args.inicial, args.base, args.esperado, args.ventana)
            numero = 0
            for _ in range(args.bloques_subida):
                numero += 1
                res = mandar_y_medir(base, args.maximo, args.ventana_nct)
                if res is None:
                    print(f"   bloque {numero}: sin solución en {args.maximo} s; corto", flush=True)
                    return
                registrar(escritor, archivo, numero, "convergencia", *res, gateway)

            if args.esperar_gpu:
                input("\n>> Desconectá la GPU, esperá ~20 s (VIVO_SEGUNDOS) y apretá Enter para seguir… ")
                for _ in range(args.bloques_sin_gpu):
                    numero += 1
                    res = mandar_y_medir(base, args.maximo, args.ventana_nct)
                    if res is None:
                        print(f"   bloque {numero}: sin solución en {args.maximo} s; corto", flush=True)
                        return
                    registrar(escritor, archivo, numero, "sin_gpu", *res, gateway)
        finally:
            print("== restaurando configuración del TrP", flush=True)
            configurar_trp(None, None, None, None)


if __name__ == "__main__":
    main()
