# Herramientas

## Auditoría de la cadena (BC-61, BC-80)

```bash
cd pilar2
REDIS_HOST=... REDIS_CLAVE=... python -m herramientas.auditar --organizador <cuenta del organizador> [--reparar]
```

Recorre la cadena desde el génesis con [`comun/estado.py`](../comun/estado.py), el mismo código que usan el NCT y los nodos verificadores, y verifica cada hash (con la raíz de transacciones y la de estado), la coinbase, cada firma y nonce, las altas, bajas y permisos, las cargas, y cada pago de participación contra la lista de shares publicada (`epoca:<n>`) y la raíz que quedó en la cadena (`shares-<n>`). Después compara el caché de Redis (`saldos`, `nonces:confirmados`, `cuentas`, `permisos`) con el estado que sale de la cadena y que `nonces` no esté atrás. Sale con 0 si da todo igual y con 1 si encontró algo; con `--reparar`, gana la cadena y pisa el caché (lo mismo que hace el NCT solo cada 60 s).

**Para la demo:** `HINCRBY saldos <cuenta> 1000` en Redis, y la auditoría lo encuentra: los saldos no se creen, salen de la cadena.

Tests: `INTEGRACION=1 python -m pytest herramientas` arma con el NCT una cadena de 52 bloques con cargas, transferencias, coinbases y participación, la audita y después la adultera de varias formas.
