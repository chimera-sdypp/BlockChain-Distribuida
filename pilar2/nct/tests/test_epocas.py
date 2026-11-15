"""BC-63: al empezar cada época, el NCT paga la participación por shares de la anterior."""

import json

from comun import contrato
from comun.tests.claves import CUENTA
from nct.tests.escenario import minar, recibida, saldo

FIN_DE_EPOCA = 1793836900  # el timestamp del último bloque de la época que se paga


def hasta_la_altura(redis, nct, altura):
    """Génesis y bloques de relleno hasta que la tarea siguiente sea la de `altura`."""
    nct.cargar_genesis()
    for n in range(1, altura):
        redis.rpush("cadena", f"bloque-{n}")
    redis.hset(f"bloque:bloque-{altura - 1}", "timestamp", FIN_DE_EPOCA)


def shares(redis, epoca, **por_minero):
    for nombre, n in por_minero.items():
        redis.zincrby(f"shares:{epoca}", n, CUENTA[nombre])


def tarea_de(redis, nct, publicador):
    nct.validar(recibida(redis, "alice", "bob", 1))
    nct.cerrar_cajas()
    id_tarea = nct.armar_si_libre()
    [*_, tarea] = publicador.tareas()
    assert tarea["tarea"] == id_tarea
    return tarea


def pagos(tarea):
    return {tx["destino"]: tx["monto"] for tx in tarea["transacciones"] if tx["id"].startswith("part-")}


def test_el_primer_bloque_de_la_epoca_paga_la_anterior_segun_los_shares(armar, redis, publicador):
    nct = armar(fondo_epoca=100)
    hasta_la_altura(redis, nct, 10)
    shares(redis, 0, minero=30, minero2=10)

    tarea = tarea_de(redis, nct, publicador)

    assert tarea["altura"] == 10
    primeras = tarea["transacciones"][:2]
    assert primeras == [contrato.participacion(0, m, monto, FIN_DE_EPOCA)
                        for m, monto in sorted({CUENTA["minero"]: 75, CUENTA["minero2"]: 25}.items())]
    assert tarea["transacciones"][2]["origen"] == CUENTA["alice"]  # después, las de la caja
    detalle = json.loads(redis.hget("epoca:0", "detalle"))
    assert detalle[CUENTA["minero"]] == {"shares": 30, "racha": 1, "monto": 75}
    assert redis.hget("epoca:0", "fondo") == "100"


def test_en_medio_de_una_epoca_no_se_paga_nada(armar, redis, publicador):
    nct = armar()
    hasta_la_altura(redis, nct, 7)
    shares(redis, 0, minero=30)
    assert pagos(tarea_de(redis, nct, publicador)) == {}


def test_sin_shares_en_la_epoca_no_hay_pagos(armar, redis, publicador):
    nct = armar()
    hasta_la_altura(redis, nct, 10)
    assert pagos(tarea_de(redis, nct, publicador)) == {}


def test_la_racha_suma_10_por_ciento_por_epoca_seguida_hasta_50(armar, redis, publicador):
    nct = armar(fondo_epoca=1000)
    hasta_la_altura(redis, nct, 60)  # se paga la época 5
    for epoca in range(6):
        shares(redis, epoca, minero=10)  # 6 épocas seguidas: +50 %
    shares(redis, 5, minero2=10)         # recién llega: sin bonus

    tarea = tarea_de(redis, nct, publicador)

    # pesos 10 × 1,5 = 15 y 10 × 1 = 10: 600 y 400 de 1000
    assert pagos(tarea) == {CUENTA["minero"]: 600, CUENTA["minero2"]: 400}
    detalle = json.loads(redis.hget("epoca:5", "detalle"))
    assert (detalle[CUENTA["minero"]]["racha"], detalle[CUENTA["minero2"]]["racha"]) == (6, 1)


def test_una_epoca_sin_shares_corta_la_racha(armar, redis, publicador):
    nct = armar(fondo_epoca=1000)
    hasta_la_altura(redis, nct, 40)  # se paga la época 3
    for epoca in (0, 1, 3):
        shares(redis, epoca, minero=10)
    tarea_de(redis, nct, publicador)
    assert json.loads(redis.hget("epoca:3", "detalle"))[CUENTA["minero"]]["racha"] == 1


def test_el_pago_entra_con_el_bloque_y_suma_al_historial(armar, redis, publicador):
    nct = armar(fondo_epoca=100)
    hasta_la_altura(redis, nct, 10)
    shares(redis, 0, minero2=5)
    tarea = tarea_de(redis, nct, publicador)
    redis.hset(f"tarea:{tarea['tarea']}", "prefijo", "0")

    assert nct.procesar_solucion(minar(redis, tarea["tarea"])) == "confirmada"

    assert saldo(redis, "minero2") == 100
    assert redis.hget(f"tx:part-0-{CUENTA['minero2']}", "estado") == "confirmada"
    assert redis.hget(f"historial:{CUENTA['minero2']}", "recompensas") == "100"


def test_rearmar_la_tarea_da_los_mismos_pagos(armar, redis, publicador):
    nct = armar()
    hasta_la_altura(redis, nct, 10)
    shares(redis, 0, minero=3, minero2=1)
    primera = tarea_de(redis, nct, publicador)

    nct.procesar_solucion({"tarea": primera["tarea"], "sin_solucion": True})

    [*_, segunda] = publicador.tareas()
    assert segunda["tarea"] != primera["tarea"]
    assert [tx for tx in segunda["transacciones"] if tx["id"].startswith("part-")] == \
        [tx for tx in primera["transacciones"] if tx["id"].startswith("part-")]


def test_una_epoca_no_se_paga_dos_veces(armar, redis, publicador):
    """Si el pago de una época ya estuviera en la cadena, el consenso descarta el bloque que lo repite
    y la tarea que se vuelve a armar ya no lo trae."""
    nct = armar()
    hasta_la_altura(redis, nct, 10)
    shares(redis, 0, minero=3)
    tarea = tarea_de(redis, nct, publicador)
    redis.hset(f"tx:part-0-{CUENTA['minero']}", mapping={"estado": "confirmada"})  # ya pagada
    redis.hset(f"tarea:{tarea['tarea']}", "prefijo", "0")

    assert nct.procesar_solucion(minar(redis, tarea["tarea"])) == "descartada"

    assert saldo(redis, "minero") == 0
    [*_, otra] = publicador.tareas()
    assert otra["tarea"] != tarea["tarea"] and pagos(otra) == {}
