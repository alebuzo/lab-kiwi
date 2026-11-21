# Plan de ejecución — Pruebas de rendimiento de Kiwi TCMS

Guía para quien ejecute las pruebas (persona o agente). Requiere que **todo** `plan-infraestructura.md` esté implementado y el ensayo previo (sección 10 de ese plan) esté aprobado. Referencias: `objetivos-metodologia.md`, `plan-infraestructura.md`.

> Estado: **réplica desplegada; smoke y ensayo de reseteo hechos (2026-11-20)**. Los nombres de scripts y dashboards son los definidos en el plan de infraestructura. Los valores pendientes de confirmar están listados en la sección 12 del plan de infraestructura. No se inventan datos: toda celda vacía se llena con lo observado.

---

## 0. Reglas generales

1. Nunca tocar recursos sin sufijo `-perf` (producción: `kiwi-0` y `kiwi-mariadb-0`).
2. Registrar hora local (UTC-6) de cada paso en las tablas.
3. Si se cumple un criterio de aborto (sección 3), detener, anotar y avisar al usuario antes de continuar.
4. Orden fijo: **Carga (martes) → Resistencia (miércoles) → Estrés (jueves)**, misma hora de inicio, 6:30 am UTC-6 (confirmado). Las jornadas pueden extenderse pasadas las 8:30 am; vigilar el criterio A-3 cuando entren los ingenieros.
5. Entre repeticiones: 5 min de pausa y restauración de la BD (ver 2.4).
6. Cada prueba = warm-up 5 min + medición + cooldown 5 min (fuera de la duración de la medición; ver 6.4 del plan de infraestructura: warm-up con rampa lineal y sin A1; cooldown con 1 VU de sondeo).
7. Los datos crudos van a `results/<prueba>/rep<N>/` y no se editan a mano.

---

## 1. Registro general de la campaña

Tabla E1. Datos de contexto (llenar una vez)

| Campo | Valor |
| --- | --- |
| Responsable de la ejecución | |
| Versión de k6 / imagen (plan: `grafana/k6:2.4.0`) | |
| Imagen de Kiwi réplica | `pvsdk-registry.rose.rdlabs.hpecorp.net/sdk_qa/kiwi:v13.5.5-sdk` (verificar tag/digest real) |
| Imagen de MariaDB réplica | |
| Nodo de kiwi-perf / kiwi-perf-mariadb / k6-runner-perf (plan: asic-x86-02 / asic-x86-02 / asic-x86-01) | |
| Nodo del pod de producción (app y BD) | |
| Memoria total y allocatable de cada nodo (V3: memoria asignable 1,079,645,020,160 B en -01 y 1,079,645,048,832 B en -02; 128 cores en cada nodo; confirmar el día de la prueba) | |
| Digest/versión de la imagen de producción al inicio (kiwi y kiwi-mariadb) | |
| Fecha y hora de restauración de la BD | |
| Dump golden usado (nombre, fecha, tamaño, sha256, origen: CronJob o dump nuevo) | |
| Casos de prueba / tablas tras restore (esperado 11,492 / 71, o el conteo del backup usado) | |

---

## 2. Procedimiento previo a cada día de pruebas (checklist T-30 min)

### 2.1 Verificación de producción y del nodo
- [ ] `kubectl get pods -n kiwi -o wide` → todos los pods de producción `Running` y sin reinicios nuevos. Anotar reinicios.
- [ ] Ejecutar `scripts/precheck.sh` (capacidad asignable del nodo, requests vigentes).
- [ ] En Grafana abrir **"Kiwi Perf – Nodo y producción"** y anotar el uso de memoria y CPU del nodo y los reinicios de producción en la Tabla E2.
- [ ] Si la memoria del nodo ya supera el umbral de aborto (**30 %**) o hay degradación en producción, **no iniciar**; buscar otra ventana.

Tabla E2. Estado previo al inicio (una fila por día/prueba)

| Fecha | Prueba | Hora | Mem. nodo (%) | CPU nodo (%) | Pods prod. Running (sí/no) | Reinicios prod. previos | ¿Se autoriza iniciar? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | |

### 2.2 Estado de la réplica
- [ ] `kubectl get pods -n kiwi -l app.kubernetes.io/part-of=kiwi-perf` → `kiwi-perf-0`, `kiwi-perf-mariadb-0`, `k6-runner-perf` en `Running`, 0 reinicios.
- [ ] Restore reciente verificado: tablas y casos (Tabla E1).
- [ ] Usuarios de prueba existen (`scripts/seed-users.sh` si hace falta).
- [ ] Login manual de un usuario de prueba vía el script `smoke.js` (1 iteración) correcto.

