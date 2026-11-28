"""Las cuentas con que firman las pruebas de carga (BC-91).

En GKE el génesis va vacío: las pruebas usan cuentas propias, con las privadas en el Secret `cuentas-prueba` del
namespace blockchain (nunca en el repo). Les da el alta y les carga saldo el organizador, como a cualquier cuenta.
En k3s y en los tests, sin ese Secret, son las de prueba alice, bob y carol (pilar2/comun/tests/claves.py).

    python3 pilar3/pruebas/cuentas.py generar [N]       # el Secret con N cuentas (3); no pisa uno que ya existe
    pilar2/.venv/bin/python pilar3/pruebas/cuentas.py fondear organizador.clave --url https://… [--saldo 1000000000]
    pilar2/.venv/bin/python pilar3/pruebas/cuentas.py mostrar --url https://…

`generar` sólo necesita Python y kubectl (lo corre el workflow «Pilar 3 · Preparar prueba»). `fondear` lo corre el
organizador en su PC: su privada no sale de ahí. Después de reiniciar la cadena hay que volver a fondear (el Secret
queda; las altas y los saldos eran de la cadena vieja).

Desde los scripts de pruebas:

    from cuentas import CUENTA, PRIVADA, ORIGENES   # ORIGENES: los nombres de las cuentas con saldo

Las privadas salen, en orden, de CUENTAS_PRUEBA (un archivo JSON {nombre: privada}), del Secret (con el kubectl del
contexto actual) o, si no hay ninguno, de las de prueba. Un nombre que no está (un destino, «organizador»…) es el de
prueba.
"""

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

PILAR2 = Path(__file__).resolve().parents[2] / "pilar2"
NS = os.environ.get("NS", "blockchain")
SECRET = "cuentas-prueba"
DE_PRUEBA = ("alice", "bob", "carol")


def leer_secret():
    """{nombre: privada} del Secret, o None si no está o no hay kubectl."""
    try:
        salida = subprocess.run(["kubectl", "-n", NS, "get", "secret", SECRET, "-o", "json"],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if salida.returncode != 0:
        return None
    datos = json.loads(salida.stdout).get("data") or {}
    return {nombre: base64.b64decode(valor).decode().strip() for nombre, valor in sorted(datos.items())}


def privadas_propias():
    """(privadas, de dónde salieron); privadas es None si hay que usar las de prueba."""
    archivo = os.environ.get("CUENTAS_PRUEBA")
    if archivo:
        return json.loads(Path(archivo).read_text()), f"el archivo {archivo}"
    secreto = leer_secret()
    if secreto:
        return secreto, f"el Secret {NS}/{SECRET}"
    return None, "las de prueba de comun/tests/claves.py"


class _ConRespaldo(dict):
    """Las propias y, para cualquier otro nombre, la de prueba."""

    def __init__(self, propias, respaldo):
        super().__init__(propias)
        self._respaldo = respaldo

    def __missing__(self, nombre):
        return self._respaldo[nombre]


def _cargar():
    sys.path.insert(0, str(PILAR2))
    from comun import contrato
    from comun.tests import claves

    propias, fuente = privadas_propias()
    if propias:
        cuentas = {nombre: contrato.clave_publica(privada) for nombre, privada in propias.items()}
        globals().update(PRIVADA=_ConRespaldo(propias, claves.PRIVADA), CUENTA=_ConRespaldo(cuentas, claves.CUENTA),
                         ORIGENES=tuple(propias))
    else:
        globals().update(PRIVADA=claves.PRIVADA, CUENTA=claves.CUENTA, ORIGENES=DE_PRUEBA)
    globals()["FUENTE"] = fuente
    print(f"Cuentas de las pruebas: {', '.join(ORIGENES)} (de {fuente})", file=sys.stderr)  # noqa: F821


def __getattr__(nombre):
    if nombre in ("CUENTA", "PRIVADA", "ORIGENES", "FUENTE"):
        _cargar()
        return globals()[nombre]
    raise AttributeError(f"module {__name__!r} has no attribute {nombre!r}")


def generar(cantidad, forzar=False):
    if cantidad < 2:
        sys.exit("Hacen falta al menos dos cuentas: las pruebas mandan de una a otra")
    if leer_secret() and not forzar:
        sys.exit(f"El Secret {NS}/{SECRET} ya existe: sus cuentas pueden tener saldo. Para cambiarlas, --forzar.")
    privadas = {f"carga-{i}": secrets.token_hex(32) for i in range(1, cantidad + 1)}  # Ed25519: 32 bytes al azar
    secreto = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
               "metadata": {"name": SECRET, "labels": {"app": "pruebas"}}, "stringData": privadas}
    # Por stdin: las privadas no quedan en los argumentos (ni en el registro de auditoría).
    if subprocess.run(["kubectl", "-n", NS, "apply", "-f", "-"], input=json.dumps(secreto), text=True).returncode:
        sys.exit(f"kubectl no pudo crear el Secret {NS}/{SECRET} (¿el contexto apunta al cluster?)")
    print(f"Cuentas {', '.join(privadas)} en el Secret {NS}/{SECRET}. Falta que el organizador las fondee:")
    print("  pilar2/.venv/bin/python pilar3/pruebas/cuentas.py fondear organizador.clave --url https://…")


