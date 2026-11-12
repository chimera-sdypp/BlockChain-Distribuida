import pytest

from comun import config


def test_valores_por_defecto(monkeypatch):
    for variable in ("PUERTO", "RABBIT_HOST", "REDIS_SENTINELS", "LOG_NIVEL"):
        monkeypatch.delenv(variable, raising=False)
    c = config.cargar("prueba")
    assert (c.servicio, c.puerto, c.rabbit_host, c.log_nivel) == ("prueba", 8080, "localhost", "INFO")
    assert c.redis_sentinels == []


def test_las_variables_pisan_los_defectos(monkeypatch):
    monkeypatch.setenv("PUERTO", "9000")
    monkeypatch.setenv("LOG_NIVEL", "debug")
    monkeypatch.setenv("REDIS_SENTINELS", "redis-0.redis:26379, redis-1.redis:26379")
    c = config.cargar("prueba")
    assert c.puerto == 9000
    assert c.log_nivel == "DEBUG"
    assert c.redis_sentinels == [("redis-0.redis", 26379), ("redis-1.redis", 26379)]


@pytest.mark.parametrize("variable, valor", [("PUERTO", "ocho"), ("REDIS_SENTINELS", "redis-0")])
def test_valor_invalido_dice_cual_variable(monkeypatch, variable, valor):
    monkeypatch.setenv(variable, valor)
    with pytest.raises(ValueError, match=variable):
        config.cargar("prueba")