### 2.3 Monitoreo preparado
- [ ] Dashboard **"Kiwi Perf – Pods réplica"** abierto, rango "Last 6 hours", refresco 10 s.
- [ ] Dashboard **"Kiwi Perf – Nodo y producción"** abierto en otra pestaña, refresco 10 s.
- [ ] Terminal A: `scripts/collect-mariadb-status.sh <prueba> <rep>` (cada 45 s).
- [ ] Terminal D: `scripts/collect-ephemeral.sh <prueba> <rep>` (cada 45 s; almacenamiento efímero de ambos pods).
- [ ] Terminal B: `scripts/abort-watch.sh` o `kubectl get pods -n kiwi -w` para ver reinicios/desalojos.
- [ ] Terminal C: reservada para `scripts/run-test.sh`.

### 2.4 Restauración entre ejecuciones
Antes de **cada** repetición (también la primera): restaurar la BD (mecanismo definido en la sección 5.5 del plan de infraestructura: clonado de PVC golden o restore del dump), verificar conteos. El método esperado es el clonado (≈33 s en el ensayo); el golden `mariadb-golden-perf` ya contiene los usuarios de prueba, no hace falta `seed-users.sh`. **Se ejecuta con `scripts/reset-db.sh`** (≈66–79 s en el ensayo, incluyendo apagar los pods; imprime la fila para E3 y verifica 71 tablas, 11,492 casos y 18 `perf_user_*`; no cuenta filas de `testruns_historicaltestexecution` porque con caché fría tarda >1 min y calentaría la caché de MariaDB). Pasos que realiza (solo recursos `-perf`):

1. `kubectl scale sts/kiwi-perf sts/kiwi-perf-mariadb -n kiwi --replicas=0` y esperar a que los pods desaparezcan.
2. `kubectl delete pvc mariadb-data-kiwi-perf-mariadb-0 -n kiwi`.
3. Crear de nuevo `mariadb-data-kiwi-perf-mariadb-0` (32Gi, `hpe-standard`, mismas etiquetas `kiwi-perf`) con `dataSource` = PVC `mariadb-golden-perf`.
4. `kubectl scale sts/kiwi-perf-mariadb -n kiwi --replicas=1`, esperar Ready; luego `kubectl scale sts/kiwi-perf -n kiwi --replicas=1`, esperar Ready.
5. Verificar 71 tablas, 11,492 casos y 18,541,264 filas en `testruns_historicaltestexecution`; anotar la duración en E3.

Tabla E3. Restauraciones

| Prueba | Rep. | Método (clonado PVC / restore dump) | Hora inicio reset | Hora fin | Duración (s) | Casos / tablas verificados | Observaciones |
| --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | |

---

## 3. Criterios de aborto (aplicar durante toda prueba)

Detener `run-test.sh` (Ctrl-C o `kubectl delete pod k6-runner-perf`) si ocurre **cualquiera**:

| # | Criterio | Dónde mirar |
| --- | --- | --- |
| A-1 | Memoria del nodo > **30 %** (memoria usada total: `MemTotal - MemAvailable`; valor definido por el equipo) | Dashboard B, panel "Uso de memoria del nodo (%)" |
| A-2 | Algún pod **productivo** se reinicia o es desalojado | Dashboard B, paneles de reinicios/estado; terminal B |
| A-3 | El equipo reporta degradación en producción | Canal de comunicación del equipo |

Si se aborta: anotar en la Tabla E4, preservar los datos parciales (`results/…`), no reiniciar la prueba hasta hablar con el usuario. Un aborto no se cuenta como repetición válida.

Tabla E4. Abortos / incidentes

| Fecha | Prueba y rep. | Hora | Criterio (A-1/A-2/A-3/otro) | Valor observado | Acción tomada | Quién autoriza reintento |
| --- | --- | --- | --- | --- | --- | --- |
| | | | | | | |

> Nota: los pods de la **réplica** que se reinicien (OOMKilled, etc.) **no** abortan la prueba; son un hallazgo. Se registran en la Tabla E7.

---

## 4. Procedimiento por repetición (aplica a carga, estrés y resistencia)

Sustituir `<TEST>` ∈ {`load`, `endurance`, `stress`} y `<N>` por el número de repetición.