def fondear(archivo_organizador, url, saldo):
    sys.path.insert(0, str(PILAR2))
    from claves.herramienta import Backend, aprobar, leer_privada

    propias, fuente = privadas_propias()
    if not propias:
        sys.exit(f"No hay cuentas propias (ni CUENTAS_PRUEBA ni el Secret {NS}/{SECRET}): primero `generar`.")
    backend, organizador, resultado = Backend(url), leer_privada(archivo_organizador), 0
    from comun import contrato
    for nombre, privada in propias.items():
        cuenta = contrato.clave_publica(privada)
        if backend.cuenta(cuenta).get("aprobada"):
            print(f"{nombre} ({cuenta[:12]}…): ya tiene alta en esta cadena")
            continue
        print(f"{nombre} ({cuenta[:12]}…): alta y carga de {saldo}")
        resultado |= aprobar(backend, organizador, cuenta, "usuario", nombre, carga=saldo)
    return resultado


def mostrar(url):
    sys.path.insert(0, str(PILAR2))
    from claves.herramienta import Backend
    from comun import contrato

    propias, fuente = privadas_propias()
    print(f"De {fuente}")
    backend = Backend(url)
    for nombre, privada in (propias or {}).items():
        cuenta = contrato.clave_publica(privada)
        datos = backend.cuenta(cuenta)
        print(f"  {nombre}: {cuenta}  saldo {datos.get('saldo', 0)}, nonce {datos.get('nonce', 0)}, "
              f"{'aprobada' if datos.get('aprobada') else 'sin alta'}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="accion", required=True)
    p = sub.add_parser("generar", help=f"crea el Secret {SECRET} con N cuentas nuevas")
    p.add_argument("cantidad", nargs="?", type=int, default=3)
    p.add_argument("--forzar", action="store_true", help="pisa un Secret que ya existe (sus cuentas quedan sin uso)")
    p = sub.add_parser("fondear", help="organizador: alta y carga de saldo de cada cuenta del Secret")
    p.add_argument("organizador", help="el archivo con la privada del organizador")
    p.add_argument("--url", required=True)
    p.add_argument("--saldo", type=int, default=10**9, help="la carga de cada cuenta (1.000.000.000)")
    p = sub.add_parser("mostrar", help="las cuentas, su saldo y si tienen alta")
    p.add_argument("--url", required=True)
    a = parser.parse_args(argv)
    if a.accion == "generar":
        generar(a.cantidad, a.forzar)
    elif a.accion == "fondear":
        return fondear(a.organizador, a.url, a.saldo)
    else:
        mostrar(a.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
