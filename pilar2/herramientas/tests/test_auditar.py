"""La auditoría sobre una cadena de verdad: la arma el NCT con cargas, transferencias, coinbases y participación."""

import json

import pytest

from comun import contrato
from comun.tests.claves import CUENTA, Billetera, carga
from herramientas import auditar
from nct.tests.escenario import guardar_recibida, minar

MINEROS = ("minero", "minero2")


def cadena(redis, nct, bloques):
    """Mina `bloques` bloques con el NCT: en cada uno una carga o transferencias, y shares para la participación."""
    nct.cargar_genesis()
    billetera, cargas = Billetera(), 0
    for altura in range(1, bloques + 1):
        if altura % 7 == 1:
            cargas += 1
            nct.validar(guardar_recibida(redis, carga(cargas, "dave", 50)))
        else:
            for origen, destino in (("alice", "bob"), ("bob", "carol")):
                nct.validar(guardar_recibida(redis, billetera(origen, destino, 1 + altura % 3)))
        nct.cerrar_cajas()
        id_tarea = nct.armar_si_libre()
        redis.hset(f"tarea:{id_tarea}", "prefijo", "0")
        minero = CUENTA[MINEROS[altura % 2]]
        redis.zincrby(f"shares:{contrato.epoca(altura)}", 1 + altura % 4, minero)  # lo que haría el gateway
        assert nct.procesar_solucion(minar(redis, id_tarea, minero=minero)) == "confirmada"
    return redis.lrange("cadena", 0, -1)


@pytest.fixture
def larga(armar, redis):
    nct = armar(fondo_epoca=100)
    hashes = cadena(redis, nct, 52)
    return nct, hashes


def test_una_cadena_de_52_bloques_da_todo_igual(redis, larga):
    informe = auditar.auditar(redis, CUENTA["organizador"])

    assert informe.ok, informe.problemas
    assert informe.bloques == 53 and informe.avisos == []
    assert informe.tipos["coinbase"] == 52 and informe.tipos["carga"] == 8
    assert informe.tipos["participación"] == 10  # épocas 0 a 4, dos mineros cada una
    assert informe.tipos["shares"] == 5          # y la lista de cada una, comprometida en la cadena (BC-82)
    assert informe.tipos["transferencia"] == 88 and informe.tipos["génesis"] == 3 and informe.tipos["organizador"] == 1
    assert informe.saldos == {c: int(s) for c, s in redis.hgetall("saldos").items() if int(s)}


def test_un_saldo_cambiado_a_mano_en_redis_se_detecta(redis, larga):
    redis.hincrby("saldos", CUENTA["carol"], 1000)
    informe = auditar.auditar(redis, CUENTA["organizador"])
    assert not informe.ok
    assert [texto for _, texto in informe.problemas] == [f"saldos de {CUENTA['carol']}: la cadena dice "
                                                         f"{informe.saldos[CUENTA['carol']]} y Redis "
                                                         f"{informe.saldos[CUENTA['carol']] + 1000}"]


def test_con_reparar_gana_la_cadena(redis, larga):
    redis.hincrby("saldos", CUENTA["carol"], 1000)
    redis.hset("cuentas", CUENTA["zoe"], json.dumps({"tipo": "usuario", "nombre": "zoe", "alta": 1}))
    assert auditar.auditar(redis, CUENTA["organizador"], reparar=True).reparado
    assert auditar.auditar(redis, CUENTA["organizador"]).ok


def bloque(redis, hashes, altura):
    clave = f"bloque:{hashes[altura]}"
    return clave, json.loads(redis.hget(clave, "transactions"))


def test_un_monto_cambiado_adentro_de_un_bloque_rompe_su_hash(redis, larga):
    _, hashes = larga
    clave, txs = bloque(redis, hashes, 5)
    txs[1]["monto"] += 1
    redis.hset(clave, "transactions", json.dumps(txs))
    problemas = auditar.auditar(redis, CUENTA["organizador"]).problemas
    assert (5, "el hash no sale del texto (raíz de transacciones y de estado) y el nonce") in problemas


def test_una_coinbase_inflada_se_detecta(redis, larga):
    _, hashes = larga
    clave, txs = bloque(redis, hashes, 3)
    txs[0]["monto"] = 10**6
    redis.hset(clave, "transactions", json.dumps(txs))
    textos = [t for a, t in auditar.auditar(redis, CUENTA["organizador"]).problemas if a == 3]
    assert any("coinbase" in t for t in textos)


def test_una_lista_de_shares_publicada_cambiada_se_detecta(redis, larga):
    detalle = json.loads(redis.hget("epoca:2", "detalle"))
    detalle[CUENTA["minero"]]["shares"] += 5
    redis.hset("epoca:2", "detalle", json.dumps(detalle))
    textos = [t for _, t in auditar.auditar(redis, CUENTA["organizador"]).problemas]
    assert any("la lista publicada de la época 2 no da la raíz" in t for t in textos)


def test_sin_organizador_lo_toma_del_genesis_y_lo_avisa(redis, larga):
    informe = auditar.auditar(redis)
    assert informe.ok and any("sale del génesis" in aviso for aviso in informe.avisos)


def test_otro_organizador_que_el_conocido_se_detecta(redis, larga):
    informe = auditar.auditar(redis, CUENTA["alice"])
    assert any("el organizador del génesis" in t for _, t in informe.problemas)


def test_lo_que_muestra(redis, larga):
    lineas = []
    auditar.mostrar(auditar.auditar(redis, CUENTA["organizador"]), salida=lineas.append)
    assert lineas[0].startswith("53 bloques") and lineas[-1].startswith("Todo igual")