| Paso | Acción | Registro |
| --- | --- | --- |
| 1 | Completar Sección 2 (checklist) y la Tabla E2 | E2, E3 |
| 2 | Anotar hora de inicio y capturar en Grafana el estado de reposo (CPU, memoria, almacenamiento efímero de `kiwi-perf-0` y `kiwi-perf-mariadb-0`) | Tabla E5 (columna "Reposo") |
| 3 | Terminal A: iniciar `scripts/collect-mariadb-status.sh <TEST> <N>` | — |
| 4 | Terminal C: `scripts/run-test.sh <TEST> <N>` (hace warm-up 5 min → medición → cooldown 5 min y escribe `results/<TEST>/rep<N>/`) | Hora inicio y fin en Tabla E5 |
| 5 | Durante la medición, cada **5 min** (cada 10 min en resistencia, ver 6) mirar Dashboard B: memoria de nodo y reinicios de producción; anotar en Tabla E6 | E6 |
| 6 | Al terminar la medición, en Dashboard A fijar el rango exacto de la prueba y anotar, para `kiwi-perf-0` y `kiwi-perf-mariadb-0`: CPU promedio/máximo, memoria promedio/máximo, almacenamiento efímero máximo (de `ephemeral.csv`), throttling máximo | Tabla E5 |
| 7 | Tomar captura (PNG) de ambos dashboards con el rango de la prueba y guardarla en `results/<TEST>/rep<N>/figures/` | — |
| 8 | Detener `collect-mariadb-status.sh` y `collect-ephemeral.sh` al finalizar el cooldown; ejecutar `scripts/collect-prometheus.sh <TEST> <N> <inicio> <fin>` | — |
| 9 | Ejecutar `scripts/collect-k8s-events.sh <TEST> <N>`; anotar reinicios, desalojos y motivos | Tabla E7 |
| 10 | Ejecutar `analysis/parse_k6.py results/<TEST>/rep<N>` y copiar a las tablas E8–E10 las cifras producidas | E8–E10 |
| 11 | Pausa de 5 min y restauración (2.4). Pasar a la siguiente repetición | E3 |

Tabla E5. Recursos por pod y repetición (fuente: `prom_*.csv` vía `report.py`; Dashboard A solo para comprobar visualmente)

| Prueba | Rep. | Pod | Hora inicio | Hora fin | CPU reposo | CPU prom. | CPU máx. | Mem. reposo | Mem. prom. | Mem. máx. | Ef. storage máx. | Throttling máx. (%) | Tendencia (sube/estable/baja) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | kiwi-perf-0 | | | | | | | | | | | |
| | | kiwi-perf-mariadb-0 | | | | | | | | | | | |

Tabla E6. Monitoreo del nodo y producción durante la prueba

| Prueba | Rep. | Hora | Mem. nodo (%) | CPU nodo (%) | Reinicios prod. | Pods prod. OK | Comentarios |
| --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | |

Tabla E7. Reinicios, desalojos, OOM y eventos (réplica y producción)

| Prueba | Rep. | Pod | Tipo (reinicio/desalojo/OOM/otro) | Hora | Motivo (de `describe`/eventos) | Cantidad |
| --- | --- | --- | --- | --- | --- | --- |
| | | | | | | |

---

## 5. Tablas de resultados de k6 (se llenan con la salida de `parse_k6.py`)

Tabla E8. Tiempos de respuesta por operación (ms o s, indicar unidad)

| Prueba | Rep. | Operación | N muestras | Mediana | Promedio | Máximo | p95 | p99 | ¿Mediana < 8 s? | Errores | Tasa de error (%) | Tipo de error más frecuente |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | U1 | | | | | | | | | | |
| | | U2 | | | | | | | | | | |
| | | U3 | | | | | | | | | | |
| | | U4 | | | | | | | | | | |
| | | A1 (por llamada) | | | | | | | n/a | | | |

> p95 solo con ≥ 20 muestras y p99 con ≥ 100 (por operación y repetición). Si no se alcanzan, escribir "n/d" en la repetición y calcularlos en el combinado; si ni así, reportar solo la mediana.

Tabla E9. Corridas de A1, throughput y errores

| Prueba | Rep. | Corridas A1 iniciadas | Corridas A1 completas | Corridas A1 con error (tipo y llamada que falló) | Duración mediana | Duración mín. | Duración máx. | Corridas solapadas | Throughput (req/s) | Solicitudes totales | Errores por contenido | Tasa de error (%) | Tipos de error observados |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | | | | | | | |

Tabla E10. Análisis de base de datos (fuente: `SHOW GLOBAL STATUS`, `parse_mariadb.py`)

