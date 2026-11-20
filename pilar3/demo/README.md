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

Por defecto va contra GKE (`SISTEMA`, `GATEWAY`, `KUBE_GKE`, `KUBE_K3S` y `PROYECTO` cambian el destino) con las cuentas de prueba del génesis (alice, bob y carol). Probado en GKE con la v0.8.0 el 19/11.
