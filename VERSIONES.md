# Versiones

Versión actual: **0.7.0** (19/11/2026)

[SemVer](https://semver.org/lang/es/) 0.x hasta la entrega: cada pase de `dev` a `main` sube la versión acá y deja un tag `vX.Y.Z` en `main`.

## 0.7.0 — 19/11/2026

Billetera propia, todo en la cadena y nodos que verifican (epic BC-77, #76). **Contrato v3: pide una cadena nueva.**

- **Actas en la cadena** (BC-79): altas, bajas y permisos son transacciones firmadas por el organizador (`alta-<n>`, `baja-<n>`, `permiso-<n>`); el panel ya no pide la privada. El escalador tiene su propia clave con permiso para `minero-cpu-*`.
- **Estado desde la cadena** (BC-80): `comun/estado.py`; Redis es caché y el NCT lo revisa cada 60 s. Cada bloque lleva la raíz de estado (raíz = MD5(raíz_tx|raíz_estado)); vector v3 verificado en Python, md5sum y CUDA.
- **Shares publicados** (BC-82): la raíz de la lista de cada época queda en `shares-<e>` y la lista en `/api/epocas/<n>`.
- **Nodos verificadores** (BC-83): `python -m nodo`, con su copia en SQLite y atestaciones firmadas; 2 en el cluster (`/nodos/0` y `/nodos/1`).
- **Validadores** (BC-86): opcionales, dados de alta por acta; con 2 de 3 firmas el bloque queda finalizado y una doble firma deja la prueba.
- **Billetera** (BC-78, BC-84): `python -m claves` y `billetera.html` descargable (con su SHA-256 en el release), que verifica la cadena en el navegador.
- **Pruebas** (BC-85): `pilar3/pruebas/operador.py`, 8 ataques del operador: 16 de 16 en el k3s. Modelo de confianza en `pilar2/CONTRATO.md`.

## 0.6.1 — 19/11/2026

- Arreglo: el gateway no mide los préstamos de menos de 2 s. Con un prefijo chico para la potencia del minero (o una GPU, que recorre un rango en décimas), el préstamo duraba lo que tarda la red y la medición daba una potencia ridícula: el TrP recalculaba el prefijo sin motivo (#73).
- `dificultad.py` busca los mineros en el gateway, sube `ESPACIO_NONCES` con el tope y toma la duración que mide el NCT (#73). Convergencia del ajuste corrida en el k3s con la cadena reiniciada: de 2 a 7 de a un carácter cada 5 bloques, y a 6 en el acto al salir el minero más potente (#74).

## 0.6.0 — 19/11/2026

Dificultad por tiempo y mineros que mienten (epic BC-72). Incluye la 0.5.0, que no había pasado a `main`.

- **Prefijo por tiempo** (#69, BC-73): la potencia declarada (`bench`) se usa sólo al arrancar y cuando cambia 4 veces o más; el resto del tiempo manda el ajuste por tiempo, que ya no frena un bench bajo. Bajada de emergencia: un carácter menos por cada 240 s sin bloque. La `duracion` del bloque cuenta los rearmados, y el TrP no arranca si `ESPACIO_NONCES` no alcanza para el prefijo.
- **Potencia medida y préstamos** (#70, BC-74): un rango a la vez por minero; el gateway mide la potencia con los shares (`min(bench, medido)`) y la usan el TrP y el escalador; cada préstamo tiene un plazo según esa potencia. El escalador ya no cree en un `tipo: gpu` que no mina, y levanta VMs si hace 5 minutos que no sale un bloque.
- **`sin_solucion` falsos** (#71, BC-75): chequeo estadístico y reverificación por muestreo; el minero queda suspendido y el organizador lo reactiva o le da de baja desde el panel. Alerta `MineroSuspendido`.
- **Pruebas** (#72, BC-76): minero tramposo (`TRAMPA`, apagado por defecto) y `pilar3/pruebas/trampas.py`: 10 de 10 en el k3s.

## 0.5.0 — 18/11/2026

- Historial por minero (BC-54): el gateway lleva el tiempo conectado, la fecha de alta y quién entra y quién sale (con una línea en el log), y Grafana suma la fila «Historial de mineros» con una tabla por minero, sus curvas y las altas, conexiones y desconexiones desde Loki. Los mineros salen de la API de la app: `/api/mineros` pasa a ser `GET /mineros` del gateway, y la página muestra sólo la blockchain (#65).
- Grafana más limpio: CPU y memoria por servicio en vez de por pod, selector de mineros vivos o todos, y los logs separados en errores, advertencias, bloques y épocas, y escalado y dificultad; el log completo queda en una fila plegada (#66).
- Arreglo: el hashrate que mandan los mineros en el keep-alive ya no baja solo cuando se quedan sin trabajo, y un minero que todavía no minó cuenta con su bench (#67).
- Diagramas generales de los pilares 2 y 3 en sus README (#64).

## 0.4.0 — 18/11/2026

- Los mineros se vuelven a presentar si el gateway los pierde (por ejemplo, al reiniciar la cadena): ante un 403 insisten sólo con el `/hello`, el keep-alive contesta `presentarse: true` si el nodo no tiene bench, y el `/hello` le pone el alias a un alta pendiente (#60, BC-71).
- Pruebas: fragmentación del 1 % al 50 % en GKE con 3 GPU (#61, BC-40). Análisis de desempeño y escalabilidad que cruza todas las pruebas, con un gráfico comparativo (#62, BC-42).

## 0.3.0 — 18/11/2026

- Redis sin copias: una transacción confirmada queda en `tx:<id>` como puntero a su bloque, las tareas terminadas vencen a la hora y Redis tiene `maxmemory` (#58, BC-70). Límite de memoria de Redis a 1Gi (#56).
- Pruebas: prefijo de 1 a 8 rehecho con la v0.2.0 (#55, BC-39), entrada y salida de GPU en 3 ciclos (#57, BC-41) y `graficos.py` con los gráficos comparativos (#57, BC-42).
- Arreglo: `preparar.sh fijar-vms` con la clave de cada VM, el alias y HTTPS del contrato v2 (#55).

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
