// Lo que comparten las páginas: armar nodos, llamar a la API y firmar con WebCrypto Ed25519.
// La privada se importa como CryptoKey no exportable: no sale nunca del navegador ni se guarda.
"use strict";

const $ = (id) => document.getElementById(id);
const corto = (hash) => hash ? `${hash.slice(0, 10)}…` : "—";
const cuentaCorta = (cuenta) => cuenta === "emision" ? "emisión" : corto(cuenta);
const hora = (segundos) => new Date(segundos * 1000).toLocaleString();

function el(etiqueta, atributos = {}, ...hijos) {
  const nodo = document.createElement(etiqueta);
  for (const [clave, valor] of Object.entries(atributos)) {
    if (clave === "clase") nodo.className = valor; else nodo.setAttribute(clave, valor);
  }
  for (const hijo of hijos) nodo.append(hijo);
  return nodo;
}

async function api(ruta, opciones) {
  const respuesta = await fetch(ruta, opciones);
  const cuerpo = await respuesta.json().catch(() => ({}));
  return { ok: respuesta.ok, status: respuesta.status, cuerpo };
}

const errorDe = (cuerpo) => [].concat(cuerpo.errores ? Object.values(cuerpo.errores).flat() : [],
                                     cuerpo.error || []).join(" · ") || "datos inválidos";

// ── Claves ──

const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
const deBase64url = (texto) => Uint8Array.from(atob(texto.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
// WebCrypto sólo existe en HTTPS o en localhost: por HTTP plano, `crypto.subtle` no está y no se puede firmar.
const AVISO_NAVEGADOR = window.isSecureContext
  ? "Este navegador necesita Ed25519 en WebCrypto (Chrome 137, Firefox 129 o Safari 17)."
  : "La página no está en HTTPS y el navegador no deja firmar: entrá por https:// (o por localhost).";

// El JWK de `python -m claves exportar` (o el que baja la página «Pedir el alta») → {clave, cuenta}.
async function importarClave(texto) {
  const jwk = JSON.parse(texto);
  if (jwk.kty !== "OKP" || jwk.crv !== "Ed25519" || !jwk.d || !jwk.x) throw new Error("no es una clave Ed25519");
  const clave = await crypto.subtle.importKey("jwk", { kty: "OKP", crv: "Ed25519", d: jwk.d, x: jwk.x },
                                              { name: "Ed25519" }, false, ["sign"]);
  return { clave, cuenta: hex(deBase64url(jwk.x)) };
}

async function firmar(clave, texto) {
  return hex(new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, clave, new TextEncoder().encode(texto))));
}

// Lo que firma el origen de una transacción (contrato v2); en una carga el origen es «emision».
const mensajeTransaccion = (tx) => `${tx.origen}:${tx.destino}:${tx.monto}:${tx.nonce}:${tx.timestamp}`;

// Un pedido firmado como los de los mineros: método, ruta, hora y cuerpo (BC-57); `quien` es el encabezado.
async function pedidoFirmado(firmante, quien, metodo, ruta, datos) {
  const cuerpo = datos === undefined ? "" : JSON.stringify(datos);
  const tiempo = Math.floor(Date.now() / 1000);
  const firma = await firmar(firmante.clave, `${metodo}\n${ruta}\n${tiempo}\n${cuerpo}`);
  return api(ruta, { method: metodo, body: cuerpo || undefined, headers: {
    "content-type": "application/json", [quien]: firmante.cuenta, "X-Tiempo": String(tiempo), "X-Firma": firma } });
}

function bajarArchivo(nombre, contenido) {
  const enlace = el("a", { href: URL.createObjectURL(new Blob([contenido], { type: "application/octet-stream" })),
                           download: nombre });
  document.body.append(enlace);
  enlace.click();
  enlace.remove();
  setTimeout(() => URL.revokeObjectURL(enlace.href), 10_000);
}
