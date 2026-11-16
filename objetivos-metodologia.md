# OBJETIVOS

## Objetivo General

Determinar cuál de los tres tipos de pruebas de rendimiento (carga, estrés o resistencia) revela el mayor riesgo para la instancia local de Kiwi TCMS, variando la carga concurrente mediante su API JSON-RPC y con k6 en un ambiente de pruebas, para entregar al equipo de QA una recomendación de prioridad respaldada por métricas de tiempo de respuesta, tasa de error y uso de recursos.

## Objetivos Específicos

1. **Carga:** Establecer, antes del final del semestre II-2026 y con métricas de tiempo de respuesta (p95 y p99), throughput, tasa de error y uso de CPU y memoria, la línea base de rendimiento de Kiwi TCMS, mediante una prueba de carga de 60 minutos, con 6 usuarios virtuales concurrentes que ejecutan múltiples solicitudes y una operación masiva de ≈1500 elementos cada 10 minutos.
2. **Estrés:** Evaluar el comportamiento de Kiwi TCMS bajo una carga tres veces mayor que la línea base mediante una prueba de estrés de 30 minutos, con 18 usuarios virtuales y una operación masiva cada 200 segundos, manteniendo la misma mezcla de operaciones. Medir con respecto a la línea base del objetivo 1, el aumento de la mediana del tiempo de respuesta y de la tasa de error, el tipo de fallo observado y el tiempo de recuperación posterior.
3. **Resistencia:** Evaluar, antes del final del semestre II-2026, la estabilidad de Kiwi TCMS mediante una prueba de resistencia de 4 horas, con el mismo escenario de la prueba de carga, midiendo la tendencia del tiempo de respuesta, la tasa de error y el uso de memoria de los pods y utilizando umbrales definidos por las métricas actuales de la aplicación.
4. Elaborar antes del final del semestre II-2026 una matriz de riesgo que compare los hallazgos de las pruebas de carga, estrés y resistencia, calificando cada uno en una escala de 1 a 5 según su probabilidad de ocurrencia en el uso real del equipo, su impacto en el trabajo del equipo  y presentar al equipo de QA un ranking de las tres áreas con la recomendación sobre cuál requiere mayor atención.

# METODOLOGÍA

## Enfoque de la investigación

Estudio de caso aplicado, de enfoque cuantitativo y experimental. Se ejecutan pruebas de carga, estrés y resistencia sobre una réplica de Kiwi TCMS, con un perfil de uso derivado de la operación real del equipo, y se comparan las métricas obtenidas para determinar qué tipo de prueba revela el mayor riesgo.

## Sistema bajo prueba y ambiente

Las pruebas se ejecutan sobre una réplica de la aplicación y su base de datos, en el mismo cluster de Kubernetes que la instancia productiva, en un namespace independiente. Se eligió este ambiente porque no se dispone de un cluster no productivo y porque garantiza el mismo hardware, red y almacenamiento que la instancia real.

Tabla 1. Características del sistema

| Componente | Configuración de la réplica |
| --- | --- |
| Kiwi TCMS | Versión 13.5, mismo manifiesto que producción; requests de 8 CPU y 8Gi; límites de 8 CPU y 16Gi |
| MariaDB | Versión 11.3.2, pod separado; requests de 1 CPU y 3Gi; límites de 6 CPU, 12Gi y 2Gi de almacenamiento efímero |
| Parámetros de MariaDB | innodb_buffer_pool_size de 128 MiB, max_connections de 151, tmpdir en /opt/bitnami/mariadb/tmp; se mantienen sin modificar para reproducir fielmente el sistema en uso |
| Ubicación | Mismo nodo que producción, con ambos pods juntos |
| Datos | Copia de la base de datos de producción (4.3 GB, 71 tablas, 11,472 casos de prueba), restaurada con autorización del equipo |

## Herramientas

Tabla 2. Herramientas

