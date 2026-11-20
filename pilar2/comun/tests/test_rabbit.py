import socket
from types import SimpleNamespace

from comun import rabbit


def test_conectar_reintenta_si_falla_el_dns(monkeypatch):
    config = SimpleNamespace(rabbit_host="rabbitmq", rabbit_puerto=5672, rabbit_vhost="/", rabbit_usuario="u",
                             rabbit_clave="c", rabbit_prefetch=1)
    conexion = rabbit.Rabbit(config)
    intentos = []

    def abrir():
        intentos.append(1)
        if len(intentos) == 1:
            raise socket.gaierror(-5, "No address associated with hostname")
        conexion._conexion = SimpleNamespace(is_open=True)

    monkeypatch.setattr(conexion, "_abrir", abrir)
    monkeypatch.setattr(rabbit.time, "sleep", lambda s: None)
    monkeypatch.setattr(rabbit.threading, "Thread", lambda **k: SimpleNamespace(start=lambda: None))
    conexion.conectar()
    assert len(intentos) == 2
    assert conexion.conectado()