| Prueba | Rep. | Δ Queries | Queries/s prom. | Δ Created_tmp_tables | Δ Created_tmp_disk_tables | Δ Created_tmp_files | Threads_running máx. | Max_used_connections | Ratio buffer pool (reads/requests) | Slow_queries Δ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | | | | |

---

## 6. Prueba de carga (Objetivo 1) — martes

Configuración: 6 usuarios virtuales (U1–U4), A1 una vez cada 10 min, 60 min de medición, 3 repeticiones.

1. Completar Sección 2.
2. Repetición 1: `scripts/run-test.sh load 1` → pasos 1–11 de la Sección 4.
3. Pausa de 5 min + restore → repetición 2 → pausa + restore → repetición 3.
4. Tras la repetición 3: calcular con `analysis/report.py load` la tabla combinada (E11).
5. En la medición, A1 debe dispararse a los minutos 0, 10, 20, 30, 40, 50 (6 corridas). Confirmar en E9.

Tabla E11. Línea base (carga) — repeticiones por separado y combinadas

| Métrica | Rep. 1 | Rep. 2 | Rep. 3 | Combinado |
| --- | --- | --- | --- | --- |
| Mediana U1 | | | | |
| Mediana U2 | | | | |
| Mediana U3 | | | | |
| Mediana U4 | | | | |
| Promedio global usuarios | | | | |
| p95 / p99 por operación | | | | |
| Duración mediana A1 | | | | |
| Throughput (req/s) | | | | |
| Tasa de error (%) | | | | |
| CPU kiwi prom./máx. | | | | |
| Mem. kiwi prom./máx. | | | | |
| CPU MariaDB prom./máx. | | | | |
| Mem. MariaDB prom./máx. | | | | |
| Almacenamiento efímero MariaDB máx. | | | | |
| Reinicios / desalojos / throttling | | | | |

Duración estimada de la jornada: 3 × 70 min + 2 × 5 min de pausa ≈ 3 h 40 min más restores.

---

## 7. Prueba de resistencia (Objetivo 3) — miércoles

Configuración: igual a la carga, **4 horas**, 1 repetición. Warm-up y cooldown de 5 min.

Medidas adicionales (la prueba es larga y el riesgo es de tendencia/memoria):
- Mirar el Dashboard B cada **10 min** y anotar en E6. Mirar el Dashboard A cada **30 min** y registrar en E12.
- `collect-mariadb-status.sh` debe seguir corriendo durante toda la prueba (verificar cada hora que el último archivo es reciente).
- Asegurar que la terminal/sesión no se cierre (usar `tmux` o `screen`; verificar que estén instalados en la máquina de ejecución).
- Anotar la memoria de `kiwi-perf-0` y `kiwi-perf-mariadb-0` **antes** de empezar (nivel previo) y **después** del cooldown para evaluar el criterio "no regresa a menos de 25 % sobre su nivel previo".

Tabla E12. Resistencia — tendencia por hora

| Hora de prueba | Mediana U1 | Mediana U2 | Mediana U3 | Mediana U4 | Tasa de error (%) | Mem. kiwi (prom./máx.) | Mem. MariaDB (prom./máx.) | CPU kiwi (prom.) | CPU MariaDB (prom.) | Reinicios acumulados |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | | | | | | | | | | |
| 2 | | | | | | | | | | |
| 3 | | | | | | | | | | |
| 4 | | | | | | | | | | |

Tabla E13. Evaluación de umbrales de resistencia

| Umbral | Cálculo | Resultado | ¿Se supera? |
| --- | --- | --- | --- |
| Degradación del tiempo | (mediana última hora − mediana primera hora) / mediana primera hora > 25 % (por operación U1–U4) | | |
| Degradación de memoria kiwi | Memoria tras cooldown vs nivel previo: ¿> +25 %? | | |
| Degradación de memoria MariaDB | ídem | | |
| Mediana de usuarios < 8 s | | | |

Tabla E14. Comparación de memoria pre/post (resistencia)

| Pod | Mem. antes (MiB) | Mem. máx. en prueba | Mem. 5 min tras cooldown | Variación vs antes (%) |
| --- | --- | --- | --- | --- |
| kiwi-perf-0 | | | | |
| kiwi-perf-mariadb-0 | | | | |

---

## 8. Prueba de estrés (Objetivo 2) — jueves

Configuración: 18 usuarios virtuales, A1 cada 200 s, 30 min de medición, 3 repeticiones, misma mezcla de operaciones y mismo cálculo por usuario que en carga.