| Función | Herramienta | Justificación |
| --- | --- | --- |
| Generación de carga | k6 | Permite enviar solicitudes JSON-RPC con su módulo HTTP, definir escenarios independientes para usuarios y automatización, y etiquetar cada solicitud por operación |
| Monitoreo de pods | Prometheus y Grafana | Ya disponibles en el cluster; miden CPU, memoria, almacenamiento efímero, reinicios y throttling por pod |
| Análisis de la base de datos | SHOW GLOBAL STATUS de MariaDB | Consultas ejecutadas y uso de tablas temporales |
| Análisis y gráficos | Python | Procesar resultados y construir la matriz |

## Perfil de carga

Descripción de las pruebas de modificación, creación o listado de casos de pruebas, simula operaciones usuales de usuarios reales y automatizaciones.

Tabla 3. Descripción de operaciones de usuarios

| Código | Operación | Tamaño aproximado |
| --- | --- | --- |
| U1 | Listado completo de casos (equivale a abrir "Search test cases") | ≈11,500 casos |
| U2 | Búsqueda filtrada por producto, tipo funcional y un plan de pruebas | ≈1,500 casos |
| U3 | Búsqueda por nombre de un caso | 1 caso |
| U4 | Actualización de un campo del caso encontrado en U3 | 1 caso |

Tabla 4. Descripción de operaciones de automatizaciones

| Código | Operación | Tamaño |
| --- | --- | --- |
| A1 | Registro de ejecuciones de casos de prueba | 1,500 llamadas individuales en un ciclo secuencial; dura ≈3 min |

**Protocolo:** JSON-RPC (HTTP POST con cuerpo JSON), el mismo que utilizan las automatizaciones. Los usuarios virtuales se autentican al inicio y reutilizan su sesión.

**Escenarios en k6:** los usuarios y la automatización se definen como escenarios separados. La automatización se programa con una tasa de llegada fija, de modo que una corrida nueva inicia aunque la anterior no haya terminado, lo que permite el solapamiento observado en la práctica.

**Justificación de los 6 usuarios:** no todos los días se registran casos nuevos. El equipo tiene 80 personas y 120 pull requests abiertos, y en la revisión de 20 ejecuciones de integración continua ninguna activó la automatización de consulta y registro. Con base en esto se asume que 6 usuarios concurrentes es el tráfico máximo de un día normal.

**Justificación de la compresión de A1:** la automatización corre unas 6 veces por noche, cuando casi no hay usuarios. A esa frecuencia, una hora de prueba contendría una corrida como máximo, sin mediciones analizables. Por eso se comprime a una corrida cada 10 minutos (6 por hora) y se superpone a la actividad de los usuarios. Este escenario es un peor caso deliberado, no una réplica del horario real.

## Diseño de las pruebas

Tabla 5. Descripción de las pruebas

| Obj. | Prueba | Diseño | Repeticiones |
| --- | --- | --- | --- |
| 1 | Carga (1x) | 6 usuarios; A1 cada 10 min; 60 min | 3 |
| 2 | Estrés (3x) | 18 usuarios; A1 cada 200 s; 30 min; misma mezcla de operaciones y mismo cálculo por usuario | 3 |
| 3 | Resistencia | Mismo escenario que la carga durante 4 horas, siempre con 6 usuarios y A1 cada 10 min | 1 |
| 4 | Matriz de riesgo | Análisis de los resultados de los objetivos 1 a 3 | No aplica |

Sobre el estrés: se evalúa un único nivel de sobrecarga de 3x (18 usuarios y una corrida de A1 cada ≈3.3 min, es decir, el triple de la frecuencia base), para acotar el riesgo sobre el nodo compartido.Solo cambia la intensidad, no la mezcla, de modo que cualquier diferencia respecto a la línea base se atribuye a la sobrecarga.

## Métricas

**Criterios de cálculo:**

1. Las respuestas JSON-RPC pueden reportar errores dentro del cuerpo con un código HTTP de éxito, por lo que cada solicitud se verifica por contenido y no solo por estado HTTP.
2. Las mediciones de las tres repeticiones se reportan por separado y combinadas.

