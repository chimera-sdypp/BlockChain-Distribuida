"""BC-38: bulks de 1 a 100.000 transacciones. Corre k6 (bulk.js) y mide hasta que se confirma todo.

    python3 correr.py --url http://<IP del Ingress>
    python3 correr.py --url http://localhost --tamanos 1,10,100 --repeticiones 1

Para cada tamaño y repetición:
  1. Anota las condiciones: altura de la cadena, prefijo del último bloque y mineros vivos por tipo.
  2. Corre bulk.js con k6: lado HTTP (p50, p95 y p99 de cada lote, pedidos/s, % de errores).
  3. Recorre los bloques nuevos hasta ver confirmadas todas las transacciones aceptadas de la
     corrida (se reconocen por el destino, que lleva un prefijo único por corrida): lado del sistema
     (tiempo hasta la última confirmada, bloques y transacciones por bloque).
  4. Agrega una fila a resultados.csv.

Antes de empezar comprueba que los ORIGENES tengan saldo para el bulk más grande: el NCT valida
contra el saldo confirmado menos lo comprometido, así que con los saldos por defecto (1000 cada uno)
las transacciones de más se rechazarían. Ver README.md.
Solo biblioteca estándar de Python, más k6 en el PATH.
"""

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

AQUI = Path(__file__).resolve().parent
CAMPOS = ["corrida", "inicio", "tamano", "repeticion", "vus", "lote", "lotes",
          "mineros_gpu", "mineros_cpu", "prefijo", "altura_inicial",
          "p50_ms", "p95_ms", "p99_ms", "pedidos", "pedidos_por_s", "errores_http_pct",
          "aceptadas", "segundos_envio", "confirmadas", "segundos_confirmacion",
          "bloques", "tx_por_bloque_prom", "tx_por_bloque_max", "completa"]


def lista_enteros(texto):
    return [int(x) for x in texto.split(",") if x.strip()]


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def get(self, ruta):
        with urllib.request.urlopen(self.base + ruta, timeout=15) as respuesta:
            return json.load(respuesta)

    def altura(self):
        """Altura de la punta de la cadena (-1 si está vacía) y el bloque."""
        bloques = self.get("/api/bloques?limite=1")
        return (bloques[0]["altura"], bloques[0]) if bloques else (-1, None)

    def bloques_desde(self, altura_vista):
        """Los bloques con altura > altura_vista, del más viejo al más nuevo (de a 100 por pedido)."""
        punta, _ = self.altura()
        nuevos, desde = [], punta
        while desde > altura_vista:
            limite = min(100, desde - altura_vista)
            pagina = self.get(f"/api/bloques?limite={limite}&desde={desde}")
            if not pagina:
                break
            nuevos.extend(b for b in pagina if b["altura"] > altura_vista)
            desde = pagina[-1]["altura"] - 1
        return sorted(nuevos, key=lambda b: b["altura"])

    def mineros_vivos(self):
        cuenta = {"gpu": 0, "cpu": 0}
        for minero in self.get("/api/mineros"):
            if minero.get("vivo") and minero.get("tipo") in cuenta:
                cuenta[minero["tipo"]] += 1
        return cuenta


def comprobar_saldos(api, origenes, mayor):
    """Cada origen manda ~mayor/len(origenes) transacciones de 1: tiene que tener ese saldo."""
    necesario = math.ceil(mayor / len(origenes))
    saldos = api.get("/api/saldos")
    faltan = {o: saldos.get(o, 0) for o in origenes if saldos.get(o, 0) < necesario}
    return necesario, faltan


