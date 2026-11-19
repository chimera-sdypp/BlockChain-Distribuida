# Claves (BC-55, BC-78)

La billetera de línea de comandos: las claves Ed25519 se generan y se usan en tu máquina, nunca en una página que sirve la nube. La privada queda en un archivo con permisos 600 (o en el `.jwk` que baja la billetera del navegador) y no sale de ahí: lo que se manda es la transacción firmada. Sólo necesita `cryptography` (`pip install -r comun/requirements.txt`).

| Comando | Qué hace |
|---|---|
| `generar ARCHIVO` | Genera un par, guarda la privada (no pisa una que exista) y muestra la cuenta (la pública) |
| `publica ARCHIVO` · `exportar ARCHIVO` | La cuenta de esa clave · la privada en JWK (RFC 8037), para el navegador |
| `alta ARCHIVO --tipo usuario\|minero\|validador --nombre X --url U` | Pide el alta (firmada: demuestra que tenés la privada) |
| `saldo ARCHIVO --url U` | Saldo, nonce y si la cuenta está aprobada, según el backend |
| `transferir ARCHIVO --destino C --monto N --url U` | Firma con el nonce siguiente, la manda y espera a que la valide el NCT |
| `firmar ARCHIVO --destino C --monto N [--nonce N] [--url U] [--enviar]` | Sólo arma y firma (con `--enviar`, la manda) |
| `rafaga --url U -n N ARCHIVO…` | N transferencias en ronda entre esas cuentas, y las sigue hasta que se cierran |

Para el organizador (contrato v3): cada decisión es un acta firmada con su clave que entra en la cadena.

| Comando | Qué hace |
|---|---|
| `pendientes --url U` | Los pedidos de alta |
| `aprobar ORG --cuenta C --tipo T --nombre X [--carga N] --url U` | `alta-<n>` (y, a un usuario, `carga-<n+1>`) |
| `baja ORG --cuenta C [--motivo M] --url U` | `baja-<n>` |
| `cargar ORG --destino C --monto N --url U --enviar` | `carga-<n>` |
| `permiso ORG --clave K --prefijo minero-cpu- --url U` | `permiso-<n>`: K da de alta y de baja sólo mineros con ese prefijo (el escalador). Prefijo vacío: lo revoca |
| `rechazar ORG --cuenta C --url U` · `reactivar ORG --cuenta C --url U` | Lo que no va a la cadena: borrar un pedido, reactivar un minero suspendido (pedidos firmados con `X-Organizador`) |

```bash
cd pilar2
python -m claves generar ~/claves/zoe.clave                                   # muestra la cuenta de zoe
python -m claves alta ~/claves/zoe.clave --nombre zoe --url https://blockchain-chimera.duckdns.org
python -m claves aprobar ~/claves/organizador.clave --cuenta <zoe> --nombre zoe --carga 500 --url https://…
python -m claves transferir ~/claves/zoe.clave --destino <cuenta> --monto 10 --url https://…
```

El panel `/organizador` muestra los pedidos, las cuentas y los suspendidos, y copia el comando de cada acción. En un cluster de prueba, `k8s/aprobar.sh <cuenta> <tipo> [nombre]` hace lo mismo con la clave de prueba. Tests: `python -m pytest claves`.