Tabla 6. Métricas seleccionadas

| Métrica | Fuente | Se reporta |
| --- | --- | --- |
| Tiempo de respuesta de U1 a U4 | k6, con etiqueta por operación | Mediana, promedio y máximo. P95 y p99 si se corren suficientes requests. |
| Tiempo de respuesta por llamada de A1 | k6 | p95 y p99 |
| Duración total de cada corrida de A1 | k6 | Mediana, mínimo y máximo |
| Throughput | k6 | Solicitudes por segundo |
| Tasa de error | k6, verificando el cuerpo de cada respuesta | Porcentaje de solicitudes fallidas |
| CPU, memoria y almacenamiento efímero (Kiwi y MariaDB, por separado) | Prometheus y Grafana | Promedio, máximo y tendencia |
| Reinicios, desalojos y throttling de CPU | Prometheus y eventos del cluster | Cantidad y motivo |
| Tiempo de recuperación | k6 y Grafana | Segundos hasta que el tiempo de respuesta vuelve a la línea base |

## Umbrales de decisión

Tabla 7. Umbrales de decisión de las pruebas

| Umbral | Valor | Justificación |
| --- | --- | --- |
| Tiempo de respuesta aceptable | Mediana de operaciones de usuario menor a 8 s | Duración actual en producción |
| Impacto significativo en el estrés | Aumento de la mediana o de la tasa de error mayor a 50% sobre la línea base | Decisión del equipo |
| Degradación en resistencia (tiempo) | Mediana de la última hora más de 25% sobre la de la primera | Decisión del equipo |
| Degradación en resistencia (memoria) | Memoria que no regresa a menos de 25% sobre su nivel previo | De Grafana, el pico es un 25% superior a la línea base en producción |

## Matriz de riesgo

Cada hallazgo se califica de 1 a 5 en probabilidad e impacto, y su puntuación es probabilidad × impacto:

Tabla 8. Definición de impacto y probabilidad

| Nivel | Probabilidad (uso real del equipo) | Impacto (en el trabajo del equipo) |
| --- | --- | --- |
| 1 | Muy baja | Mínimo: sin efecto perceptible |
| 2 | Baja | Leve: lentitud ocasional |
| 3 | Media | Moderado: interrumpe tareas |
| 4 | Alta | Alto: indisponibilidad temporal |
| 5 | Muy alta: ocurre con el uso diario | Crítico: caída con pérdida de trabajo |

El ranking de las tres áreas se obtiene de las puntuaciones.

## Mitigación de riesgos en pruebas en el cluster de producción

Como la réplica comparte el cluster y el nodo con la instancia productiva, las pruebas de estrés pueden afectar a otras cargas. Para reducir ese riesgo se aplican las siguientes medidas:

1. Autorización previa del administrador del cluster y del equipo de QA, incluyendo la ventana horaria y el uso de una copia de los datos.
2. Namespace dedicado con un ResourceQuota que limite la réplica a la suma de los límites definidos (14 CPU y 28Gi de memoria)
3. Verificación de capacidad del nodo antes de desplegar, comprobando que haya recursos asignables para ambos conjuntos de pods. La carga del nodo es variable debido a las horas laborales; en caso de que el nodo no tenga suficientes recursos, se buscará otra ventana.
4. Ejecución en horarios de bajo uso de la instancia productiva. Primera opción de ventana: martes 6:30 am UTC-6. Esta corresponde a dos horas antes del horario de entrada de los ingenieros de Costa Rica.
5. Monitoreo de producción durante las pruebas (uso del nodo y salud de los pods productivos) con Prometheus y Grafana.
6. Criterios de aborto: se detiene la prueba si el uso de memoria del nodo supera [85]%, si algún pod productivo se reinicia o es desalojado, o si el equipo reporta degradación en producción.
7. Orden de ejecución de menor a mayor intensidad (carga, resistencia y, al final, estrés).