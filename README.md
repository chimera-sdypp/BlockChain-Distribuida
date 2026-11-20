# Blockchain distribuida con minería en CUDA

TP Integrador de Sistemas Distribuidos y Programación Paralela (UNLu, 2C 2026), grupo Cerberus: Salvador Baez, Mateo Nomico y Tomás Resnik.

Transferencias firmadas entre cuentas sobre una cadena de bloques con prueba de trabajo MD5. La minan GPU en CUDA y, cuando no hay GPU, mineros CPU en VMs que se crean solas. Corre en GKE, y la cadena se verifica desde afuera de la nube: nodos verificadores, validadores opcionales y una billetera descargable.

| Parte | Qué hay |
|---|---|
| [`pilar1/`](pilar1/README.md) | El minero MD5 en CUDA, del hola mundo al minero acotado a un rango, y la comparativa CPU contra GPU |
| [`pilar2/`](pilar2/README.md) | Los servicios (backend, frontend, NCT, TrP, gateway, escalador), los mineros, el nodo verificador, la herramienta de claves y la billetera. El formato de todo está en [`CONTRATO.md`](pilar2/CONTRATO.md) |
| [`pilar3/`](pilar3/README.md) | OpenTofu, los 4 pipelines, los manifiestos de GKE, las [pruebas de carga](pilar3/pruebas/README.md) y la [demo](pilar3/demo/README.md) |
| [`VERSIONES.md`](VERSIONES.md) | Qué entró en cada versión (tag `vX.Y.Z` en `main`) |

## Desplegado

- La página y la API: <https://blockchain-chimera.duckdns.org>. Hay un health-check por servicio en `/salud/<servicio>` (backend, frontend, nct, trp y gateway).
- Los mineros se conectan al gateway: `http://136.80.87.183:8081`.
- Los nodos verificadores: `/nodos/0` y `/nodos/1`.
- La billetera es [`billetera.html`, en el último release](https://github.com/chimera-sdypp/BlockChain-Distribuida/releases/latest). Se comprueba con el SHA-256 de las notas y se abre desde el disco.

## Probar

```bash
cd pilar2 && python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                           # unitarios; con INTEGRACION=1, contra RabbitMQ y Redis reales
```

Cómo levantarlo en k3s está en [`pilar2/README.md`](pilar2/README.md), y en GKE, en [`pilar3/README.md`](pilar3/README.md).
