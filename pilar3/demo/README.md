# Demo (BC-66)

Un subcomando por paso de la demo, contra el sistema desplegado: nada se tipea en vivo. El guion completo (qué mostrar en Grafana y Loki y qué decir) está en `Distribuidos/diagramas-tpi/guion-demo.html`, fuera del repo.

```bash
pilar2/.venv/bin/python pilar3/demo/demo.py estado              # altura, nodos, finalidad, mineros, pods y VMs
pilar2/.venv/bin/python pilar3/demo/demo.py transferencia       # alice → bob, con su recorrido por los servicios y la consulta de Loki
pilar2/.venv/bin/python pilar3/demo/demo.py rechazos            # firma falsa, reenvío, sin saldo, sin alta, minero sin aprobar
pilar2/.venv/bin/python pilar3/demo/demo.py rafaga 200          # pilar3/pruebas/rafaga.sh
pilar2/.venv/bin/python pilar3/demo/demo.py gpu sacar|volver    # el minero GPU del k3s; muestra el escalador y las VMs
pilar2/.venv/bin/python pilar3/demo/demo.py matar nct|trp|gateway|redis   # borra un pod (en redis, el primario) y confirma una transferencia
pilar2/.venv/bin/python pilar3/demo/demo.py operador            # HINCRBY a un saldo en Redis: se rechaza y el NCT lo corrige
```

La ráfaga también tiene ventana, para manejarla en vivo: el ritmo (transferencias por segundo) se mueve con una barra y hay botones para tirar 10, 50, 200 o 500 juntas; muestra las mandadas, las que esperan bloque, las confirmadas, lo que hay en las cajas del NCT y un gráfico de los últimos 60 s.

```bash
pilar2/.venv/bin/python pilar3/demo/rafaga.py                   # abre la ventana (Chrome en modo app); Ctrl+C la cierra
```

Por defecto va contra GKE (`SISTEMA`, `GATEWAY`, `KUBE_GKE`, `KUBE_K3S` y `PROYECTO` cambian el destino) con las cuentas de prueba del génesis (alice, bob y carol). Probado en GKE con la v0.8.0 el 19/11.
