"""Arma billetera.html: pagina.html con verificador.js adentro (un solo archivo, sin nada externo).

    python billetera/armar.py        # desde pilar2/; el test comprueba que billetera.html esté al día
"""

from pathlib import Path

AQUI = Path(__file__).parent
MARCA = "/*VERIFICADOR*/"


def armar():
    pagina = (AQUI / "pagina.html").read_text()
    assert pagina.count(MARCA) == 1
    verificador = (AQUI / "verificador.js").read_text().replace("</script", "<\\/script")
    return pagina.replace(MARCA, verificador)


if __name__ == "__main__":
    (AQUI / "billetera.html").write_text(armar())
    print(AQUI / "billetera.html")
