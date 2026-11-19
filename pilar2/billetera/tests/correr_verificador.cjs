// Corre el verificador de la billetera sobre una cadena en JSON (lo usa test_verificador.py):
//   node correr_verificador.cjs entrada.json  →  {bloques: [{altura, problemas, avisos}], estado, raiz, hojas, …}
// entrada = {bloques, organizador?, shares?: {epoca: detalle}, firmas?: [...], md5?: [textos], prueba?: {hoja, rama}}
"use strict";
const fs = require("fs");
const Verificador = require("../verificador.js");

(async () => {
  const entrada = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const reglas = { organizador: entrada.organizador || "", shares: async (e) => (entrada.shares || {})[e] ?? null };
  const estado = Verificador.estadoNuevo();
  const resultados = [], hashes = {};
  for (const bloque of entrada.bloques || []) {
    const { problemas, avisos } = await Verificador.aplicarBloque(estado, bloque, reglas);
    resultados.push({ altura: estado.altura + (problemas.length ? 1 : 0), problemas, avisos });
    if (problemas.length) break;
    hashes[estado.altura] = estado.punta;
  }
  const salida = {
    bloques: resultados, estado, raiz: Verificador.raiz(estado), hojas: Verificador.hojas(estado).map(([, h]) => h),
    md5: (entrada.md5 || []).map(Verificador.md5),
  };
  if (entrada.firmas) salida.finalidad = await Verificador.finalidad(estado, entrada.firmas, hashes);
  if (entrada.prueba) salida.prueba = Verificador.raizDeRama(entrada.prueba.hoja, entrada.prueba.rama);
  process.stdout.write(JSON.stringify(salida));
})();
