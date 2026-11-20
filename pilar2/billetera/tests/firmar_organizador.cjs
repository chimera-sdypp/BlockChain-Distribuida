// Firma con el modo organizador de la billetera lo que pide test_organizador.py, con WebCrypto como el navegador:
//   node firmar_organizador.cjs entrada.json  →  {txs: [...], pedidos: [firma, …]}
// entrada = {jwk, txs: [{tipo: carga|alta|baja|permiso, numero, destino, monto?, datos?, timestamp}],
//            pedidos: [{metodo, ruta, tiempo, cuerpo}]}
"use strict";
const fs = require("fs");
const Verificador = require("../verificador.js");

const hex = (bytes) => Buffer.from(bytes).toString("hex");

(async () => {
  const entrada = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const { d, x } = entrada.jwk;
  const privada = await crypto.subtle.importKey("jwk", { kty: "OKP", crv: "Ed25519", d, x }, { name: "Ed25519" }, false, ["sign"]);
  const firmar = async (texto) => hex(await crypto.subtle.sign({ name: "Ed25519" }, privada, new TextEncoder().encode(texto)));
  const txs = [];
  for (const t of entrada.txs) {
    const tx = t.tipo === "carga" ? Verificador.armarCarga(t.numero, t.destino, t.monto, t.timestamp)
                                  : Verificador.armarActa(t.tipo, t.numero, t.destino, t.datos, t.timestamp);
    txs.push({ ...tx, firma: await firmar(Verificador.mensajeDe(tx)) });
  }
  const pedidos = [];
  for (const p of entrada.pedidos) pedidos.push(await firmar(Verificador.mensajePedido(p.metodo, p.ruta, p.tiempo, p.cuerpo)));
  process.stdout.write(JSON.stringify({ txs, pedidos }));
})();
