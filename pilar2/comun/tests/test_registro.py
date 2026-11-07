import re

from comun import registro

LINEA = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| WARNING \| hola 1$")


def test_loguea_a_consola_memoria_y_disco(tmp_path, capsys):
    log = registro.configurar("prueba", "INFO", tmp_path)
    log.debug("no se ve")
    log.warning("hola %d", 1)

    assert LINEA.match(capsys.readouterr().err.strip())
    assert LINEA.match(registro.memoria().lineas[-1])
    assert LINEA.match((tmp_path / "prueba.log").read_text().strip())


def test_la_memoria_guarda_las_ultimas():
    memoria = registro.Memoria(capacidad=2)
    for texto in ("a", "b", "c"):
        memoria.emit(registro.logging.makeLogRecord({"msg": texto}))
    assert list(memoria.lineas) == ["b", "c"]
