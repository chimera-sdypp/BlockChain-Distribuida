# Herramientas

## Auditoría de la cadena (BC-61)

```bash
cd pilar2
REDIS_HOST=... REDIS_CLAVE=... python -m herramientas.auditar --organizador <cuenta del organizador>
```

Recorre la cadena desde el génesis sin confiar en nada de lo que guardó el sistema, y verifica:

- cada hash: sale del texto (`previous_hash;timestamp;raíz;`) y el nonce, cumple el prefijo y encadena con el anterior;
- la coinbase: es la primera transacción, del ganador y por `10 × largo del prefijo`;
- cada transferencia: la firma, que el nonce sea el siguiente de su cuenta y que el origen tuviera saldo;
- cada carga: la firma del organizador (`--organizador` u `ORGANIZADOR_CLAVE`) y que su número siga al anterior;
- cada pago de participación: que esté en el primer bloque de la época y coincida con `epoca:<n>`, cuyos montos tienen que salir de repartir el fondo según sus shares y rachas;
- que ninguna transacción esté en dos bloques.

Después recalcula todos los saldos y los compara con `saldos` de Redis, y que `nonces` no esté atrás de la cadena. Sale con 0 si da todo igual y con 1 si encontró algo.

**Para la demo:** `HINCRBY saldos <cuenta> 1000` en Redis, y la auditoría lo encuentra: los saldos no se creen, salen de la cadena.

Tests: `INTEGRACION=1 python -m pytest herramientas` arma con el NCT una cadena de 52 bloques con cargas, transferencias, coinbases y participación, la audita y después la adultera de varias formas.
