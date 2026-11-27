"""Un minero que miente a propósito, sólo para las pruebas de BC-76 (`TRAMPA`, apagado por defecto).

Cada trampa es uno de los ataques del epic BC-72, y el sistema tiene que detectarla o neutralizarla:

| TRAMPA | Qué hace | Qué tiene que pasar |
|---|---|---|
| `bench-alto` | Declara 1000 veces su bench | Lo medido lo deja en lo real (BC-74) |
| `bench-bajo` | Declara la centésima parte | No frena al ajuste por tiempo (BC-73) |
| `gpu-falsa` | Dice ser una GPU de 2 GH/s, avisa y nunca pide rangos | El escalador levanta VMs igual (BC-74) |
| `acaparar` | Pide 10 rangos seguidos y los retiene sin minar | Recibe uno solo, y el plazo se lo saca (BC-74) |
| `retener` | Toma un rango y manda keep-alive sin minarlo | El plazo se lo saca (BC-74) |
| `sin-solucion` | Contesta «sin solución» sin buscar | Queda suspendido (BC-75) |
| `guardar` | Busca, y si encuentra el nonce contesta «sin solución» | Queda suspendido (BC-75) |
| `rangos-ajenos` | En cada tarea nueva manda «sin solución» de los rangos 0 a 9 que no son suyos y de 1000 a 1009, y después mina el suyo | Ninguno llega al TrP (409 y 400) y los bloques siguen saliendo (BC-92) |
"""

import json
import logging
import time

from comun import contrato

from .minero import Minero

log = logging.getLogger("minero_cpu")

TRAMPAS = ("bench-alto", "bench-bajo", "gpu-falsa", "acaparar", "retener", "sin-solucion", "guardar", "rangos-ajenos")
AJENOS = (*range(10), *range(1000, 1010))  # los del profesor (1000 a 1009) y los que la tarea sí tiene


class MineroTramposo(Minero):
    def __init__(self, *args, trampa, **opciones):
        if trampa not in TRAMPAS:
            raise ValueError(f"TRAMPA va {', '.join(TRAMPAS)}, no {trampa!r}")
        super().__init__(*args, **opciones)
        self.trampa, self.tareas_atacadas = trampa, set()
        if trampa == "gpu-falsa":
            self.tipo, self.placa = "gpu", "NVIDIA GeForce RTX 4090 (falsa)"
        log.warning("Minero TRAMPOSO (%s): sólo para pruebas", trampa)

    def _insistir(self, metodo, ruta, cuerpo=None):
        if ruta == "/bench" and cuerpo:
            factor = {"bench-alto": 1000, "bench-bajo": 0.01}.get(self.trampa)
            if self.trampa == "gpu-falsa":
                cuerpo = {**cuerpo, "hashes_por_segundo": 2_000_000_000}
            elif factor:
                cuerpo = {**cuerpo, "hashes_por_segundo": max(1, round(cuerpo["hashes_por_segundo"] * factor))}
            log.warning("Trampa %s: declaro %d hashes/s", self.trampa, cuerpo["hashes_por_segundo"])
        return super()._insistir(metodo, ruta, cuerpo)

    def un_rango(self):
        if self.trampa == "gpu-falsa":
            return False  # avisa (el keep-alive del Minero) y nunca pide trabajo
        if self.trampa in ("acaparar", "retener"):
            return self._retener(10 if self.trampa == "acaparar" else 1)
        if self.trampa == "sin-solucion":
            codigo, rango = self._insistir("GET", "/tasks/next")
            if codigo != 200 or not rango:
                return False
            log.warning("Trampa: contesto «sin solución» en el rango %s sin buscar", rango["rango"])
            self._insistir("POST", "/results", {"tarea": rango["tarea"], "rango": rango["rango"],
                                                "minero": self.cuenta, "sin_solucion": True})
            return True
        if self.trampa == "guardar":
            return self._guardar()
        if self.trampa == "rangos-ajenos":
            return self._rangos_ajenos()
        return super().un_rango()

    def _rangos_ajenos(self):
        """Pide un rango para saber el id de la tarea y la da por agotada con «sin solución» de rangos que no son
        suyos (la devolución, punto 2). Después mina el suyo como un honesto: el gateway le vuelve a dar el mismo."""
        codigo, rango = self._insistir("GET", "/tasks/next")
        if codigo != 200 or not rango:
            return False
        if rango["tarea"] not in self.tareas_atacadas:
            self.tareas_atacadas.add(rango["tarea"])
            codigos = {}
            for numero in AJENOS:
                if numero != rango["rango"]:
                    c, _ = self.gateway.pedir("POST", "/results", {"tarea": rango["tarea"], "rango": numero,
                                                                   "minero": self.cuenta, "sin_solucion": True})
                    codigos[c] = codigos.get(c, 0) + 1
            log.warning("Trampa: «sin solución» de %d rangos ajenos de la tarea %s; el gateway contestó %s",
                        sum(codigos.values()), rango["tarea"], json.dumps(dict(sorted(codigos.items()))))
        return super().un_rango()

    def _retener(self, pedidos):
        """Pide `pedidos` rangos y los retiene mandando keep-alive, sin minar, hasta que se los saquen."""
        rangos = {}
        for _ in range(pedidos):
            codigo, rango = self._insistir("GET", "/tasks/next")
            if codigo == 200 and rango:
                rangos[(rango["tarea"], rango["rango"])] = rango
        if not rangos:
            return False
        log.warning("Trampa: pedí %d rangos y me dieron %d distintos: los retengo sin minar", pedidos, len(rangos))
        inicio = time.monotonic()
        while rangos and not self.detenido.is_set():
            self.detenido.wait(self.latido)
            for clave in list(rangos):
                codigo, respuesta = self.gateway.pedir("POST", "/keepalive", {
                    "minero": self.cuenta, "hashes_por_segundo": 1_000_000,
                    "rango": {"tarea": clave[0], "rango": clave[1]}})
                if codigo == 403 or (respuesta or {}).get("cortar"):
                    log.warning("Trampa: me sacaron el rango %s a los %.0f s", clave[1], time.monotonic() - inicio)
                    del rangos[clave]
        return True

    def _guardar(self):
        """Busca de verdad, pero si encuentra el nonce contesta «sin solución» (block withholding)."""
        codigo, rango = self._insistir("GET", "/tasks/next")
        if codigo != 200 or not rango:
            return False
        nonce, cortado = self.buscador.buscar(rango["bloque"], rango["desde"], rango["hasta"], rango["prefijo"])
        shares = self.buscador.shares.tomar()
        if cortado:
            return True
        if nonce is not None:
            log.warning("Trampa: encontré el nonce %d (%s) y me lo guardo", nonce,
                        contrato.hash_bloque(rango["bloque"], nonce))
        self._insistir("POST", "/results", {"tarea": rango["tarea"], "rango": rango["rango"], "minero": self.cuenta,
                                            "sin_solucion": True, "shares": shares})
        return True