Particularidades:
- Es la prueba de mayor riesgo para el nodo: mirar el Dashboard B **cada 2–3 min**.
- Anotar en E15 si hay errores, timeouts, 5xx, reinicios, OOM y a qué minuto empiezan.
- Durante el cooldown de 5 min, el sondeo (1 VU, U3 cada 10 s) mide el **tiempo de recuperación**: segundos hasta que la mediana móvil (ventana de 1 min) de U3 vuelve a ≤ la mediana de U3 de la línea base (Tabla E11, combinado).
- Si en el cooldown no se recupera, extender el seguimiento (con consentimiento del usuario) hasta recuperar o 15 min y anotarlo.

Tabla E15. Estrés — fallos observados

| Rep. | Minuto de inicio de fallos | Tipo de fallo (timeout, HTTP 5xx, error JSON-RPC, conexión rechazada, OOM, throttling, otro) | Pod afectado | Evidencia (captura/log) | Cantidad |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

Tabla E16. Estrés vs línea base

| Métrica | Línea base (E11 comb.) | Estrés rep. 1 | Rep. 2 | Rep. 3 | Estrés combinado | Aumento (%) | ¿> 50 %? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Mediana U1 | | | | | | | |
| Mediana U2 | | | | | | | |
| Mediana U3 | | | | | | | |
| Mediana U4 | | | | | | | |
| Tasa de error (%) | | | | | | | |
| Duración mediana A1 | | | | | | | |
| Throughput (req/s) | | | | | | | |
| CPU kiwi máx. | | | | | | | |
| Mem. kiwi máx. | | | | | | | |
| CPU MariaDB máx. | | | | | | | |
| Mem. MariaDB máx. | | | | | | | |

Nota sobre el aumento de la tasa de error: se reporta siempre el valor absoluto en puntos porcentuales (p. ej. 0 % → 2.3 %); no se calcula porcentaje relativo.

Tabla E17. Tiempo de recuperación

| Rep. | Hora fin de la carga | Hora de retorno a la línea base | Segundos de recuperación | ¿Recuperó en el cooldown? | ¿Corrida de A1 activa durante el sondeo? |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

Duración estimada de la jornada: 3 × 40 min + 2 × 5 min ≈ 2 h 10 min más restores.

---

## 9. Después de cada jornada

- [ ] `kubectl get pods -n kiwi` → producción sin cambios; anotar reinicios nuevos.
- [ ] Respaldar `results/` fuera del pod (copiar desde el PVC con `kubectl cp` y verificar tamaños).
- [ ] Exportar los dashboards (JSON o PNG) del rango de la jornada.
- [ ] Dejar la réplica corriendo solo si habrá otra prueba al día siguiente; si no, ver cleanup en el plan de infraestructura (requiere confirmación del usuario).
- [ ] Completar Tabla E18 con la jornada.

Tabla E18. Bitácora de jornada

| Fecha | Prueba | Inicio | Fin | Reps. completadas / planificadas | Abortos | Desviaciones del plan | Notas |
| --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | |

---

## 10. Cierre y matriz de riesgo (Objetivo 4)

1. Ejecutar `analysis/report.py all` para consolidar todas las tablas (carga, resistencia, estrés).
2. Con los resultados, el equipo de QA califica cada hallazgo de 1 a 5 en probabilidad e impacto (Tabla 8 de la metodología). El agente no califica por su cuenta; transcribe lo que acuerde el equipo.

Tabla E19. Hallazgos y calificación

| Área | Hallazgo (con métrica y tabla de origen) | Probabilidad (1–5) | Impacto (1–5) | Puntuación (P × I) |
| --- | --- | --- | --- | --- |
| Carga | | | | |
| Resistencia | | | | |
| Estrés | | | | |

(Se pueden agregar varias filas por área si hay varios hallazgos.)

Tabla E20. Ranking y recomendación

| Posición | Área | Puntuación (máximo de las puntuaciones de sus hallazgos) | Recomendación para QA |
| --- | --- | --- | --- |
| 1 | | | |
| 2 | | | |
| 3 | | | |

3. Ejecutar `analysis/risk_matrix.py` con la tabla E19 para producir la matriz y el ranking, y compararlos con E20.
4. Adjuntar a la entrega: tablas E8–E17, E19–E20, gráficos y las capturas de Grafana.

---

## 11. Pendientes adicionales de este plan

Sin pendientes abiertos. Decididos: P18 = valor absoluto en puntos porcentuales; P19 = máximo por área.
