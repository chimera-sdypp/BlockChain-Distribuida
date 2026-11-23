# Blockchain distribuida con minería en CUDA

TP Integrador de Sistemas Distribuidos y Programación Paralela (UNLu, 2C 2026), grupo Cerberus: Salvador Baez, Mateo Nomico y Tomás Resnik.

Transferencias firmadas entre cuentas sobre una cadena de bloques con prueba de trabajo MD5. La minan GPU en CUDA y, cuando no hay GPU, mineros CPU en VMs que se crean solas. Corre en GKE, y la cadena se verifica desde afuera de la nube: nodos verificadores, validadores opcionales y una billetera descargable.

| Parte | Qué hay |
|---|---|
| [`pilar1/`](pilar1/README.md) | El minero MD5 en CUDA, del hola mundo al minero acotado a un rango, y la comparativa CPU contra GPU |
| [`pilar2/`](pilar2/README.md) | Los servicios (backend, frontend, NCT, TrP, gateway, escalador), los mineros, el nodo verificador, la herramienta de claves y la billetera. El formato de todo está en [`CONTRATO.md`](pilar2/CONTRATO.md) |
| [`pilar3/`](pilar3/README.md) | OpenTofu, los 4 pipelines, los manifiestos de GKE y las [pruebas de carga](pilar3/pruebas/README.md) |
| [`informe/`](informe/informe-tpi-cerberus.pdf) | El informe final, en PDF |
| [Video](#video-de-la-demo) | La demo grabada sobre GKE, en tres partes |
| [`VERSIONES.md`](VERSIONES.md) | Qué entró en cada versión (tag `vX.Y.Z` en `main`) |

## Video de la demo

Grabada sobre GKE en tres partes, para ver en orden. Los videos son adjuntos del [release v0.8.4](https://github.com/chimera-sdypp/BlockChain-Distribuida/releases/tag/v0.8.4) (GitHub no acepta archivos de más de 100 MB en el repo).

| Video | Qué muestra |
|---|---|
| Video 1 (falta) | El sistema en pie, el alta de una cuenta desde la billetera, una transferencia de punta a punta en Loki y lo que se rechaza |
| Video 2 (falta) | Una ráfaga de transferencias (qué escala y por qué), un pod que se mata y el operador que infla un saldo |
| [Video 3](https://github.com/chimera-sdypp/BlockChain-Distribuida/releases/download/v0.8.4/video3.mp4) | Salen las GPU y entra la VM, los mineros que mienten, los validadores 2 de 3 y el historial de dificultad |

Para sumar uno: `gh release upload v0.8.4 video1.mp4` y cambiar «Video 1 (falta)» por `[Video 1](https://github.com/chimera-sdypp/BlockChain-Distribuida/releases/download/v0.8.4/video1.mp4)`.

## Desplegado

- La página y la API: <https://blockchain-chimera.chimera-sdypp.workers.dev> (por Cloudflare, también desde la red de la universidad) o <https://blockchain-chimera.duckdns.org> (directo a GKE). Hay un health-check por servicio en `/salud/<servicio>` (backend, frontend, nct, trp y gateway).
- Los mineros se conectan al gateway: `http://136.80.87.183:8081`.
- Los nodos verificadores: `/nodos/0` y `/nodos/1`.
- La billetera es [`billetera.html`, en el último release](https://github.com/chimera-sdypp/BlockChain-Distribuida/releases/latest). Se comprueba con el SHA-256 de las notas y se abre desde el disco.

## Probar

```bash
cd pilar2 && python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                           # unitarios; con INTEGRACION=1, contra RabbitMQ y Redis reales
```

Cómo levantarlo en k3s está en [`pilar2/README.md`](pilar2/README.md), y en GKE, en [`pilar3/README.md`](pilar3/README.md).
