# Claves (BC-55)

El «servicio que genera las claves»: un CLI para las cuentas del contrato v2 (claves Ed25519). La privada queda en un archivo con permisos 600 y no sale de ahí; lo que se manda es la transacción firmada.

| Comando | Qué hace |
|---|---|
| `generar ARCHIVO` | Genera un par, guarda la privada (no pisa una que exista) y muestra la cuenta (la pública) |
| `publica ARCHIVO` | La cuenta de esa clave |
| `exportar ARCHIVO` | La privada en JWK (RFC 8037), para importarla en la sección «Transferir» de la página |
| `firmar ARCHIVO --destino C --monto N [--nonce N] [--url U] [--enviar]` | Arma y firma una transferencia. Sin `--nonce`, le pide al backend el último y usa el siguiente |
| `cargar ARCHIVO --destino C --monto N --url U --enviar` | Una carga de saldo (`carga-<n>`) firmada con la clave del organizador |
| `aprobar CUENTA --tipo usuario\|minero --nombre X` | La aprueba en `cuentas`, directo en Redis (`REDIS_*`). Es lo que hace el organizador hasta que esté el panel (BC-58). En un cluster: `k8s/aprobar.sh` |
| `rafaga --url U -n N ARCHIVO…` | N transferencias en ronda entre esas cuentas, llevando el nonce de cada una, y las sigue hasta que se cierran |

```bash
cd pilar2
python -m claves generar ~/claves/zoe.clave                       # muestra la cuenta de zoe
k8s/aprobar.sh <cuenta de zoe> usuario zoe                         # el organizador la aprueba
python -m claves cargar ~/claves/organizador.clave --destino <cuenta de zoe> --monto 500 --url http://localhost --enviar
python -m claves firmar ~/claves/zoe.clave --destino <cuenta> --monto 10 --url http://localhost --enviar
python -m claves exportar ~/claves/zoe.clave                       # para pegarla en la página
```

La ráfaga de la demo con las cuentas de prueba del génesis: `pilar3/pruebas/rafaga.sh http://<Ingress> 20`.

Tests: `python -m pytest claves` (con `INTEGRACION=1`, también `aprobar` contra Redis).
