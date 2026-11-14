# Pruebas de carga y escalabilidad (BC-38 a BC-42)

| Carpeta / archivo | Qué es |
|---|---|
| [`bulks/`](bulks/README.md) | BC-38: bulks de 1 a 100.000 transacciones con k6 |
| [`prefijos.py`](prefijos.py) y `grafico_prefijos.py` | BC-39: prefijo de 1 a 8 caracteres |
| `preparar.sh` | Deja GKE en las condiciones de una prueba y después lo vuelve a la normalidad |

## Preparar GKE: workflow «Pilar 3 · Preparar prueba»

Se corre a mano: **Actions → Pilar 3 · Preparar prueba → Run workflow**. Entra al cluster con la misma cuenta de los pipelines (WIF), corre [`preparar.sh`](preparar.sh) y deja en el resumen la IP pública, la altura de la cadena, los saldos, los mineros vivos y las condiciones del TrP.

| Acción | Qué hace |
|---|---|
| `estado` | Sólo muestra; no cambia nada |
| `preparar` | Fija `PREFIJO_BASE`, `AJUSTAR_PREFIJO` y `FRAGMENTO_PORCENTAJE` en el TrP (y el mismo prefijo en el escalador). Opcionales: reiniciar la cadena y dejar una cantidad fija de VMs |
| `restaurar` | TrP y escalador como en los manifiestos, y el escalador de nuevo en manos de KEDA |

**Reiniciar la cadena** (escribir `REINICIAR` en el campo): apaga el NCT y el TrP, vacía las colas y Redis, y los vuelve a prender. El NCT carga el génesis con los `SALDOS_INICIALES` de su Deployment, que en GKE son 1.000.000.000 por usuario ([`nct-saldos.yaml`](../k8s/apps/nct-saldos.yaml)). Se pierden todos los bloques y saldos anteriores, para todo el grupo. Se niega si el NCT todavía tiene los saldos por defecto.

**VMs fijas** (`vms_cpu`): pausa el escalador en KEDA (`autoscaling.keda.sh/paused-replicas: "0"`), aplica `vms/instancias` con esa cantidad y espera a que los mineros CPU avisen. Sin esto, el escalador apaga las VMs a los 2 minutos sin trabajo y las vuelve a crear en la corrida siguiente: cambia la cantidad de mineros entre corridas. Si hay una GPU viva, con `vms_cpu` vacío el escalador no levanta ninguna.

Mientras se mide **nadie mergea a `main`**: el Pipeline 3 volvería a aplicar los manifiestos y cambiaría el prefijo en medio de la prueba. Al terminar, `restaurar`.

Desde una PC con `kubectl` apuntando al cluster es lo mismo:

```bash
pilar3/pruebas/preparar.sh estado
pilar3/pruebas/preparar.sh reiniciar
pilar3/pruebas/preparar.sh condiciones 5 0 10        # PREFIJO_BASE, AJUSTAR_PREFIJO, FRAGMENTO_PORCENTAJE
PROYECTO=<proyecto de GCP> pilar3/pruebas/preparar.sh fijar-vms 1
pilar3/pruebas/preparar.sh restaurar
```

## Prefijo de 1 a 8 caracteres (BC-39)

[`prefijos.py`](prefijos.py) fija el prefijo del TrP (`PREFIJO_BASE=n`, `AJUSTAR_PREFIJO=0`), manda una transacción por bloque y toma de los logs del NCT el tiempo desde que publica la tarea hasta que confirma el bloque (si la tarea vuelve sin solución, desde la primera publicación). Al terminar deja el TrP como estaba.

```bash
python pilar3/pruebas/prefijos.py --api http://<traefik> --salida resultados/prefijos-gpu.csv
python pilar3/pruebas/prefijos.py --api http://<traefik> --salida resultados/prefijos-cpu.csv --tipo cpu --maximo 600
python pilar3/pruebas/grafico_prefijos.py
```

13/11, 5 bloques por largo: GPU con la RTX 4060 del k3s; CPU con 3 VMs e2-highcpu-2 (el escalador con `PREFIJO_BASE=8` durante la corrida, para que pida el máximo).

![Tiempo por bloque según el prefijo](resultados/prefijos.png)

| Prefijo | 1 GPU | 3 VMs CPU |
|---|---|---|
| 1 a 5 | 1,3 a 1,7 s | 0,8 a 1,5 s |
| 6 | 1,8 s | 7,3 s |
| 7 | 2,3 s | 72 s |
| 8 | 9,4 s | sin bloque en 600 s |

- Hasta 5 caracteres no se mide el minado sino el resto del sistema (RabbitMQ, TrP, sondeo del minero, confirmación en Redis).
- Con CPU se ve el ×16 por carácter desde el 6; con 8 harían falta ~17 min y el NCT vence la tarea a los 10 (`TAREA_VENCE`).
- Con 8 caracteres hacen falta 16⁸ = 2³² intentos, el mismo tamaño que `ESPACIO_NONCES`: 3 de 8 tareas no tuvieron solución (e⁻¹ ≈ 37 %) y el NCT las rearmó con otro timestamp.
