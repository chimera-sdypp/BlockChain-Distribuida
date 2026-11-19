"""billetera.html (BC-78): un solo archivo al día con sus fuentes, sin nada que se baje de afuera."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from billetera import armar

AQUI = Path(__file__).parents[1]


def test_billetera_html_esta_armada_con_el_verificador_de_hoy():
    assert (AQUI / "billetera.html").read_text() == armar.armar(), "correr `python billetera/armar.py`"


def test_no_carga_nada_de_afuera():
    html = (AQUI / "billetera.html").read_text()
    assert not re.search(r"<(script|link|img|iframe)[^>]+(src|href)=", html)
    assert "Verificador.aplicarBloque" in html and "const Verificador" in html


def test_los_scripts_de_la_pagina_son_javascript_valido(tmp_path):
    if not shutil.which("node"):
        pytest.skip("necesita node")
    html = (AQUI / "billetera.html").read_text()
    for i, codigo in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
        archivo = tmp_path / f"script{i}.js"
        archivo.write_text(codigo)
        resultado = subprocess.run(["node", "--check", str(archivo)], capture_output=True, text=True)
        assert resultado.returncode == 0, resultado.stderr


def test_la_clave_se_baja_en_el_formato_de_la_herramienta():
    html = (AQUI / "billetera.html").read_text()
    assert "JSON.stringify({ kty, crv, d, x })" in html  # lo que lee claves.herramienta.leer_privada
