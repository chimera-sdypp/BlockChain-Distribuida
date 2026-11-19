"""El almacenamiento propio del nodo (BC-83): un SQLite en su disco. No usa el Redis compartido.

Guarda los bloques que verificó (en orden de altura), un snapshot del estado cada tanto, las listas de shares que
ya comprobó, los bloques que rechazó y, si es validador, la última firma (para no firmar nunca dos bloques a la
misma altura, ni uno que no descienda del último).
"""

import json
import sqlite3
import threading
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS bloques (altura INTEGER PRIMARY KEY, hash TEXT UNIQUE NOT NULL, bloque TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS epocas (epoca INTEGER PRIMARY KEY, detalle TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rechazos (altura INTEGER, hash TEXT, motivos TEXT, cuando INTEGER, PRIMARY KEY (altura, hash));
CREATE TABLE IF NOT EXISTS meta (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
"""


class Almacen:
    def __init__(self, ruta):
        Path(ruta).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(ruta, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(ESQUEMA)
        self._lock = threading.Lock()

    def _uno(self, consulta, *parametros):
        with self._lock:
            return self._db.execute(consulta, parametros).fetchone()

    def _todos(self, consulta, *parametros):
        with self._lock:
            return self._db.execute(consulta, parametros).fetchall()

    def _hacer(self, consulta, *parametros):
        with self._lock:
            self._db.execute(consulta, parametros)

    # ── Bloques ──

    def guardar_bloque(self, bloque):
        self._hacer("INSERT OR REPLACE INTO bloques VALUES (?, ?, ?)", bloque["altura"], bloque["block_hash"],
                    json.dumps(bloque))

    def bloques(self, desde=0, hasta=None, limite=None):
        consulta, parametros = "SELECT bloque FROM bloques WHERE altura >= ?", [desde]
        if hasta is not None:
            consulta, parametros = consulta + " AND altura <= ?", parametros + [hasta]
        consulta += " ORDER BY altura"
        if limite:
            consulta, parametros = consulta + " LIMIT ?", parametros + [limite]
        return [json.loads(fila[0]) for fila in self._todos(consulta, *parametros)]

    def bloque(self, hash_):
        fila = self._uno("SELECT bloque FROM bloques WHERE hash = ?", hash_)
        return json.loads(fila[0]) if fila else None

    def altura(self):
        fila = self._uno("SELECT MAX(altura) FROM bloques")
        return -1 if fila[0] is None else fila[0]

    # ── Lo demás ──

    def guardar_epoca(self, epoca, detalle):
        self._hacer("INSERT OR REPLACE INTO epocas VALUES (?, ?)", epoca, json.dumps(detalle))

    def epoca(self, epoca):
        fila = self._uno("SELECT detalle FROM epocas WHERE epoca = ?", epoca)
        return json.loads(fila[0]) if fila else None

    def rechazar(self, altura, hash_, motivos, cuando):
        """Anota un bloque rechazado. Devuelve True si es la primera vez que se lo rechaza."""
        nuevo = self._uno("SELECT 1 FROM rechazos WHERE altura = ? AND hash = ?", altura, hash_) is None
        self._hacer("INSERT OR REPLACE INTO rechazos VALUES (?, ?, ?, ?)", altura, hash_, json.dumps(motivos), cuando)
        return nuevo

    def rechazos(self, limite=50):
        return [{"altura": a, "block_hash": h, "motivos": json.loads(m), "cuando": c}
                for a, h, m, c in self._todos("SELECT * FROM rechazos ORDER BY cuando DESC LIMIT ?", limite)]

    def meta(self, clave, valor=None):
        if valor is None:
            fila = self._uno("SELECT valor FROM meta WHERE clave = ?", clave)
            return json.loads(fila[0]) if fila else None
        self._hacer("INSERT OR REPLACE INTO meta VALUES (?, ?)", clave, json.dumps(valor))
        return valor
