// Tests del Worker con node:test, sin wrangler: se reemplaza el fetch global por uno que anota el pedido.
//   node --test pilar3/cloudflare/
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import worker from "./worker.mjs";

const PUERTA = "https://blockchain-chimera.cuenta.workers.dev";
let enviado;
let responder;

beforeEach(() => {
  enviado = null;
  responder = () => new Response("ok");
  globalThis.fetch = async (url, init) => {
    enviado = { url: url.toString(), ...init };
    return responder();
  };
});

test("reenvía ruta y query al origen", async () => {
  await worker.fetch(new Request(`${PUERTA}/api/saldo/abc?desde=3`));
  assert.equal(enviado.url, "https://blockchain-chimera.duckdns.org/api/saldo/abc?desde=3");
  assert.equal(enviado.method, "GET");
  assert.equal(enviado.body, null);
});

test("el origen se puede cambiar con la variable ORIGEN", async () => {
  await worker.fetch(new Request(`${PUERTA}/salud/backend`), { ORIGEN: "https://otro.example" });
  assert.equal(enviado.url, "https://otro.example/salud/backend");
});

test("POST con cuerpo y encabezados", async () => {
  await worker.fetch(new Request(`${PUERTA}/api/transacciones`, {
    method: "POST", body: '{"monto":5}', headers: { "Content-Type": "application/json" },
  }));
  assert.equal(enviado.method, "POST");
  assert.equal(enviado.headers.get("content-type"), "application/json");
  assert.equal(enviado.headers.get("x-forwarded-host"), "blockchain-chimera.cuenta.workers.dev");
  assert.equal(enviado.headers.get("x-forwarded-proto"), "https");
  assert.equal(await new Response(enviado.body).text(), '{"monto":5}');
  assert.equal(enviado.redirect, "manual");
});

test("devuelve el estado, los encabezados y el cuerpo del origen", async () => {
  responder = () => new Response('{"backend":"caido"}', {
    status: 503, headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
  const respuesta = await worker.fetch(new Request(`${PUERTA}/salud/backend`));
  assert.equal(respuesta.status, 503);
  assert.equal(respuesta.headers.get("access-control-allow-origin"), "*");
  assert.equal(await respuesta.text(), '{"backend":"caido"}');
});

test("reescribe las redirecciones al origen para que sigan en la puerta", async () => {
  responder = () => new Response(null, {
    status: 308, headers: { Location: "https://blockchain-chimera.duckdns.org/alta.html" },
  });
  const respuesta = await worker.fetch(new Request(`${PUERTA}/alta`));
  assert.equal(respuesta.headers.get("location"), `${PUERTA}/alta.html`);
});

test("no toca las redirecciones a otros sitios", async () => {
  responder = () => new Response(null, { status: 302, headers: { Location: "https://github.com/x" } });
  const respuesta = await worker.fetch(new Request(`${PUERTA}/repo`));
  assert.equal(respuesta.headers.get("location"), "https://github.com/x");
});

test("502 si el origen no responde", async () => {
  globalThis.fetch = async () => { throw new Error("timeout"); };
  const respuesta = await worker.fetch(new Request(`${PUERTA}/api/cadena`));
  assert.equal(respuesta.status, 502);
  assert.match((await respuesta.json()).error, /timeout/);
});

test("pasa la IP del cliente en X-Real-Ip, y no la que manda el cliente (BC-94)", async () => {
  const pedido = new Request(`${PUERTA}/api/transacciones`, {
    method: "POST", body: "{}", headers: { "CF-Connecting-IP": "203.0.113.7", "X-Real-Ip": "1.2.3.4" },
  });
  await worker.fetch(pedido);
  assert.equal(new Headers(enviado.headers).get("X-Real-Ip"), "203.0.113.7");
});
