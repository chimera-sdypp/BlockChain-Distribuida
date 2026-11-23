# Versiones

Versión actual: **0.8.5** (23/11/2026)

[SemVer](https://semver.org/lang/es/) 0.x hasta la entrega: cada pase de `dev` a `main` sube la versión acá y deja un tag `vX.Y.Z` en `main`.

## 0.8.5 — 23/11/2026

Sólo herramientas y documentación: no cambia ningún servicio ni la cadena.

- Arreglo (#94): la herramienta de claves, `rafaga.py` y `demo.py` mandan su propio User-Agent. Cloudflare rechazaba el de Python (error 1010), así que fallaban con la URL del Worker, que es la de por defecto desde la 0.8.4.
- **Video de la demo** (#95, #96): sección en el README; los videos son adjuntos del release v0.8.4, porque GitHub no acepta archivos de más de 100 MB en el repo.

## 0.8.4 — 22/11/2026

- **Puerta en Cloudflare** (#89): un Worker en `blockchain-chimera.chimera-sdypp.workers.dev` reenvía cada pedido al sistema en GKE, para entrar desde la red de la universidad, que filtra DuckDNS. Lo despliega el workflow «Pilar 3 · Cloudflare». La billetera, el nodo y los scripts de la demo usan esa URL por defecto; DuckDNS sigue andando y los mineros siguen entrando por la IP del gateway. Cambia la billetera: nuevo SHA-256 en el release.
- **Informe final** (#92) en [`informe/informe-tpi-cerberus.pdf`](informe/informe-tpi-cerberus.pdf).

## 0.8.3 — 21/11/2026

- **Bloques que se llevan toda la cola** (#90, BC-89): el NCT cierra una caja sólo cuando no hay otra en la fila, y al confirmarse un bloque el siguiente sale en el acto con todo lo que llegó mientras se minaba, hasta `TX_POR_CAJA`, que pasa de 100 a 1000. Hasta la 0.8.2 cada tic apilaba cajas de 100 y se minaba una por bloque: la capacidad era 100 tx por minuto y, con la ráfaga de 10 tx/s de la demo del 20/11, la cola crecía sin techo (sumar mineros no la sube: el prefijo sube y el bloque vuelve a tardar un minuto). No cuesta más PoW: lo que se mina lleva la raíz de las transacciones. Probado en k3s con la ráfaga de 10 tx/s durante 5 min y 3 mineros CPU (prefijo 7): 3010 de 3010 confirmadas, la cola se vació 76 s después de cortar.
- Grafana: métrica `blockchain_tx_por_bloque_maximo` y línea «capacidad» (`TX_POR_CAJA` / `SEGUNDOS_POR_BLOQUE`) en el panel de transacciones recibidas.

## 0.8.2 — 20/11/2026

- Arreglo (#87): los servicios vuelven a intentar la conexión a RabbitMQ cuando falla el DNS. Cuando GKE recreaba el nodo Spot de las apps, el backend y el frontend arrancaban antes de que el DNS del cluster resolviera `rabbitmq`, el hilo que conecta moría y quedaban en `0/1` hasta reiniciarlos a mano.

## 0.8.1 — 20/11/2026

Sólo herramientas de la demo y documentación: no cambia ningún servicio ni la cadena.

- **Demo** (#85, BC-66): `pilar3/demo/demo.py`, un subcomando por paso contra el sistema desplegado, y `pilar3/demo/rafaga.py`, la ráfaga con una ventana para cambiar el ritmo en vivo (transferencias por segundo) o tirar varias juntas, con lo que esperan bloque, lo confirmado y un gráfico de los últimos 60 s.
- README de la raíz con los pilares, el despliegue y la billetera.

## 0.8.0 — 19/11/2026

El organizador opera desde la billetera descargable y sus listas dejan de ser públicas. No cambia el contrato de la cadena: no hace falta reiniciarla. Sí cambia la API: se borran rutas.

- **Modo organizador en la billetera** (#82, BC-87): con el `.jwk` del organizador, botones para aprobar (con carga), rechazar, dar de baja, cargar saldo, dar permisos y reactivar mineros. Las actas y las cargas se firman en el navegador, desde el archivo verificado por su SHA-256; un test con node comprueba que salen idénticas a las de `comun/contrato.py`.
- **Sin panel del organizador en la nube** (#83, BC-88): se borra `/organizador`. Los pedidos de alta y los mineros suspendidos ya no son públicos (se borran `GET /api/altas/pendientes` y `GET /api/suspendidos`): se leen en `/api/organizador/altas` y `/api/organizador/suspendidos`, firmados con `X-Organizador`. En la billetera, la sección del organizador aparece sólo cuando el backend acepta la firma de la clave cargada. `python -m claves pendientes` pide la clave del organizador. Las cuentas y los saldos siguen públicos.
- El backend acepta CORS en las cuatro rutas firmadas que usa la billetera (las dos lecturas, rechazar y reactivar).
- Arreglo (#81): el escalador vuelve a pedir el alta delegada de las VMs que creó antes de tener el permiso del organizador. Antes la pedía una sola vez, dentro del `tofu apply`, y la VM quedaba prendida, cobrando y sin alta.
- Diagramas generales de los pilares 2 y 3 con la v0.7.0 en los README (#80): nodos verificadores, validadores y la verificación fuera de GCP.

## 0.7.1 — 19/11/2026

- `preparar.sh reiniciar` con la v3 (#78): apaga los nodos y borra su copia de la cadena (con la vieja, la nueva les aparecía como una historia reescrita), y genera la clave del escalador en Redis aunque KEDA lo tenga en 0, porque el FLUSHALL la borraba con su permiso. Imprime la clave para darle el permiso.

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
