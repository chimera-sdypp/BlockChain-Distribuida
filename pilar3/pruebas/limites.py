"""Los límites de BC-94 contra un sistema corriendo (k3s o GKE): lo que el profesor probó y lo que se agregó.

    pilar2/.venv/bin/python pilar3/pruebas/limites.py --sistema http://192.168.100.21          # k3s
    pilar2/.venv/bin/python pilar3/pruebas/limites.py --sistema https://…workers.dev            # GKE (kubectl en GKE)

Lee Redis con `kubectl exec` en el namespace `blockchain` del contexto actual (KUBECONFIG). Comprueba:

1. Una clave recién generada transfiere: 403 y su tx:<id> no existe en Redis (la prueba del profesor).
2. Una transferencia que el NCT rechaza (la primera cuenta de cuentas.py, sin saldo para el monto) vence: TTL entre 1 y 3600.
3. Un POST de 1 MB + 1 a /api da 413 (Traefik, antes del backend).
4. Muchos GET seguidos no reciben 429: el límite es sólo para los POST.
5. Una ráfaga de POST a /api/transacciones/lote más grande que el burst recibe 429.

Sale con 1 si alguno falla. La ráfaga gasta el burst del lote de esta IP: después hay que esperar unos segundos.
"""

import argparse
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pilar2"))

from claves.herramienta import Backend  # noqa: E402
from comun import contrato  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cuentas import CUENTA, ORIGENES, PRIVADA  # noqa: E402

GUARDA = 3600
CUERPO_MAXIMO = 1024 * 1024
BURST_LOTE = 20


def redis(*argumentos):
    """Un comando de redis-cli contra el primario (lo dice Sentinel), por kubectl exec."""
    def k(*a):
        return subprocess.run(["kubectl", "-n", "blockchain", "exec", "redis-0", *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    primario = k("-c", "sentinel", "--", "redis-cli", "-p", "26379", "sentinel", "get-master-addr-by-name",
                 "mymaster").splitlines()[0]
    return k("-c", "redis", "--", "redis-cli", "-h", primario, *argumentos)


def crudo(backend, ruta, cuerpo):
    """POST de bytes tal cual: el código HTTP. Con curl, que lee la respuesta mientras manda: Traefik contesta 413 y
    corta antes de recibir todo el cuerpo, y urllib sólo vería la conexión cortada."""
    salida = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST", "--data-binary", "@-",
                             "-H", "Content-Type: application/json", "-A", "chimera-limites", backend.url + ruta],
                            input=cuerpo, capture_output=True, timeout=60)
    return int(salida.stdout or 0)


def clave_nueva(backend):
    privada, _ = contrato.nueva_clave()
    tx = contrato.transaccion(privada, CUENTA[ORIGENES[1]], 1, 1, cadena=backend.cadena())
    codigo, cuerpo = backend.mandar(tx)
    existe = redis("EXISTS", f"tx:{tx['id']}")
    return codigo == 403 and existe == "0", f"{codigo} {cuerpo}, EXISTS tx:{tx['id'][:12]}… = {existe}"


def rechazada_vence(backend):
    datos = backend.cuenta(CUENTA[ORIGENES[0]])
    tx = contrato.transaccion(PRIVADA[ORIGENES[0]], CUENTA[ORIGENES[1]], int(datos["saldo"]) + 1, datos["nonce"] + 1,
                              cadena=backend.cadena())
    codigo, cuerpo = backend.mandar(tx)
    if codigo != 202:
        return False, f"{ORIGENES[0]} no pudo mandar la transferencia: {codigo} {cuerpo}"
    limite = time.monotonic() + 60
    while time.monotonic() < limite and (backend.estado(tx["id"]) or {}).get("estado") != "rechazada":
        time.sleep(1)
    estado = backend.estado(tx["id"]) or {}
    ttl = int(redis("TTL", f"tx:{tx['id']}"))
    return estado.get("estado") == "rechazada" and 0 < ttl <= GUARDA, \
        f"{estado.get('estado')} ({estado.get('motivo')}), TTL {ttl}"


def cuerpo_grande(backend):
    codigo = crudo(backend, "/api/transacciones", b"[" + b" " * CUERPO_MAXIMO + b"]")
    return codigo == 413, str(codigo)


def get_sin_limite(backend, cantidad=300):
    with ThreadPoolExecutor(max_workers=30) as hilos:
        codigos = list(hilos.map(lambda _: backend.pedir("GET", "/api/transacciones/no-existe")[0], range(cantidad)))
    return 429 not in codigos, f"{cantidad} GET: {sorted(set(codigos))}"


def rafaga_lote(backend, cantidad=4 * BURST_LOTE):
    with ThreadPoolExecutor(max_workers=cantidad) as hilos:
        codigos = list(hilos.map(lambda _: crudo(backend, "/api/transacciones/lote", b"[]"), range(cantidad)))
    return codigos.count(429) > 0, f"{cantidad} POST a /lote: {codigos.count(429)} con 429, el resto {sorted(set(codigos) - {429})}"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sistema", default="http://192.168.100.21", help="la URL del sistema (el Ingress)")
    a = p.parse_args()
    backend = Backend(a.sistema, timeout=30)
    fallas = 0
    for nombre, prueba in (("clave nueva → 403 y nada en Redis", clave_nueva),
                           ("la rechazada vence", rechazada_vence),
                           ("1 MB + 1 → 413", cuerpo_grande),
                           ("los GET no tienen límite", get_sin_limite),
                           ("ráfaga al lote → 429", rafaga_lote)):
        bien, detalle = prueba(backend)
        fallas += not bien
        print(f"{'OK   ' if bien else 'FALLA'} {nombre}: {detalle}")
    sys.exit(1 if fallas else 0)


if __name__ == "__main__":
    main()
