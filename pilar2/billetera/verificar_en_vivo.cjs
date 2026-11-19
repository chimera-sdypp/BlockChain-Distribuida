// El verificador de la billetera contra un sistema de verdad, desde la terminal (lo mismo que hace la página):
//   node billetera/verificar_en_vivo.cjs http://localhost [cuenta]
// Baja la cadena entera, la verifica, calcula la finalidad y compara con /api/prueba y los nodos /nodos/0 y /nodos/1.
"use strict";
const Verificador = require("./verificador.js");

const base = (process.argv[2] || "http://localhost").replace(/\/+$/, "");
const cuenta = process.argv[3];
const pedir = async (url) => { const r = await fetch(url); return r.ok ? r.json() : null; };

(async () => {
  const estado = Verificador.estadoNuevo();
  const reglas = { shares: async (e) => (await pedir(`${base}/api/epocas/${e}`))?.detalle ?? null };
  const hashes = {}, firmas = [];
  const inicio = Date.now();
  for (;;) {
    const lote = await pedir(`${base}/api/bloques?orden=asc&desde=${estado.altura + 1}&limite=100`);
    if (!lote || !lote.length) break;
    for (const bloque of lote) {
      const { problemas, avisos } = await Verificador.aplicarBloque(estado, bloque, reglas);
      if (problemas.length) { console.log(`✗ bloque ${estado.altura + 1} NO verifica: ${problemas.join("; ")}`); process.exit(1); }
      for (const a of avisos) console.log(`· bloque ${estado.altura}: ${a}`);
      hashes[bloque.altura] = bloque.block_hash;
      for (const f of bloque.firmas || []) firmas.push({ ...f, altura: bloque.altura, block_hash: bloque.block_hash });
    }
    if (lote.length < 100) break;
  }
  const raiz = Verificador.raiz(estado);
  console.log(`✓ ${estado.altura + 1} bloques verificados en ${(Date.now() - inicio) / 1000} s; raíz de estado ${raiz}`);
  const final = await pedir(`${base}/api/finalidad`);
  const f = await Verificador.finalidad(estado, [...firmas, ...Object.values(final?.firmas || {})], hashes);
  console.log(`  finalidad: altura ${f.altura} (${f.firmasValidas} de ${f.validadores} validadores, umbral ${f.umbral})`);
  if (cuenta) {
    const p = await pedir(`${base}/api/prueba/${cuenta}`);
    const hoja = (Verificador.hojas(estado).find(([k]) => k === cuenta) || [])[1] ?? null;
    console.log(`  saldo de ${cuenta.slice(0, 12)}…: ${estado.saldos[cuenta] || 0}; /api/prueba ${p && p.altura === estado.altura && p.raiz === raiz && p.hoja === hoja ? "coincide" : "NO coincide (u otra altura)"}`);
  }
  for (const n of [0, 1]) {
    const a = await pedir(`${base}/nodos/${n}/api/atestacion`);
    if (!a) { console.log(`· nodo ${n}: no respondió`); continue; }
    const firma = await Verificador.verificarFirma(a.nodo, Verificador.mensajeAtestacion(a), a.firma);
    const igual = a.altura === estado.altura ? a.punta === estado.punta && a.raiz === raiz : hashes[a.altura] === a.punta;
    console.log(`  nodo ${n} (${a.nodo.slice(0, 12)}…): altura ${a.altura}, firma ${firma ? "válida" : "MALA"}, ${igual ? "coincide" : "NO coincide"}`);
  }
})();