def correr_k6(k6, opciones, cantidad, prefijo, resumen):
    entorno = dict(os.environ, BASE_URL=opciones.url, CANTIDAD=str(cantidad), VUS=str(opciones.vus),
                   LOTE=str(opciones.lote), ORIGENES=",".join(opciones.origenes),
                   PREFIJO_DESTINO=prefijo, RESUMEN=str(resumen))
    proceso = subprocess.run([k6, "run", "--quiet", str(AQUI / "bulk.js")], env=entorno,
                             capture_output=True, text=True)
    if proceso.returncode not in (0, 99):  # 99: falló algún threshold/check, igual hay métricas
        sys.exit(f"k6 terminó con código {proceso.returncode}:\n{proceso.stderr[-2000:]}")
    print("   " + proceso.stdout.strip().replace("\n", "\n   "), flush=True)
    return json.loads(Path(resumen).read_text(encoding="utf-8"))["metrics"]


def timeout_para(tamano, opciones):
    """Segundos máximos esperando la confirmación de un bulk.

    Entran como mucho `tx_por_bloque` transacciones por bloque (TX_POR_CAJA del NCT) y los bloques
    salen de a uno, así que 100.000 son 1000 bloques: el tope crece con el tamaño. Los 120 s fijos
    cubren la ventana del NCT (30 s) y el primer bloque.
    """
    if opciones.timeout:
        return opciones.timeout
    return 120 + opciones.segundos_por_bloque * math.ceil(tamano / opciones.tx_por_bloque)


def esperar_confirmacion(api, prefijo, aceptadas, altura_inicial, inicio, timeout, opciones):
    """Sigue los bloques nuevos hasta ver `aceptadas` tx de la corrida. Devuelve el resumen."""
    marca = prefijo + "-"
    confirmadas, por_bloque, vista, ultima = 0, [], altura_inicial, None
    limite = time.time() + timeout
    while confirmadas < aceptadas and time.time() < limite:
        for bloque in api.bloques_desde(vista):
            vista = bloque["altura"]
            propias = sum(1 for tx in bloque["transactions"] if str(tx.get("destino", "")).startswith(marca))
            if propias:
                confirmadas += propias
                por_bloque.append(propias)
                ultima = time.time()
        if confirmadas < aceptadas:
            time.sleep(opciones.intervalo)
    return {
        "confirmadas": confirmadas,
        "segundos_confirmacion": round(ultima - inicio, 2) if ultima else "",
        "bloques": len(por_bloque),
        "tx_por_bloque_prom": round(confirmadas / len(por_bloque), 1) if por_bloque else "",
        "tx_por_bloque_max": max(por_bloque) if por_bloque else "",
        "completa": "si" if confirmadas >= aceptadas else "no",
    }


def metrica(metricas, nombre, valor, defecto=0):
    return metricas.get(nombre, {}).get("values", {}).get(valor, defecto)


