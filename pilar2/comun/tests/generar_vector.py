"""Genera comun/vector_prueba.json (contrato v4). Se corre una vez y el resultado se versiona.

    python -m comun.tests.generar_vector > comun/vector_prueba.json

El vector lo verifican igual Python (comun/tests/test_contrato.py), CUDA (pilar1/hit4/md5), `md5sum` y la billetera
del navegador (BC-84). Se busca un timestamp de la tarea con el que el primer nonce con prefijo 0000 caiga entre
54.000 y 54.999: así siguen valiendo los rangos que usan los tests de los mineros.
"""

import json

from comun import contrato, estado
from comun.tests.claves import CUENTA, PRIVADA

GENESIS_TS = 1793836800
SALDOS = {CUENTA["alice"]: 1000, CUENTA["bob"]: 1000}
NOMBRES = {CUENTA["alice"]: "alice", CUENTA["bob"]: "bob"}
PREFIJO_MINIMO = 4  # v4: el piso del génesis; el bloque del vector se mina con 0000


def primer_nonce(texto, prefijo, hasta):
    return next((n for n in range(hasta) if contrato.hash_bloque(texto, n).startswith(prefijo)), None)


def generar():
    genesis = contrato.bloque_genesis(SALDOS, GENESIS_TS, organizador=CUENTA["organizador"], nombres=NOMBRES,
                                      prefijo_minimo=PREFIJO_MINIMO)
    cadena = genesis["block_hash"]  # v4: lo firmado lleva el hash del génesis
    despues = estado.Estado()
    problemas, _ = estado.aplicar_bloque(despues, genesis, estado.Reglas(organizador=CUENTA["organizador"]))
    assert not problemas, problemas
    transacciones = [contrato.transaccion(PRIVADA["alice"], CUENTA["bob"], 250, 1, 1793836830, cadena=cadena),
                     contrato.transaccion(PRIVADA["bob"], CUENTA["carol"], 75, 1, 1793836841, cadena=cadena),
                     contrato.alta(PRIVADA["organizador"], 1, CUENTA["carol"], "usuario", "carol", 1793836845,
                                   cadena=cadena)]
    for timestamp in range(1793836860, 1793836860 + 100_000):
        tarea = {"previous_hash": genesis["block_hash"], "timestamp": timestamp, "altura": 1, "prefijo": "0000",
                 "transacciones": transacciones, "raiz_estado": despues.raiz()}
        texto = contrato.texto_para(tarea, CUENTA["minero"])
        nonce = primer_nonce(texto, "0000", 55_000)
        if nonce is not None and nonce >= 54_000:
            break
    del_bloque = contrato.transacciones_del_bloque(tarea, CUENTA["minero"])
    bloque = {"previous_hash": tarea["previous_hash"], "timestamp": timestamp, "transactions": del_bloque,
              "nonce": nonce, "block_hash": contrato.hash_bloque(texto, nonce), "prefijo": "0000", "altura": 1,
              "raiz_estado": despues.raiz()}
    tras_bloque = despues.copia()
    problemas, _ = estado.aplicar_bloque(tras_bloque, bloque, estado.Reglas(organizador=CUENTA["organizador"]))
    assert not problemas, problemas
    return {
        "descripcion": "Contrato v4. El mismo texto + nonce tiene que dar el mismo MD5 en Python, CUDA y la billetera "
                       "del navegador. Lo firmado lleva adelante el hash del génesis (`cadena`). Claves: "
                       "comun/tests/claves.py. Generado con comun/tests/generar_vector.py",
        "cadena": cadena,
        "mensaje_transferencia": contrato.mensaje_transaccion(transacciones[0], cadena),
        "mensaje_alta": contrato.mensaje_acta(transacciones[2], cadena),
        "cuentas": {n: CUENTA[n] for n in ("alice", "bob", "carol", "minero", "organizador")},
        "genesis": {"saldos": SALDOS, "nombres": NOMBRES, "organizador": CUENTA["organizador"],
                    "prefijo_minimo": PREFIJO_MINIMO, "timestamp": GENESIS_TS, "block_hash": genesis["block_hash"],
                    "bloque": genesis},
        "estado_genesis": {"hojas": [hoja for _, hoja in despues.hojas()], "raiz": despues.raiz(),
                           "prueba_alice": despues.prueba(CUENTA["alice"])},
        "tarea": tarea,
        "minero": CUENTA["minero"],
        "coinbase": del_bloque[0],
        "raiz_transacciones": contrato.raiz_transacciones(del_bloque),
        "raiz": contrato.raiz_bloque(del_bloque, despues.raiz()),
        "texto": texto,
        "nonce": nonce,
        "block_hash": bloque["block_hash"],
        "bloque": bloque,
        "estado_bloque": {"hojas": [hoja for _, hoja in tras_bloque.hojas()], "raiz": tras_bloque.raiz(),
                          "saldos": tras_bloque.saldos},
        "texto_de_alice": contrato.texto_para(tarea, CUENTA["alice"]),
    }


if __name__ == "__main__":
    print(json.dumps(generar(), indent=1, ensure_ascii=False))
