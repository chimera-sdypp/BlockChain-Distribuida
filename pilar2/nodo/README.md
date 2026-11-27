# Nodo verificador (BC-83) y validador (BC-86)

Una copia propia de la cadena que verifica cada bloque entero con [`comun/estado.py`](../comun/estado.py) en modo estricto, contra **su** copia: hash y prefijo, que encadene con **su** punta, la raíz de transacciones y la de estado, cada firma, nonce y saldo, las actas, la coinbase y la participación contra la lista de shares publicada. Si cierra, lo aplica; si no, lo rechaza con el motivo y no sigue. No usa el Redis compartido: guarda su copia en un SQLite.

Lo corre cualquiera, sin permiso ni túnel: lee la API pública por HTTPS.

```bash
cd pilar2 && pip install -r comun/requirements.txt
python -m nodo --fuente https://blockchain-chimera.chimera-sdypp.workers.dev --datos ~/nodo-chimera          # :8090
```

| Ruta | Qué da |
|---|---|
| `/api/estado` | altura, punta, raíz de estado, atraso, y si rechazó un bloque o vio la historia reescrita |
| `/api/atestacion` | lo mismo, firmado con la clave del nodo (`atestacion:<nodo>:<altura>:<punta>:<raíz>:<tiempo>`) |
| `/api/saldos`, `/api/cuentas/<c>`, `/api/prueba/<c>` | el estado que calculó él (con la rama de Merkle) |
| `/api/bloques`, `/api/bloques/<hash>` | su copia |
| `/api/rechazos` | los bloques que rechazó, con los motivos |
| `/api/finalidad` | la altura finalizada según las firmas de validadores, verificadas acá |
| `/health`, `/metrics` | `blockchain_nodo_altura`, `_atraso`, `_punta`, `_rechazos_total`, `_conflictos_total`, `_finalizada` |

- **Piso de prefijo (v4, BC-97):** el del génesis; con `--prefijo-minimo N` (`PREFIJO_MINIMO`) exige uno mayor. Una historia reminada desde el génesis con prefijo bajo no la acepta, aunque el nodo arranque de cero.
- **Historia reescrita:** si la fuente muestra otro bloque a una altura que el nodo ya verificó (o pierde bloques), es un conflicto: lo anota, alerta y no sigue esa cadena.
- **Al arrancar** sigue desde su copia (con un snapshot cada 100 bloques); la primera vez baja todo desde el génesis.
- **En el cluster:** StatefulSet de 2 con su disco ([`k8s/nodo/`](../k8s/nodo/)), publicados en `/nodos/0` y `/nodos/1`. Las alertas `NodoRechazoUnBloque` y `NodosNoCoinciden` están en [`k8s/monitoreo/alertas.yaml`](../k8s/monitoreo/alertas.yaml). Los que más valen son los que corre otra gente fuera de la nube.

## Validador (BC-86)

```bash
python -m nodo --fuente https://… --datos ~/validador --validador     # muestra su cuenta al arrancar
python -m claves aprobar organizador.clave --cuenta <esa cuenta> --tipo validador --nombre v-tomas --url https://…
```

Con un alta de tipo `validador` en la cadena, firma `firma:<altura>:<block_hash>` del último bloque que verificó y la manda a `POST /api/firmas` (sale: no necesita IP pública). Firma siempre hacia adelante y una sola vez por altura: lo anota en su disco antes de mandarla. Un bloque firmado por 2/3 de los validadores (2 de 3) queda **finalizado**: lo finalizado no se puede deshacer sin que 2 validadores firmen dos historias distintas, y esa doble firma queda como prueba (`/api/finalidad`, `dobles`). Los validadores corren en las PCs de los integrantes, no en GKE: en la nube del operador, el operador los controlaría. Sin validadores el sistema anda igual; sólo no se finaliza nada.

Tests: `python -m pytest nodo`.
