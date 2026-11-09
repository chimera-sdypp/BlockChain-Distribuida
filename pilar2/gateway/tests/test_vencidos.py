"""Ticket 05: los préstamos vencidos vuelven a `rangos` una sola vez y no se pierden."""

import threading

from prometheus_client import REGISTRY

from gateway.tests.test_keepalive import prestado
from gateway.tests.test_rangos import sacar


def todos(colas, cola):
    mensajes = []
    while (mensaje := sacar(colas, cola)) is not None:
        mensajes.append(mensaje)
    return mensajes


def test_un_prestamo_vencido_vuelve_a_rangos_y_se_borra(armar, redis, colas):
    g, cliente = armar()
    prestado(cliente, redis, colas)  # vence en t=1: hace rato
    antes = REGISTRY.get_sample_value("blockchain_prestamos_vencidos_total") or 0

    g.revisar_vencidos()

    [mensaje] = todos(colas, "rangos")
    assert (mensaje["tarea"], mensaje["rango"]) == ("t1", 0)
    assert redis.zcard("prestamos") == 0 and not redis.exists("prestamo:t1:0")
    assert REGISTRY.get_sample_value("blockchain_prestamos_vencidos_total") == antes + 1


def test_los_que_no_vencieron_no_se_tocan(armar, redis, colas):
    g, cliente = armar()
    prestado(cliente, redis, colas)
    redis.zadd("prestamos", {"t1:0": 9_999_999_999})

    g.revisar_vencidos()

    assert todos(colas, "rangos") == [] and redis.zcard("prestamos") == 1


def test_con_dos_pods_cada_vencido_se_publica_una_sola_vez(armar, redis, colas):
    g, cliente = armar()
    for numero in range(10):
        prestado(cliente, redis, colas, numero=numero)
    pods = [armar()[0] for _ in range(2)]
    hilos = [threading.Thread(target=p.revisar_vencidos) for p in pods for _ in range(3)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    numeros = sorted(m["rango"] for m in todos(colas, "rangos"))
    assert numeros == list(range(10))


def test_los_de_una_tarea_cerrada_no_vuelven(armar, redis, colas):
    g, cliente = armar()
    prestado(cliente, redis, colas)
    redis.hset("tarea:t1", "estado", "confirmada")

    g.revisar_vencidos()

    assert todos(colas, "rangos") == [] and redis.zcard("prestamos") == 0


def test_si_el_pod_muere_entre_reclamarlo_y_publicarlo_se_reintenta(armar, redis, colas):
    g, cliente = armar()
    prestado(cliente, redis, colas)
    publicar = g.rabbit.publicar
    g.rabbit.publicar = lambda *a: (_ for _ in ()).throw(ConnectionError("el pod murió"))
    g.revisar_vencidos()
    g.rabbit.publicar = publicar
    assert todos(colas, "rangos") == [] and redis.zscore("prestamos", "t1:0") > 1  # reclamado, no perdido

    redis.zadd("prestamos", {"t1:0": 1})  # pasó el tiempo: volvió a vencer
    g.revisar_vencidos()

    assert [m["rango"] for m in todos(colas, "rangos")] == [0]
