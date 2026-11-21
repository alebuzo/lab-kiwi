# analysis/

Análisis de resultados de las pruebas de Kiwi TCMS (plan-infraestructura.md sección 8; tablas E5–E20 de plan-ejecucion.md).

```
python3 -m venv .venv && .venv/bin/pip install -r analysis/requirements.txt   # probado con Python 3.14
python analysis/parse_k6.py results/load/rep1 results/load/rep2 results/load/rep3 [--baseline-u3-ms 210]
python analysis/parse_mariadb.py results/load/rep1 [--csv deltas.csv]
python analysis/report.py --test load --results results --baseline-test load   # también: stress, endurance, all
python analysis/risk_matrix.py [--input analysis/risk_input.csv] [--output matriz.md]
```

`report.py` escribe `results/<prueba>/report.md` (y `results/report_all.md` con `all`) y las figuras en `results/<prueba>/figures/`
(`rep<N>_latencia.png`, `_throughput.png`, `_recursos.png`, con sombreado de warm-up y cooldown).

## Datos de entrada (`results/<prueba>/rep<N>/`)

| Archivo | Uso |
| --- | --- |
| `k6.csv` | CSV estándar de k6; las etiquetas propias se leen de `extra_tags` (urlencoded). Solo se cargan `http_req_duration`, `dur_*`, `rpc_ok`, `a1_*`. |
| `times.txt` | Opcional. Se buscan líneas con `warmup`/`measure`/`cooldown` y una o dos marcas de tiempo (ISO UTC o epoch); `…start`/`…end` o dos marcas en la misma línea. Sin él, las fases salen de la etiqueta `phase`. |
| `mariadb_status_<ts>.txt` | `SHOW GLOBAL STATUS` crudo (tab). El tiempo se toma del nombre (`YYYYMMDDTHHMMSSZ` o similar) o, si falta, del mtime. |
| `ephemeral.csv` | `timestamp_utc,pod,tmp_kb,rootfs_du_kb` (`du -sxk /`, sin volúmenes montados). |
| `prom_*.csv` | La métrica se detecta por subcadena del nombre (`node_mem_pct`, `node_cpu_pct`, `log_fs`, `throttl`, `restart`, `cpu`, `mem`). Columnas: la de tiempo (`timestamp`/`time`/primera; epoch s/ms o ISO), la última o `value` como valor, y `pod`/`node`/`instance` (o `metric`/`labels` con `pod="..."`); si faltan, se infieren del nombre del archivo. Valores de throttling ≤ 1 se interpretan como fracción (× 100). |
| `*event*.json` | Opcional (`kubectl get events -o json`) para la tabla E7; además E7 usa los incrementos de reinicios de Prometheus. |

## Reglas y supuestos

- Todas las tablas de métricas usan solo `phase=measure`. Tiempos en ms (A1: duración de corrida en s). Pods de la réplica: `kiwi-perf-0` y `kiwi-perf-mariadb-0`; producción: el resto (E5b).
- Tiempos de respuesta: se usan las `Trend` `dur_<op>` y `dur_A1_call`; si faltan, `http_req_duration` con la etiqueta `op`. Las operaciones `A1_<método>` se agrupan en A1.
- Percentiles: p95 con N ≥ 20 y p99 con N ≥ 100 por operación y repetición (se muestra `N=`); si no, `n/d` y se calcula en el combinado cuando lo cumpla.
- Errores por contenido: `rpc_ok == 0`; tipo más frecuente: etiqueta `error_type`. Throughput: `http_req_duration` de la medición / duración de la medición (de `times.txt` o inferida: primer U* de la medición hasta el inicio del cooldown).
- A1: iniciadas = `run_id` distintos; con error = `a1_run_duration` con `status=error`, `calls_failed>0` o algún `rpc_ok=0` del `run_id` (se indica tipo y llamada); completas = terminadas sin error; solapadas = corridas cuyo intervalo (fin − duración) se traslapa con otra.
- **Tiempo de recuperación**: segundos desde el inicio del cooldown hasta que la mediana móvil de 60 s (mín. 3 sondeos; sondeo con `rpc_ok=0` = ∞) de U3 del escenario `recovery_probe` es ≤ la mediana de U3 de la línea base (carga, combinado; `--baseline-u3-ms` la sustituye). Se anota si había una corrida A1 activa en el cooldown.
- MariaDB: deltas por intervalo; un delta negativo (reinicio de contadores) se marca y se excluye de los totales. Ratio buffer pool = reads/requests (se añade hit ratio).
- Recursos: reposo = promedio de los primeros 60 s de la serie exportada; prom./máx. dentro de la medición; almacenamiento efímero = máx. de `rootfs_du_kb` en toda la repetición; tendencia = último vs primer cuarto (±10 %).
- Resistencia: mediana por hora completa; primera hora vs últimos 60 min (umbral 25 %); memoria tras el cooldown = promedio de los últimos 60 s de la serie vs reposo (umbral +25 %).
- Estrés: aumento relativo de medianas (umbral 50 %); tasa de error en puntos porcentuales absolutos (P18), comparada literalmente contra 50 pp.
- Matriz de riesgo: `risk_input.csv` (`hallazgo,area,probabilidad,impacto,justificacion`; área `carga|estres|resistencia`, 1–5). Las filas cuyo hallazgo empieza con `EXAMPLE` se ignoran (`--include-examples` para probar). Puntuación = P × I; la del área = máximo de sus hallazgos (P19).

## Dashboards (`dashboards/`)

`kiwi-perf-pods.json` ("Kiwi Perf – Pods réplica") y `kiwi-perf-nodo-produccion.json` ("Kiwi Perf – Nodo y producción").
Importar en Grafana: Dashboards → New → Import → subir el JSON → elegir el datasource Prometheus (`DS_PROMETHEUS`) → carpeta **Kiwi Perf** → Import.
Variables: `datasource`, `namespace` (kiwi), `pod` (regex; `kiwi-perf.*` en A, `kiwi.*` en B, que además excluye `*perf*`), `node` (nombre de nodo vía `node_uname_info`). Rango 6 h, refresco 30 s; el panel de memoria del nodo marca el umbral de aborto de 30 %.
