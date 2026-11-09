"""Contra RabbitMQ y Redis de verdad. Corren con INTEGRACION=1 (en el CI, con services)."""

import os
import threading
import time
import uuid

import pytest
import redis
from fastapi.testclient import TestClient

from comun import Rabbit, Servicio, config, redisdb

pytestmark = pytest.mark.skipif(os.environ.get("INTEGRACION") != "1",
                                reason="necesita RabbitMQ y Redis (INTEGRACION=1)")


@pytest.fixture
def cola():
    nombre = f"prueba-{uuid.uuid4().hex[:8]}"
    yield nombre
    limpiador = Rabbit(config.cargar("prueba"))
    limpiador.conectar()
    limpiador._canal.queue_delete(nombre)
    limpiador.detener()


def consumir_en_hilo(rabbit, cola, manejador):
    hilo = threading.Thread(target=rabbit.consumir, args=(cola, manejador), daemon=True)
    hilo.start()
    return hilo


def esperar(condicion, segundos=10):
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.1)
    return False


def test_publicar_y_consumir(cola):
    c = config.cargar("prueba")
    publicador, consumidor = Rabbit(c, [cola]), Rabbit(c, [cola])
    publicador.conectar()
    recibidos = []
    hilo = consumir_en_hilo(consumidor, cola, recibidos.append)
    for numero in range(3):
        publicador.publicar(cola, {"numero": numero})
    assert esperar(lambda: len(recibidos) == 3)
    assert recibidos == [{"numero": 0}, {"numero": 1}, {"numero": 2}]
    consumidor.detener()
    publicador.detener()
    hilo.join(5)
    assert not hilo.is_alive()


def test_si_el_manejador_falla_se_reintenta_una_vez_y_se_descarta(cola):
    c = config.cargar("prueba")
    publicador, consumidor = Rabbit(c, [cola]), Rabbit(c, [cola])
    intentos = []

    def falla(mensaje):
        intentos.append(mensaje)
        raise RuntimeError("falla a propósito")

    consumir_en_hilo(consumidor, cola, falla)
    publicador.conectar()
    publicador.publicar(cola, {"malo": True})
    assert esperar(lambda: len(intentos) == 2)
    time.sleep(1)
    assert len(intentos) == 2  # no vuelve una tercera vez
    consumidor.detener()
    publicador.detener()


def test_si_falla_redis_el_mensaje_vuelve_hasta_que_se_procesa(cola, monkeypatch):
    monkeypatch.setattr("comun.rabbit.ESPERA_TRANSITORIO", 0)
    c = config.cargar("prueba")
    publicador, consumidor = Rabbit(c, [cola]), Rabbit(c, [cola])
    intentos, procesados = [], []

    def redis_caido_tres_veces(mensaje):
        intentos.append(mensaje)
        if len(intentos) <= 3:
            raise redis.ConnectionError("Sentinel todavía no eligió primario")
        procesados.append(mensaje)

    consumir_en_hilo(consumidor, cola, redis_caido_tres_veces)
    publicador.conectar()
    publicador.publicar(cola, {"tx": 1})
    assert esperar(lambda: procesados == [{"tx": 1}])
    assert len(intentos) == 4
    consumidor.detener()
    publicador.detener()


def test_health_del_servicio_con_rabbitmq_y_redis(tmp_path, monkeypatch, cola):
    monkeypatch.setenv("LOGS_DIR", str(tmp_path))
    servicio = Servicio("prueba", colas=[cola])
    servicio.rabbit.conectar()
    assert redisdb.vivo(servicio.redis)
    respuesta = TestClient(servicio.app).get("/health")
    assert respuesta.json() == {"prueba": "ok", "rabbitmq": "ok", "redis": "ok"}
    servicio.rabbit.detener()


# ── Suscripción a un exchange (eventos) ──

@pytest.fixture
def exchange():
    """Un exchange topic propio de cada test, para no mezclar avisos con un sistema corriendo."""
    nombre = f"eventos-prueba-{uuid.uuid4().hex[:8]}"
    yield nombre
    limpiador = Rabbit(config.cargar("prueba"))
    limpiador.conectar()
    limpiador._canal.exchange_delete(nombre)
    limpiador.detener()


def suscribir_en_hilo(rabbit, exchange, claves, manejador):
    hilo = threading.Thread(target=rabbit.suscribir, args=(exchange, claves, manejador), daemon=True)
    hilo.start()
    assert esperar(rabbit.suscripto)
    return hilo


def test_el_suscriptor_recibe_solo_las_claves_a_las_que_se_ato(exchange):
    c = config.cargar("prueba")
    suscriptor, avisador = Rabbit(c), Rabbit(c)
    recibidos = []
    suscribir_en_hilo(suscriptor, exchange, ["bloque.confirmado"], lambda clave, aviso: recibidos.append((clave, aviso)))
    avisador.conectar()

    avisador.avisar(exchange, "transaccion.estado", {"id": "x"})
    avisador.avisar(exchange, "bloque.confirmado", {"block_hash": "abc"})

    assert esperar(lambda: recibidos == [("bloque.confirmado", {"block_hash": "abc"})])
    time.sleep(0.5)
    assert recibidos == [("bloque.confirmado", {"block_hash": "abc"})]
    suscriptor.detener()
    avisador.detener()


def test_dos_suscriptores_reciben_el_mismo_aviso(exchange):
    c = config.cargar("prueba")
    uno, otro, avisador = Rabbit(c), Rabbit(c), Rabbit(c)
    recibidos_uno, recibidos_otro = [], []
    suscribir_en_hilo(uno, exchange, ["bloque.confirmado", "transaccion.estado"],
                      lambda clave, aviso: recibidos_uno.append(clave))
    suscribir_en_hilo(otro, exchange, ["bloque.confirmado"], lambda clave, aviso: recibidos_otro.append(aviso))
    avisador.conectar()

    avisador.avisar(exchange, "bloque.confirmado", {"altura": 1})
    avisador.avisar(exchange, "transaccion.estado", {"id": "t"})

    assert esperar(lambda: len(recibidos_uno) == 2 and len(recibidos_otro) == 1)
    assert sorted(recibidos_uno) == ["bloque.confirmado", "transaccion.estado"]
    assert recibidos_otro == [{"altura": 1}]
    uno.detener()
    otro.detener()
    avisador.detener()


def test_avisar_sin_suscriptores_no_falla(exchange):
    avisador = Rabbit(config.cargar("prueba"))
    avisador.conectar()
    avisador.avisar(exchange, "bloque.confirmado", {"altura": 1})
    avisador.detener()


def test_detener_corta_la_suscripcion(exchange):
    suscriptor = Rabbit(config.cargar("prueba"))
    hilo = suscribir_en_hilo(suscriptor, exchange, ["bloque.confirmado"], lambda clave, aviso: None)
    suscriptor.detener()
    hilo.join(5)
    assert not hilo.is_alive()
