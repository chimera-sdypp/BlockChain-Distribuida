"""La ráfaga de la demo con una ventana: el ritmo (transferencias por segundo) se mueve en vivo y un botón tira
N juntas. Firma en ronda con las cuentas de prueba del génesis (alice → bob → carol → alice), lleva el nonce de
cada una y sigue cada transferencia hasta que entra en un bloque o se rechaza.

    pilar2/.venv/bin/python pilar3/demo/rafaga.py [--sistema https://…] [--puerto 8765] [--sin-ventana]

Abre la ventana con Chrome en modo app (o en el navegador). Ctrl+C la cierra.
"""

import argparse
import collections
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pilar2"))

from claves.herramienta import Backend  # noqa: E402
from comun import contrato  # noqa: E402
from comun.tests.claves import PRIVADA  # noqa: E402

NOMBRES = ("alice", "bob", "carol")
RITMO_MAXIMO = 100
TIRADA_MAXIMA = 1000
SIN_RESPUESTA = 600  # segundos: la que no entró en un bloque ni se rechazó, deja de seguirse


class Rafaga:
    def __init__(self, sistema):
        self.backend = Backend(sistema, timeout=15)
        self.sistema = sistema
        self.privadas = [PRIVADA[n] for n in NOMBRES]
        self.cuentas = [contrato.clave_publica(p) for p in self.privadas]
        self.nombre = dict(zip(self.cuentas, NOMBRES))
        self.nonces = {c: self.backend.nonce(c) for c in self.cuentas}
        self.lock = threading.Lock()
        self.turno = 0
        self.ritmo = 0.0
        self.envios = ThreadPoolExecutor(max_workers=48)
        self.abiertas = {}  # id → hora en que se aceptó
        self.cuenta = collections.Counter()  # mandadas, aceptadas, confirmadas, rechazadas, fallidas, sin_respuesta
        self.motivos = collections.Counter()
        self.por_segundo = collections.defaultdict(lambda: [0, 0])  # segundo → [mandadas, confirmadas]
        self.demoras = collections.deque(maxlen=200)
        self.log = collections.deque(maxlen=8)
        self.bloque = None
        self.en_cajas = None
        self.altura = self._punta()
        for hilo in (self._emisor, self._seguidor, self._cajas):
            threading.Thread(target=hilo, daemon=True).start()

    def anotar(self, texto):
        with self.lock:
            self.log.appendleft(f"{time.strftime('%H:%M:%S')}  {texto}")

    def _punta(self):
        codigo, bloques = self.backend.pedir("GET", "/api/bloques?limite=1")
        return bloques[0]["altura"] if codigo == 200 and bloques else -1

    # ── Mandar ──

    def _una(self):
        with self.lock:
            i = self.turno
            self.turno += 1
            origen, destino = self.cuentas[i % 3], self.cuentas[(i + 1) % 3]
            self.nonces[origen] += 1
            nonce = self.nonces[origen]
            self.cuenta["mandadas"] += 1
            self.por_segundo[int(time.time())][0] += 1
        tx = contrato.transaccion(self.privadas[i % 3], destino, 1 + i % 5, nonce)
        for intento in range(4):  # con el primario de Redis muerto el backend contesta 503 unos segundos
            try:
                codigo, respuesta = self.backend.mandar(tx)
            except OSError as error:
                codigo, respuesta = 0, str(error)
            if codigo not in (0, 502, 503, 504):
                break
            time.sleep(1 + intento)
        with self.lock:
            if codigo == 202:
                self.cuenta["aceptadas"] += 1
                self.abiertas[respuesta["id"]] = time.time()
                return
            self.cuenta["fallidas"] += 1
        self.anotar(f"✗ {self.nombre[origen]} → {self.nombre[destino]}: {codigo} {json.dumps(respuesta)[:80]}")
        self._resincronizar(origen)

    def _resincronizar(self, cuenta):
        """Una que no entró deja un hueco en los nonces: se vuelve al último que aceptó el sistema."""
        try:
            nonce = self.backend.nonce(cuenta)
        except SystemExit:
            return
        with self.lock:
            self.nonces[cuenta] = nonce

    def tirar(self, cantidad):
        cantidad = max(1, min(TIRADA_MAXIMA, int(cantidad)))
        for _ in range(cantidad):
            self.envios.submit(self._una)
        self.anotar(f"Tiro {cantidad} juntas")

    def poner_ritmo(self, ritmo):
        ritmo = max(0.0, min(RITMO_MAXIMO, float(ritmo)))
        if ritmo != self.ritmo:
            self.anotar(f"Ritmo: {ritmo:g} por segundo" if ritmo else "Ritmo: parado")
        self.ritmo = ritmo

    def _emisor(self):
        siguiente = time.monotonic()
        while True:
            if not self.ritmo:
                time.sleep(0.05)
                siguiente = time.monotonic()
                continue
            ahora = time.monotonic()
            if ahora < siguiente:
                time.sleep(min(siguiente - ahora, 0.05))
                continue
            self.envios.submit(self._una)
            siguiente = max(siguiente + 1 / self.ritmo, ahora - 1)

    # ── Seguir ──

    def _seguidor(self):
        """Cada segundo baja los bloques nuevos y marca las que entraron; a las que tardan, les pregunta el estado."""
        revisadas = 0.0
        while True:
            time.sleep(1)
            try:
                codigo, bloques = self.backend.pedir("GET", f"/api/bloques?orden=asc&desde={self.altura + 1}&limite=20")
            except OSError:
                continue
            for b in bloques if codigo == 200 else []:
                self.altura = b["altura"]
                ahora, mias = time.time(), 0
                with self.lock:
                    for tx in b["transactions"]:
                        aceptada = self.abiertas.pop(tx["id"], None)
                        if aceptada is not None:
                            mias += 1
                            self.cuenta["confirmadas"] += 1
                            self.por_segundo[int(ahora)][1] += 1
                            self.demoras.append(ahora - aceptada)
                    self.bloque = {"altura": b["altura"], "transacciones": len(b["transactions"]) - 1, "mias": mias,
                                   "prefijo": len(b["prefijo"]), "duracion": float(b.get("duracion") or 0)}
                self.anotar(f"Bloque {b['altura']}: {len(b['transactions']) - 1} tx ({mias} de la ráfaga), "
                            f"prefijo {len(b['prefijo'])}")
            if time.monotonic() - revisadas > 10:
                revisadas = time.monotonic()
                self._revisar_viejas()

    def _revisar_viejas(self):
        with self.lock:
            viejas = sorted((t, i) for i, t in self.abiertas.items() if time.time() - t > 45)[:30]
        for aceptada, id_ in viejas:
            tx = self.backend.estado(id_) or {}
            with self.lock:
                if id_ not in self.abiertas:
                    continue
                if tx.get("estado") == "rechazada":
                    del self.abiertas[id_]
                    self.cuenta["rechazadas"] += 1
                    self.motivos[tx.get("motivo", "?")] += 1
                elif time.time() - aceptada > SIN_RESPUESTA:
                    del self.abiertas[id_]
                    self.cuenta["sin_respuesta"] += 1

    def _cajas(self):
        while True:
            try:
                codigo, datos = self.backend.pedir("GET", "/api/cajas")
                self.en_cajas = datos.get("total_pendientes") if codigo == 200 else None
            except OSError:
                self.en_cajas = None
            time.sleep(2)

    def estado(self):
        ahora = int(time.time())
        with self.lock:
            serie = [self.por_segundo.get(s, [0, 0]) for s in range(ahora - 59, ahora + 1)]
            for s in [s for s in self.por_segundo if s < ahora - 120]:
                del self.por_segundo[s]
            demoras = sorted(self.demoras)
            return {
                "sistema": self.sistema, "ritmo": self.ritmo, "cuenta": dict(self.cuenta), "esperando": len(self.abiertas),
                "motivos": dict(self.motivos), "en_cajas": self.en_cajas, "bloque": self.bloque, "serie": serie,
                "demora": demoras[len(demoras) // 2] if demoras else None, "log": list(self.log),
                "mandadas_10s": sum(m for m, _ in serie[-10:]) / 10, "confirmadas_30s": sum(c for _, c in serie[-30:]) / 30,
            }


def servidor(rafaga, puerto):
    pagina = PAGINA.encode()

    class Manejador(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def responder(self, codigo, cuerpo, tipo="application/json"):
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(cuerpo)

        def do_GET(self):
            if self.path == "/":
                self.responder(200, pagina, "text/html; charset=utf-8")
            elif self.path == "/estado":
                self.responder(200, json.dumps(rafaga.estado()).encode())
            else:
                self.responder(404, b"{}")

        def do_POST(self):
            datos = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/ritmo":
                rafaga.poner_ritmo(datos.get("ritmo", 0))
            elif self.path == "/tirar":
                rafaga.tirar(datos.get("cantidad", 10))
            else:
                return self.responder(404, b"{}")
            self.responder(200, b"{}")

    return ThreadingHTTPServer(("127.0.0.1", puerto), Manejador)


def abrir(url):
    chrome = next((c for c in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")
                   if shutil.which(c)), None)
    if chrome:
        perfil = tempfile.mkdtemp(prefix="rafaga-")  # un perfil propio: así respeta el tamaño de la ventana
        subprocess.Popen([chrome, f"--app={url}", "--window-size=520,1000", f"--user-data-dir={perfil}",
                          "--no-first-run", "--no-default-browser-check"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sistema", default="https://blockchain-chimera.chimera-sdypp.workers.dev", help="la API del sistema")
    p.add_argument("--puerto", type=int, default=8765)
    p.add_argument("--sin-ventana", action="store_true", help="no abre el navegador")
    a = p.parse_args()
    rafaga = Rafaga(a.sistema.rstrip("/"))
    http = servidor(rafaga, a.puerto)
    url = f"http://127.0.0.1:{a.puerto}/"
    print(f"Ráfaga contra {rafaga.sistema}: ventana en {url} (Ctrl+C para cerrar)")
    if not a.sin_ventana:
        abrir(url)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        rafaga.ritmo = 0
        print("\nCerrada.", json.dumps(rafaga.estado()["cuenta"]))


PAGINA = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ráfaga</title>
<style>
:root { --fondo:#F6F4EE; --tarjeta:#FBFAF6; --borde:#DCD5C4; --texto:#26304A; --suave:#5E6678; --acento:#B4521A;
        --azul:#245E9E; --verde:#2E7D4F; --rojo:#B23A3A; --pista:#E4DDCD; }
@media (prefers-color-scheme: dark) { :root { --fondo:#111A30; --tarjeta:#18243F; --borde:#2C3B5E; --texto:#E6EAF2;
        --suave:#9AA5BC; --acento:#F2A65A; --azul:#8CB8EA; --verde:#6CC795; --rojo:#F08C8C; --pista:#24335A; } }
* { box-sizing:border-box; margin:0 }
body { background:var(--fondo); color:var(--texto); font:15px/1.4 system-ui, "IBM Plex Sans", sans-serif; padding:16px;
       display:flex; flex-direction:column; gap:12px; min-height:100vh }
h1 { font-size:20px; display:flex; justify-content:space-between; align-items:baseline; gap:8px }
h1 small { font-size:12px; color:var(--suave); font-weight:400; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
.tarjeta { background:var(--tarjeta); border:1px solid var(--borde); border-radius:12px; padding:14px }
.etiqueta { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--suave); font-weight:600 }
.ritmo { display:flex; align-items:baseline; gap:8px; margin:4px 0 8px }
.ritmo b { font-size:44px; line-height:1; color:var(--acento); font-variant-numeric:tabular-nums }
input[type=range] { width:100%; accent-color:var(--acento); height:28px }
.botones { display:flex; gap:6px; flex-wrap:wrap; margin-top:8px }
button { font:inherit; font-weight:600; border:1px solid var(--borde); background:var(--fondo); color:var(--texto);
         border-radius:8px; padding:7px 12px; cursor:pointer }
button:hover { border-color:var(--acento) } button.fuerte { background:var(--acento); color:#fff; border-color:var(--acento) }
.tirar { display:flex; gap:6px; margin-top:8px } .tirar input { width:90px; font:inherit; padding:6px 8px;
         border:1px solid var(--borde); border-radius:8px; background:var(--fondo); color:var(--texto) }
.grilla { display:grid; grid-template-columns:repeat(3,1fr); gap:8px }
.dato { background:var(--tarjeta); border:1px solid var(--borde); border-radius:10px; padding:8px 10px }
.dato b { display:block; font-size:22px; font-variant-numeric:tabular-nums }
.dato span { font-size:12px; color:var(--suave) }
.ok b { color:var(--verde) } .mal b { color:var(--rojo) }
canvas { width:100%; height:110px; display:block }
.leyenda { display:flex; gap:14px; font-size:12px; color:var(--suave); margin-top:4px }
.leyenda i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:-1px }
#log { font:12px/1.5 ui-monospace, "JetBrains Mono", monospace; color:var(--suave); white-space:pre-wrap; min-height:96px }
#bloque { font-size:14px; margin-top:2px }
</style></head><body>
<h1>Ráfaga <small id="sistema"></small></h1>
<div class="tarjeta">
  <div class="etiqueta">Transferencias por segundo</div>
  <div class="ritmo"><b id="ritmo">0</b><span id="real" class="etiqueta"></span></div>
  <input id="barra" type="range" min="0" max="100" step="1" value="0" aria-label="Transferencias por segundo">
  <div class="botones">
    <button data-ritmo="0">Parar</button><button data-ritmo="2">2</button><button data-ritmo="10">10</button>
    <button data-ritmo="30">30</button><button data-ritmo="60">60</button><button data-ritmo="100">100</button>
  </div>
</div>
<div class="tarjeta">
  <div class="etiqueta">Tirar varias juntas</div>
  <div class="botones">
    <button class="fuerte" data-tirar="10">+10</button><button class="fuerte" data-tirar="50">+50</button>
    <button class="fuerte" data-tirar="200">+200</button><button class="fuerte" data-tirar="500">+500</button>
  </div>
  <div class="tirar"><input id="cantidad" type="number" min="1" max="1000" value="100" aria-label="Cantidad">
    <button id="tirar">Tirar</button></div>
</div>
<div class="grilla">
  <div class="dato"><b id="mandadas">0</b><span>mandadas</span></div>
  <div class="dato"><b id="esperando">0</b><span>esperan bloque</span></div>
  <div class="dato ok"><b id="confirmadas">0</b><span>confirmadas</span></div>
  <div class="dato"><b id="cajas">–</b><span>en cajas (NCT)</span></div>
  <div class="dato"><b id="demora">–</b><span>demora mediana</span></div>
  <div class="dato mal"><b id="rechazadas">0</b><span id="motivo">rechazadas</span></div>
</div>
<div class="tarjeta">
  <div class="etiqueta">Últimos 60 segundos</div>
  <canvas id="grafico" width="960" height="220"></canvas>
  <div class="leyenda"><span><i style="background:var(--azul)"></i>mandadas/s</span>
    <span><i style="background:var(--verde)"></i>confirmadas/s</span></div>
  <div id="bloque"></div>
</div>
<div class="tarjeta"><div class="etiqueta">Qué pasó</div><div id="log"></div></div>
<script>
const $ = (id) => document.getElementById(id);
const post = (ruta, cuerpo) => fetch(ruta, { method: "POST", body: JSON.stringify(cuerpo) });
let tocando = false;
const ponerRitmo = (r) => { $("barra").value = r; $("ritmo").textContent = r; post("/ritmo", { ritmo: Number(r) }); };
$("barra").addEventListener("input", (e) => { tocando = true; $("ritmo").textContent = e.target.value; });
$("barra").addEventListener("change", (e) => { tocando = false; ponerRitmo(e.target.value); });
document.querySelectorAll("[data-ritmo]").forEach((b) => b.onclick = () => ponerRitmo(b.dataset.ritmo));
document.querySelectorAll("[data-tirar]").forEach((b) => b.onclick = () => post("/tirar", { cantidad: Number(b.dataset.tirar) }));
$("tirar").onclick = () => post("/tirar", { cantidad: Number($("cantidad").value || 0) });
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

function dibujar(serie) {
  const c = $("grafico"), g = c.getContext("2d"), W = c.width, H = c.height, pie = 18;
  g.clearRect(0, 0, W, H);
  const tope = Math.max(5, ...serie.map(([m, k]) => Math.max(m, k)));
  const paso = W / serie.length, alto = (v) => (H - pie) * v / tope;
  g.fillStyle = css("--pista"); g.fillRect(0, H - pie, W, 1);
  g.fillStyle = css("--azul");
  serie.forEach(([m], i) => { const h = alto(m); g.fillRect(i * paso + 2, H - pie - h, paso - 4, h); });
  g.strokeStyle = css("--verde"); g.lineWidth = 4; g.beginPath();
  serie.forEach(([, k], i) => { const x = i * paso + paso / 2, y = H - pie - alto(k); i ? g.lineTo(x, y) : g.moveTo(x, y); });
  g.stroke();
  g.fillStyle = css("--suave"); g.font = "22px system-ui";
  g.fillText(`${tope}/s`, 6, 22); g.fillText("−60 s", 6, H - 2); g.fillText("ahora", W - 64, H - 2);
}

async function actualizar() {
  try {
    const e = await (await fetch("/estado")).json(), n = e.cuenta;
    $("sistema").textContent = e.sistema;
    if (!tocando && document.activeElement !== $("barra")) { $("barra").value = e.ritmo; $("ritmo").textContent = e.ritmo; }
    $("real").textContent = `real ${e.mandadas_10s.toFixed(1)}/s · confirman ${e.confirmadas_30s.toFixed(1)}/s`;
    $("mandadas").textContent = n.mandadas || 0;
    $("esperando").textContent = e.esperando;
    $("confirmadas").textContent = n.confirmadas || 0;
    $("cajas").textContent = e.en_cajas ?? "–";
    $("demora").textContent = e.demora == null ? "–" : `${e.demora.toFixed(0)} s`;
    $("rechazadas").textContent = (n.rechazadas || 0) + (n.fallidas || 0);
    const motivo = Object.entries(e.motivos).sort((a, b) => b[1] - a[1])[0];
    $("motivo").textContent = motivo ? `rechazadas · ${motivo[0]}` : (n.fallidas ? "no entraron (HTTP)" : "rechazadas");
    $("bloque").textContent = e.bloque ? `Último bloque: ${e.bloque.altura} · ${e.bloque.transacciones} tx (${e.bloque.mias} de la ráfaga) · prefijo ${e.bloque.prefijo} · minado en ${e.bloque.duracion.toFixed(1)} s` : "";
    $("log").textContent = e.log.join("\n");
    dibujar(e.serie);
  } catch { $("log").textContent = "Se cortó: ¿se cerró rafaga.py?"; }
}
setInterval(actualizar, 700); actualizar();
</script></body></html>
"""

if __name__ == "__main__":
    main()
