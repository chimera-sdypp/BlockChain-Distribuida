"""Configuración por variables de entorno, con valores por defecto."""

import os
from dataclasses import dataclass


def texto(nombre, defecto=""):
    return os.environ.get(nombre) or defecto


def entero(nombre, defecto):
    valor = os.environ.get(nombre)
    if not valor:
        return defecto
    try:
        return int(valor)
    except ValueError:
        raise ValueError(f"{nombre} tiene que ser un entero, no {valor!r}") from None


def direcciones(nombre):
    """Lee una lista «host:puerto,host:puerto» y la devuelve como [(host, puerto)]."""
    resultado = []
    for item in texto(nombre).split(","):
        if not item.strip():
            continue
        host, _, puerto = item.strip().rpartition(":")
        if not host or not puerto.isdigit():
            raise ValueError(f"{nombre}: {item!r} no tiene la forma host:puerto")
        resultado.append((host, int(puerto)))
    return resultado


@dataclass(frozen=True)
class Config:
    servicio: str
    puerto: int
    log_nivel: str
    logs_dir: str
    rabbit_host: str
    rabbit_puerto: int
    rabbit_usuario: str
    rabbit_clave: str
    rabbit_vhost: str
    rabbit_prefetch: int
    redis_host: str
    redis_puerto: int
    redis_clave: str
    redis_sentinels: list
    redis_master: str


def cargar(servicio):
    return Config(
        servicio=servicio,
        puerto=entero("PUERTO", 8080),
        log_nivel=texto("LOG_NIVEL", "INFO").upper(),
        logs_dir=texto("LOGS_DIR", "logs"),
        rabbit_host=texto("RABBIT_HOST", "localhost"),
        rabbit_puerto=entero("RABBIT_PUERTO", 5672),
        rabbit_usuario=texto("RABBIT_USUARIO", "guest"),
        rabbit_clave=texto("RABBIT_CLAVE", "guest"),
        rabbit_vhost=texto("RABBIT_VHOST", "/"),
        rabbit_prefetch=entero("RABBIT_PREFETCH", 1),
        redis_host=texto("REDIS_HOST", "localhost"),
        redis_puerto=entero("REDIS_PUERTO", 6379),
        redis_clave=texto("REDIS_CLAVE"),
        redis_sentinels=direcciones("REDIS_SENTINELS"),
        redis_master=texto("REDIS_MASTER", "mymaster"),
    )
