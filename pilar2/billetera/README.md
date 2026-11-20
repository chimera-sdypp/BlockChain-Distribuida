# Billetera descargable (BC-78) que verifica (BC-84)

[`billetera.html`](billetera.html) es un solo archivo, sin nada externo, que se baja una vez del **release de GitHub** (con su SHA-256 en las notas: `sha256sum billetera.html`) y se abre desde el disco. No la sirve la nube, así que quien opera la nube no puede cambiar su código para llevarse la clave.

- **La clave:** se genera con WebCrypto (Ed25519) y se baja un `.jwk` (el formato de `python -m claves exportar`; la herramienta lo lee). O se carga uno.
- **Pedir el alta y transferir:** firma acá y manda sólo lo firmado (`POST /api/altas`, `POST /api/transacciones`).
- **Saldo verificado por tu navegador:** baja `/api/bloques?orden=asc` y verifica cada bloque con [`verificador.js`](verificador.js), el port exacto de `comun/estado.py`: hash y texto de 77 caracteres, raíz de transacciones y de estado (Merkle), firmas Ed25519, nonces y saldos, actas, coinbase y el pago de cada época contra `/api/epocas/<n>`. Guarda en IndexedDB lo verificado y la próxima vez baja sólo lo nuevo. Si un bloque no verifica, lo dice y no sigue.
- **Confirmada o finalizada:** cada movimiento propio está confirmado (en un bloque) o finalizado (firmado por 2/3 de los validadores). La finalidad la calcula ella: verifica cada firma contra los validadores que salen de **sus** altas en la cadena.
- **Comparación:** su saldo con la rama de Merkle de `/api/prueba/<cuenta>` y con el saldo del backend, y su punta y raíz con las atestaciones firmadas de 2 o más nodos (por defecto `/nodos/0` y `/nodos/1`; se pueden agregar otros, con la clave del nodo para fijarla). Lo que no coincide sale en rojo.

**Modo organizador** (BC-87, BC-88): abajo de todo, «Modo organizador». Cargás el `.jwk` del organizador y, si el backend acepta su firma (los pedidos pendientes y los suspendidos sólo se leen firmados), aparece la sección; desde ahí aprobás (con carga), rechazás, das de baja, cargás saldo, das permisos y reactivás mineros suspendidos, con botones. Las actas y las cargas se firman en este navegador, igual que `python -m claves` (un test con node comprueba que salen idénticas a las de `comun/contrato.py`); rechazar y reactivar van firmados con `X-Organizador`. Es el patrón de MetaMask: la interfaz y la firma en un código fijo y verificado, no en una página que sirve la nube.

Reglas fijas: la clave del organizador (vacía: la del génesis) y la recompensa (10 × largo del prefijo).

```bash
cd pilar2
python billetera/armar.py                                         # pagina.html + verificador.js → billetera.html
node billetera/verificar_en_vivo.cjs https://blockchain-chimera.duckdns.org <cuenta>   # lo mismo, desde la terminal
python -m pytest billetera                                        # el JS contra Python, sobre el mismo vector (con node)
```

Lo mismo sin navegador: `python -m claves saldo ARCHIVO --url U --verificar --nodo <url>…`.