def main():
    argumentos = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    argumentos.add_argument("--url", required=True, help="base del backend por el Ingress, p. ej. http://34.1.2.3")
    argumentos.add_argument("--tamanos", type=lista_enteros, default=lista_enteros("1,10,100,1000,10000,100000"))
    argumentos.add_argument("--repeticiones", type=int, default=3)
    argumentos.add_argument("--vus", type=int, default=10, help="usuarios virtuales de k6")
    argumentos.add_argument("--lote", type=int, default=1000, help="transacciones por pedido (máximo 1000)")
    argumentos.add_argument("--origenes", type=lambda t: [o for o in t.split(",") if o], default=["alice", "bob", "carol"])
    argumentos.add_argument("--timeout", type=int, default=0,
                            help="segundos máximos esperando la confirmación (0: según el tamaño, ver --segundos-por-bloque)")
    argumentos.add_argument("--segundos-por-bloque", type=float, default=15.0,
                            help="tope de segundos por bloque para el timeout automático")
    argumentos.add_argument("--tx-por-bloque", type=int, default=100,
                            help="transacciones por bloque como mucho (TX_POR_CAJA del NCT)")
    argumentos.add_argument("--intervalo", type=float, default=1.0, help="segundos entre consultas de bloques")
    argumentos.add_argument("--pausa", type=float, default=5.0, help="segundos entre corridas")
    argumentos.add_argument("--permitir-rechazos", action="store_true",
                            help="correr aunque los orígenes no tengan saldo para el bulk más grande")
    argumentos.add_argument("--csv", type=Path, default=AQUI / "resultados.csv")
    opciones = argumentos.parse_args()

    k6 = shutil.which("k6") or sys.exit("No está k6 en el PATH (https://k6.io/docs/get-started/installation/)")
    if not 1 <= opciones.lote <= 1000:
        sys.exit("--lote tiene que estar entre 1 y 1000 (el máximo del backend)")
    api = Api(opciones.url)
    try:
        necesario, faltan = comprobar_saldos(api, opciones.origenes, max(opciones.tamanos))
    except (urllib.error.URLError, OSError) as error:
        sys.exit(f"No responde {opciones.url}/api/saldos: {error}")
    if faltan and not opciones.permitir_rechazos:
        sys.exit(f"Cada origen necesita al menos {necesario} de saldo y no alcanza: {faltan}. "
                 "Ver README.md (SALDOS_INICIALES) o usar --permitir-rechazos.")

    nuevo = not opciones.csv.exists()
    with opciones.csv.open("a", newline="") as archivo, tempfile.TemporaryDirectory() as temporal:
        escritor = csv.DictWriter(archivo, CAMPOS, lineterminator="\n")
        if nuevo:
            escritor.writeheader()
        for tamano in opciones.tamanos:
            for repeticion in range(1, opciones.repeticiones + 1):
                corrida = uuid.uuid4().hex[:6]
                prefijo = f"b38-{corrida}"
                altura_inicial, punta = api.altura()
                mineros = api.mineros_vivos()
                print(f"\n== {tamano} tx · repetición {repeticion} · corrida {corrida} · "
                      f"mineros gpu={mineros['gpu']} cpu={mineros['cpu']}", flush=True)
                inicio = time.time()
                metricas = correr_k6(k6, opciones, tamano, prefijo, Path(temporal) / f"{corrida}.json")
                envio = time.time() - inicio
                aceptadas = int(metrica(metricas, "tx_aceptadas", "count"))
                timeout = timeout_para(tamano, opciones)
                sistema = esperar_confirmacion(api, prefijo, aceptadas, altura_inicial, inicio, timeout, opciones)
                fila = {
                    "corrida": corrida, "inicio": datetime.fromtimestamp(inicio, timezone.utc).isoformat(timespec="seconds"),
                    "tamano": tamano, "repeticion": repeticion, "vus": opciones.vus, "lote": opciones.lote,
                    "lotes": math.ceil(tamano / opciones.lote), "mineros_gpu": mineros["gpu"], "mineros_cpu": mineros["cpu"],
                    "prefijo": (punta or {}).get("prefijo", ""), "altura_inicial": altura_inicial,
                    "p50_ms": round(metrica(metricas, "http_req_duration", "p(50)"), 1),
                    "p95_ms": round(metrica(metricas, "http_req_duration", "p(95)"), 1),
                    "p99_ms": round(metrica(metricas, "http_req_duration", "p(99)"), 1),
                    "pedidos": int(metrica(metricas, "http_reqs", "count")),
                    "pedidos_por_s": round(metrica(metricas, "http_reqs", "rate"), 2),
                    "errores_http_pct": round(100 * metrica(metricas, "http_req_failed", "rate"), 2),
                    "aceptadas": aceptadas, "segundos_envio": round(envio, 2), **sistema,
                }
                escritor.writerow(fila)
                archivo.flush()
                print(f"   sistema: {fila['confirmadas']}/{aceptadas} confirmadas en {fila['segundos_confirmacion']} s, "
                      f"{fila['bloques']} bloque(s), {fila['tx_por_bloque_prom']} tx/bloque"
                      + ("" if fila["completa"] == "si" else f"  <- INCOMPLETA (timeout de {timeout:.0f} s)"), flush=True)
                time.sleep(opciones.pausa)
    print(f"\nResultados en {opciones.csv}")


if __name__ == "__main__":
    main()
