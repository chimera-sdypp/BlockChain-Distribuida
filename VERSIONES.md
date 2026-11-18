# Versiones

Versión actual: **0.2.0** (18/11/2026)

[SemVer](https://semver.org/lang/es/) 0.x hasta la entrega: cada pase de `dev` a `main` sube la versión acá y deja un tag `vX.Y.Z` en `main`.

## 0.2.0 — 18/11/2026

- Contrato v2: cuentas Ed25519, transacciones firmadas y coinbase (#39, BC-53).
- Claves en mineros y usuarios; recompensas por shares y épocas (#40, BC-55 a 57 y BC-61 a 63).
- Panel del organizador: pedidos de alta, aprobación con saldo inicial y bajas (#41, BC-58). Recuerda la clave en el navegador hasta «Salir» (#52).
- Ajuste de dificultad por tiempo y techo por potencia viva (#44, BC-60), integrado con el criterio de potencia y los rangos por minero en Grafana (#45 y #46, BC-59).
- HTTPS con DuckDNS y Let's Encrypt: https://blockchain-chimera.duckdns.org (#42).
- Pruebas: bulks de 1 a 100.000 transacciones (#25 y #48, BC-38), fragmentación del pool del 1 % al 50 % (#43, BC-40), capturas de Grafana del reparto de rangos (#49, BC-59).
- Arreglos: una transacción no puede entrar en dos bloques (#38); el gateway ya no se reinicia en bucle con Redis grande, y el bench GPU se mide en caliente (#51).

## 0.1.0 — 13/11/2026

- Primera versión en GKE: Pilares 1 a 3 con el contrato v1, notebook de Colab, prueba de prefijos, tareas vencidas e IP fija del gateway (#35 y #37).
