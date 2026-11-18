// BC-38: manda un bulk de transacciones al backend, en lotes de hasta 1000 (POST /api/transacciones/lote),
// repartidos entre VUS usuarios virtuales.
//
//   k6 run -e BASE_URL=http://<Ingress> -e LOTES_ARCHIVO=lotes.json -e VUS=10 bulk.js
//
// Contrato v2: las transacciones van firmadas por su origen y con su nonce, así que no se arman acá:
// las firma correr.py antes de medir y las deja en LOTES_ARCHIVO (una lista de lotes). Si dos lotes del
// mismo origen llegan desordenados, el NCT espera el nonce que falta. Lo normal es correrlo desde
// correr.py, que además mide la confirmación y guarda el CSV.
// Variables: BASE_URL, LOTES_ARCHIVO, CANTIDAD (para el resumen), VUS (10), MAX_DURACION (30m) y
// RESUMEN (archivo JSON con las métricas, para correr.py).

import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';
import { SharedArray } from 'k6/data';
import { Counter } from 'k6/metrics';

const BASE = (__ENV.BASE_URL || 'http://localhost').replace(/\/+$/, '');
const VUS = parseInt(__ENV.VUS || '10', 10);
// Una sola copia en memoria para todos los VUS: con 100.000 transacciones son ~40 MB.
const LOTES_FIRMADOS = new SharedArray('lotes', () => JSON.parse(open(__ENV.LOTES_ARCHIVO)));
const LOTES = LOTES_FIRMADOS.length;
const CANTIDAD = parseInt(__ENV.CANTIDAD || '0', 10);
const VUS_REALES = Math.max(1, Math.min(VUS, LOTES)); // no tiene sentido más VUS que lotes

export const options = {
    scenarios: {
        bulk: {
            // Cada iteración es un lote; los VUS se los reparten hasta mandar todos.
            executor: 'shared-iterations',
            vus: VUS_REALES,
            iterations: LOTES,
            maxDuration: __ENV.MAX_DURACION || '30m',
        },
    },
    summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(50)', 'p(95)', 'p(99)'],
};

const aceptadas = new Counter('tx_aceptadas');
const rechazadas = new Counter('tx_no_aceptadas');

export default function () {
    const n = exec.scenario.iterationInTest; // número de lote, único entre todos los VUS
    const txs = LOTES_FIRMADOS[n];
    const cuantas = txs.length;
    const respuesta = http.post(`${BASE}/api/transacciones/lote`, JSON.stringify(txs), {
        headers: { 'Content-Type': 'application/json' },
        tags: { name: 'lote' },
        timeout: '120s',
    });
    const ok = check(respuesta, { 'lote aceptado (202)': (r) => r.status === 202 });
    (ok ? aceptadas : rechazadas).add(cuantas);
}

export function handleSummary(data) {
    const salida = {};
    if (__ENV.RESUMEN) {
        salida[__ENV.RESUMEN] = JSON.stringify(data);
    }
    const d = data.metrics.http_req_duration.values;
    const lineas = [
        `bulk de ${CANTIDAD} tx en ${LOTES} lote(s), ${VUS_REALES} VU(s)`,
        `  latencia por lote: p50 ${d['p(50)'].toFixed(1)} ms · p95 ${d['p(95)'].toFixed(1)} ms · p99 ${d['p(99)'].toFixed(1)} ms`,
        `  pedidos: ${data.metrics.http_reqs.values.count} (${data.metrics.http_reqs.values.rate.toFixed(1)}/s) · ` +
            `fallidos: ${(100 * data.metrics.http_req_failed.values.rate).toFixed(1)} %`,
        '',
    ];
    salida.stdout = lineas.join('\n');
    return salida;
}
