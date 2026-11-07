"""Conexión a Redis: directa o, si hay REDIS_SENTINELS, al primario que indique Sentinel."""

import redis
from redis.sentinel import Sentinel


def conectar(config):
    opciones = dict(password=config.redis_clave or None, decode_responses=True,
                    socket_timeout=5, socket_connect_timeout=5)
    if not config.redis_sentinels:
        return redis.Redis(host=config.redis_host, port=config.redis_puerto, **opciones)
    # Sentinel dice cuál es el primario; si cambia, el pool se reconecta solo al nuevo.
    sentinel = Sentinel(config.redis_sentinels, socket_timeout=5,
                        sentinel_kwargs={"password": config.redis_clave or None})
    return sentinel.master_for(config.redis_master, **opciones)


def vivo(cliente):
    try:
        return cliente.ping()
    except redis.RedisError:
        return False
