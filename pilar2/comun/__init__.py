"""Módulo común de los servicios del Pilar 2."""

from .rabbit import Rabbit
from .servicio import Servicio

__all__ = ["Rabbit", "Servicio"]
