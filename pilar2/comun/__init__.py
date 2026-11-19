"""Módulo común de los servicios del Pilar 2.

`Rabbit` y `Servicio` se importan recién cuando se piden: así `comun.contrato` y `comun.estado` andan sólo con
`cryptography` (la herramienta de claves y los nodos verificadores que corre cualquiera, fuera del cluster).
"""

__all__ = ["Rabbit", "Servicio"]


def __getattr__(nombre):
    if nombre == "Rabbit":
        from .rabbit import Rabbit
        return Rabbit
    if nombre == "Servicio":
        from .servicio import Servicio
        return Servicio
    raise AttributeError(f"module 'comun' has no attribute {nombre!r}")
