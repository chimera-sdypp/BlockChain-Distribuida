// BC-38: manda un bulk de CANTIDAD transacciones al backend, en lotes de hasta 1000
// (POST /api/transacciones/lote), repartidos entre VUS usuarios virtuales.
//
//   k6 run -e BASE_URL=http://<Ingress> -e CANTIDAD=10000 -e VUS=10 bulk.js
//
// Lo normal es correrlo desde correr.py, que además mide la confirmación y guarda el CSV.
// Variables: BASE_URL, CANTIDAD, VUS (10), LOTE (1000, el máximo del backend),
// ORIGENES (alice,bob,carol: necesitan saldo), PREFIJO_DESTINO (marca las tx de la corrida),
// MAX_DURACION (30m) y RESUMEN (archivo JSON con las métricas, para correr.py).

import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const BASE = (__ENV.BASE_URL || 'http://localhost').replace(/\/+$/, '');
const CANTIDAD = parseInt(__ENV.CANTIDAD || '1', 10);
const LOTE = Math.min(parseInt(__ENV.LOTE || '1000', 10), 1000);
const VUS = parseInt(__ENV.VUS || '10', 10);
const ORIGENES = (__ENV.ORIGENES || 'alice,bob,carol').split(',').map((o) => o.trim()).filter(Boolean);
const PREFIJO = __ENV.PREFIJO_DESTINO || 'b38-prueba';
const DESTINOS = 100; // destinos distintos por corrida: PREFIJO-0 ... PREFIJO-99
const LOTES = Math.ceil(CANTIDAD / LOTE);
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
    const desde = n * LOTE;
    const cuantas = Math.min(LOTE, CANTIDAD - desde);
    const txs = [];
    for (let j = 0; j < cuantas; j++) {
        const k = desde + j;
        txs.push({ origen: ORIGENES[k % ORIGENES.length], destino: `${PREFIJO}-${k % DESTINOS}`, monto: 1 });
    }
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
