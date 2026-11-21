# Plan de infraestructura — Pruebas de rendimiento de Kiwi TCMS

Este plan describe **qué hay que construir e instalar** antes de ejecutar las pruebas. La ejecución está en `plan-ejecucion.md`. Referencia de objetivos y metodología: `objetivos-metodologia.md`.

> Estado: **implementado y desplegado en el clúster (réplica `-perf`); smoke y ensayo de reseteo ejecutados el 2026-11-20** (ver 5.5 y 10). Los puntos marcados **PENDIENTE** no tienen valor definido y no deben asumirse. Veredicto de preparación en la sección 12.

---

## 1. Hallazgos del cluster (verificados con `kubectl`, 2026-11-16)

| Hallazgo | Detalle | Consecuencia |
| --- | --- | --- |
| Cuenta de acceso | `system:serviceaccount:qa-access:viewer-qa-user` | No puede listar nodos ni usar `kubectl top` (Metrics API no disponible). Métricas de nodo solo vía Grafana. |
| Crear namespace | `kubectl auth can-i create namespace` → **no** | Se despliega **dentro de `kiwi`** (decisión del equipo). |
| Permisos en `kiwi` | Crear/editar pods, statefulsets, deployments, services, configmaps, secrets, PVC, cronjobs, pods/exec, pods/portforward, gateway.networking.k8s.io | Suficiente para desplegar réplica, k6 y jobs. |
| ResourceQuota / LimitRange en `kiwi` | No existen | **No se creará ResourceQuota** (limitaría también a producción). Se usan solo `limits` en cada pod de la réplica. |
| Namespace de monitoreo | `prometheus-stack` (Prometheus, Grafana, kube-state-metrics, node-exporter, pushgateway) | Dashboards en Grafana: `https://pvsdk-metrics.rose.rdlabs.hpecorp.net/` (usuario con permisos de edición). |
| Nodos | `asic-x86-01` y `asic-x86-02`, 128 cores y ≈1,005.5 GiB de memoria asignable cada uno (V3, medido en Grafana el 2026-11-18; ver resultados de V3) | Réplica en `asic-x86-02`, k6 en `asic-x86-01` (ver 5.4). |
| StorageClass | `hpe-standard` | Se usa para los PVC de la réplica. |
| Gateway | `HTTPRoute/kiwi` → `kiwi.rose.rdlabs.hpecorp.net` | La réplica **no** tendrá HTTPRoute ni Ingress (ver 3). |

### Estado de producción (NO TOCAR) y modelo de la réplica

La migración de Kiwi terminó. La BD de producción es **`kiwi-mariadb`** (`StatefulSet/kiwi-mariadb`, release helm `kiwi`) y es el **único modelo** para la réplica. La BD anterior ya no existe en el alcance del proyecto y no se usa como referencia.

Valores verificados en `kiwi-mariadb` (2026-11-16):
- Imagen `docker.io/library/mariadb:11.3` (versión reportada: 11.3.2-MariaDB-1:11.3.2+maria~ubu2204; digest `sha256:e101f9db31916a5d4d7d594dd0dd092fb23ab4f499f1d7a7425d1afd4162c4bc`).
- Args: `--character-set-server=utf8mb3 --collation-server=utf8mb3_general_ci --skip-name-resolve --explicit-defaults-for-timestamp --max-allowed-packet=16M`.
- Variables: `innodb_buffer_pool_size`=134217728 (128 MiB), `max_connections`=151, `tmpdir`=/tmp, `innodb_log_file_size`=100663296.
- Recursos: requests 1 CPU / 3Gi; limits 6 CPU / 12Gi (sin límite de `ephemeral-storage`). PVC 32Gi `hpe-standard`.
- Datos: 71 tablas, 11,492 casos (`testcases_testcase`, conteo al 2026-11-18), ≈5.69 GB (datos + índices).
- Probes y contexto de seguridad: liveness/readiness con `healthcheck.sh`, `runAsUser/Group 999`, `fsGroup 999`.
- App: `StatefulSet/kiwi`, imagen `pvsdk-registry.rose.rdlabs.hpecorp.net/sdk_qa/kiwi:v13.5.5-sdk` (digest `sha256:162e4cb8fd3224e0772de88e83c2636cd18def14806669263c59abff3a103e7e`), `KIWI_DB_HOST=kiwi-mariadb`, requests 4 CPU / 8Gi, limits 8 CPU / 16Gi. Ambos pods corren en `asic-x86-02`.
- El CronJob `kiwi-database-backup` (05:00) hace `mariadb-dump` de `kiwi-mariadb` y lo copia por SSH a `darks.rose.rdlabs.hpecorp.net:/disk/data/backups/kiwi`. Ese es el origen del backup para la réplica.

**Regla:** todo recurso de la réplica lleva el sufijo `-perf` y la etiqueta `app.kubernetes.io/part-of: kiwi-perf`. Nunca se modifica, escala ni elimina un recurso sin ese sufijo/etiqueta. Antes de implementar, re-verificar estos valores con `kubectl` (pueden cambiar).

---

## 2. Arquitectura objetivo

```
namespace kiwi
 ├─ [producción, no tocar] kiwi (sts), kiwi-mariadb (sts), kiwi-http(s), HTTPRoute
 └─ [réplica de pruebas, sufijo -perf]
     ├─ sts/kiwi-perf              (app Kiwi 13.5.5, ClusterIP)
     ├─ sts/kiwi-perf-mariadb      (MariaDB 11.3, ClusterIP)
     ├─ svc/kiwi-perf-http|https, svc/kiwi-perf-mariadb   (solo ClusterIP)
     ├─ pvc/kiwi-perf-media, pvc/mariadb-data-kiwi-perf-mariadb-0
     ├─ secret/kiwi-perf-credentials
     ├─ pod/k6-runner-perf         (generador de carga + recolector de métricas de MariaDB)
     └─ pvc/k6-results-perf        (resultados)
namespace prometheus-stack → Prometheus + Grafana (lectura; dashboards importados por el usuario)
```

Sin gateway, sin ingress, sin NodePort: el tráfico de k6 va solo por el Service ClusterIP `https://kiwi-perf-https.kiwi.svc:8443`. En producción el puerto 8080 solo responde 301 a HTTPS y el gateway enruta a `kiwi-https:8443`; se usa el Service (IP virtual y DNS estables) y no la IP del pod porque ésta cambia en cada reinicio de la réplica y para reproducir la ruta de producción. k6 usa `insecureSkipTLSVerify` por el certificado interno autofirmado. Decisión confirmada por el equipo; permisos verificados (`create services` en `kiwi`: sí). Queda por validar en el ensayo (`smoke.js`) que el login JSON-RPC funciona con el `Host` del Service (CSRF/`Referer`).

---

## 3. Estructura del repositorio a crear

```
lab-kiwi/
├─ objetivos-metodologia.md
├─ plan-infraestructura.md        (este archivo)
├─ plan-ejecucion.md
├─ k8s/
│  ├─ 00-secret-kiwi-perf.yaml.example   (plantilla SIN secretos reales)
│  ├─ 10-mariadb-perf.yaml               (StatefulSet + Service + ConfigMap my.cnf)
│  ├─ 20-kiwi-perf.yaml                  (StatefulSet + Services + PVC)
│  ├─ 30-k6-runner.yaml                  (Pod/Job + PVC resultados + ConfigMap con scripts)
│  └─ 90-cleanup.sh                      (borrado por etiqueta part-of=kiwi-perf)
├─ k6/
│  ├─ lib/config.js        (URLs, tags, helpers)
│  ├─ lib/jsonrpc.js       (cliente JSON-RPC + login + verificación del cuerpo)
│  ├─ lib/data.js          (filtros U2, nombres U3, campo U4; SharedArray)
│  ├─ scenarios/load.js    (6 VUs, A1 cada 10 min, 60 min)
│  ├─ scenarios/endurance.js (igual a load, 4 h)
│  ├─ scenarios/stress.js  (18 VUs, A1 cada 200 s, 30 min)
│  ├─ scenarios/smoke.js   (1 VU, una iteración de U1-U4 y A1 reducido)
│  └─ main.js              (opciones comunes; selecciona escenario por variable TEST_TYPE)
├─ scripts/
│  ├─ precheck.sh              (capacidad del nodo, estado de pods de producción)
│  ├─ deploy.sh                (aplica k8s/ en orden y espera readiness)
│  ├─ restore-db.sh            (restaura backup en kiwi-perf-mariadb)
│  ├─ seed-secrets.sh          (crea secrets de la réplica y de k6 desde tcms.conf)
│  ├─ seed-users.sh            (crea usuarios de prueba en la réplica)
│  ├─ run-test.sh              (orquesta warm-up, prueba, cooldown y recolección; muestra en la terminal el progreso de k6 con `kubectl logs -f`)
│  ├─ collect-mariadb-status.sh (SHOW GLOBAL STATUS cada 45 s vía kubectl exec)
│  ├─ collect-prometheus.sh (exporta series de Prometheus a CSV por repetición, ver 7.3c)
│  ├─ collect-ephemeral.sh (du/df de /tmp y del contenedor, cada 45 s, ambos pods de la réplica)
│  ├─ collect-k8s-events.sh    (eventos, reinicios, desalojos)
│  └─ abort-watch.sh           (opcional: vigila reinicios de pods de producción)
├─ dashboards/
│  ├─ kiwi-perf-pods.json
│  └─ kiwi-perf-nodo-produccion.json
├─ analysis/
│  ├─ requirements.txt
│  ├─ parse_k6.py        (CSV/JSON de k6 → tablas)
│  ├─ parse_mariadb.py   (muestras SHOW GLOBAL STATUS → deltas)
│  ├─ report.py          (tablas por prueba, repeticiones por separado y combinadas, gráficos)
│  └─ risk_matrix.py     (matriz de riesgo y ranking)
└─ results/              (en .gitignore; datos crudos por prueba/repetición)
```

---

## 4. Prerrequisitos y verificaciones previas

Cada elemento se verifica antes de desplegar. Los que no puedo averiguar con mi acceso se piden al usuario.

| # | Verificación | Cómo | Quién |
| --- | --- | --- | --- |
| V1 | Re-verificar los valores de `kiwi-mariadb` y `kiwi` (imagen, digest, args, variables, recursos, conteos) listados en la sección 1 | `kubectl get sts -n kiwi -o yaml`, `SHOW VARIABLES` | Agente |
| V2 | Nodo donde corre el pod de producción de la app y de la BD | `kubectl get pods -n kiwi -o wide` | Agente |
| V3 | Memoria y CPU *allocatable* de `asic-x86-01` y `-02` y requests ya asignados | `kubectl describe node` o Grafana (panel de nodo) | Usuario (hecho, ver resultados) (mi cuenta no puede listar nodos) |
| V4 | Que se puede traer la imagen `grafana/k6` (Docker Hub o espejo en `pvsdk-registry.rose.rdlabs.hpecorp.net`) y las imágenes `kiwi:v13.5.5-sdk` y `mariadb:11.3` | `kubectl run` de prueba o consultar el registry | Agente / usuario |
| V5 | Datasource Prometheus en Grafana (nombre y UID) y que existan las series `container_*`, `kube_pod_*`, `node_*` | Grafana → Connections → Data sources; Explore | **Usuario** |
| V6 | Ubicación y procedimiento del backup de Kiwi (CronJob `kiwi-database-backup` copia a `darks.rose.rdlabs.hpecorp.net:/disk/data/backups/kiwi` por SSH) | Dump generado por el agente con `kubectl exec` (ver resultados) | Agente (hecho) |
| V7 | Espacio en `hpe-standard` para 2 PVC nuevos (BD 32Gi como la actual; media 5Gi) | `kubectl apply` de PVC de prueba | Agente |
| V8 | Autorización documentada del administrador y QA (otorgada según el usuario; no bloqueante, no se solicita más) | — | Usuario |

**Resultados de los prechequeos verificados el 2026-11-18 (agente, solo lectura salvo pruebas temporales ya eliminadas):**

| ID | Resultado |
| --- | --- |
| V1 | OK. Imágenes, digests, requests/limits y args de `kiwi` y `kiwi-mariadb` coinciden con la sección 1. MariaDB 11.3.2, 71 tablas, `innodb_buffer_pool_size`=128 MiB, `max_connections`=151, tmpdir `/tmp`. `testcases_testcase` ahora tiene **11,492** casos (eran 11,481; crece con el uso, el conteo definitivo se toma al restaurar). |
| V2 | OK. `kiwi-0` y `kiwi-mariadb-0` en `asic-x86-02`. |
| V4 | Parcial. `grafana/k6` se descargó en `asic-x86-01` en ~11 s (v2.4.0); **versión fijada a `grafana/k6:2.4.0`** (ver 6.1). Las imágenes `kiwi` y `mariadb` ya están en `asic-x86-02` (usadas por producción), no se probó otra descarga. |
| V7 | OK. Se creó y eliminó un PVC de 1Gi en `hpe-standard` (Bound en 15 s) y un **clon por `dataSource` también quedó Bound** (~20 s). Falta comprobar en el ensayo que el clon de 32Gi contiene los datos y que arranca MariaDB. |
| tmux/screen | Ambos disponibles en esta máquina (`/usr/bin/tmux`, `/usr/bin/screen`). |
| V3 | **OK (datos del usuario desde Grafana, 2026-11-18).** Memoria asignable: `asic-x86-01` 1,079,645,020,160 B (≈1,005.50 GiB), `asic-x86-02` 1,079,645,048,832 B (≈1,005.50 GiB). MemTotal: 1,081,897,361,408 B (-01) y 1,081,897,390,080 B (-02), ≈1,007.6 GiB. CPU: 128 cores en cada nodo. Requests de memoria ya asignados: -01 258,587,230,208 B (≈240.8 GiB, 24.0%), -02 170,051,764,224 B (≈158.4 GiB, 15.8%). Requests de CPU ya asignados: 79.8 en ambos nodos (≈62.3% de 128; unidad cores según el usuario, confirmar). Holgura de requests: memoria ≈764.7 GiB (-01) y ≈847.1 GiB (-02); CPU ≈48.2 cores. La réplica pide 5 CPU/11Gi (-02) y k6 2 CPU/4Gi (-01): caben con amplio margen. Nota: el dato anterior "120 cores" era incorrecto; el valor correcto es 128. |
| V5 | **OK (datos del usuario, 2026-11-18).** Datasource Prometheus, nombre "Prometheus"; URL del servidor `http://prometheus-kube-prometheus-prometheus:9090/prometheusop` (con prefijo `/prometheusop`); **UID `prometheus`** (obtenido de la URL de edición del datasource). Series con datos: CPU por pod (`kiwi-0` ≈0.02 cores), memoria por pod (1,237,950,464 B), throttling (0), reinicios (0), último motivo de terminación (1 resultado con valor "Error", por revisar: probablemente un pod de backup), memoria del nodo (`MemAvailable` de -02 = 1,006,494,932,992 B, ≈93.0% libre) y CPU del nodo. Etiquetas en series de cAdvisor: `node` (p. ej. `asic-x86-02`), `instance` (kubelet `IP:10250`), `namespace`, `pod`. `container_fs_writes_bytes_total` existe (5,771,264 B en `kiwi-0`) pero es un contador de escritura acumulada, **no** uso de almacenamiento; no sirve como medida de almacenamiento efímero. **`container_fs_usage_bytes` no devuelve datos**: el almacenamiento efímero se mide con `collect-ephemeral.sh` (ver 7.3b). |
| V6 | **OK (dump generado por el agente, 2026-11-18 22:46–23:37 UTC-6).** Carpeta `dumps/golden-20261118/` (ignorada por git, 365 MB comprimidos): `part00_all_except_big.sql.gz` (esquema y datos de 70 tablas, rutinas, triggers y eventos, `CREATE DATABASE` incluido), `part01_big_schema.sql.gz` (esquema de `testruns_historicaltestexecution`) y `part02…part21` (datos de esa tabla en bloques por `history_id`). Se creó con `mariadb-dump --single-transaction` (sin bloquear producción), cada parte validada con "Dump completed", `gzip -t` OK y `SHA256SUMS` generado. Verificación: **18,541,264 filas** en los bloques = conteo en producción al terminar; 71 tablas (70 + la grande); `testcases_testcase` 11,492 casos en producción. Un primer intento con un solo flujo falló a los ~17 min (el websocket de `kubectl exec` se corta), por eso se hizo por bloques con reintentos (se ven cortes de ~1–2 min; los bloques de 1.5M ids fallaban a veces, los de 400k no). **Orden de restauración:** `part00` → `part01` → `part02…part21` en orden numérico (`zcat partNN | mariadb`). Para acelerar la carga se puede usar `SET unique_checks=0, foreign_key_checks=0` en la sesión; validar en el ensayo. El dump está tomado en caliente: las partes de la tabla histórica pueden diferir en segundos entre sí (tabla de solo inserción). Contiene datos reales de producción (usuarios y hashes incluidos), aceptado por el usuario. |
| Credenciales de `makise` | Decidido (usuario, 2026-11-18): están en `tcms.conf` (raíz del repo, ignorado por git; claves `url`, `username`, `password`). A1 sigue en k6 emulando JSON-RPC, así que k6 usa solo usuario y contraseña (no `url`, que se reemplaza por `https://kiwi-perf-https.kiwi.svc:8443/json-rpc/`; verificar que `url` de ese archivo no apunte a la réplica ni se use contra producción). En la implementación `seed-secrets.sh` crea el secret `kiwi-perf-credentials` desde ese archivo; las credenciales no se escriben en scripts ni en los planes. Funcionan en la réplica porque restaura una copia de producción. Si algún día se ejecuta el script real de Python (`tcms_api`), el archivo debe montarse en `/etc/tcms.conf` del pod. |
| Pendientes del usuario | Ninguno. |


---

## 5. Despliegue de la réplica

### 5.1 Secreto `kiwi-perf-credentials`
Plantilla sin valores en `k8s/00-secret-kiwi-perf.yaml.example`. Claves: `secret_key`, `mariadb-root-password`, `mariadb-username`, `mariadb-password`. Se crea por `kubectl create secret` con valores **nuevos**, no se reutiliza `kiwi-credentials` ni se commitea ningún valor. (Nota: el manifiesto del CronJob de backup de producción contiene una contraseña en texto plano; no copiarla. Informar al equipo.)

### 5.2 MariaDB réplica (`k8s/10-mariadb-perf.yaml`)
- Manifiesto copiado de `sts/kiwi-mariadb` (`kubectl get sts kiwi-mariadb -n kiwi -o yaml`), cambiando únicamente nombres, etiquetas, selector, secret y PVC.
- Imagen `docker.io/library/mariadb:11.3` (11.3.2), idéntica a producción; fijar el mismo digest.
- Recursos: requests 1 CPU / 3Gi; limits 6 CPU / 12Gi, sin límite de `ephemeral-storage` (igual que producción; el `tmpdir=/tmp` usa el almacenamiento efímero del contenedor, que se mide con `collect-ephemeral.sh`, ver 7.3b).
- Args idénticos: `--character-set-server=utf8mb3 --collation-server=utf8mb3_general_ci --skip-name-resolve --explicit-defaults-for-timestamp --max-allowed-packet=16M`. Sin otras modificaciones: `innodb_buffer_pool_size` 128 MiB, `max_connections` 151, `tmpdir` /tmp. Verificar con `SHOW VARIABLES` en ambas.
- PVC 32Gi `hpe-standard` (igual que producción; la BD pesa ≈5.7 GB).
- Service `kiwi-perf-mariadb` ClusterIP 3306.

### 5.3 Kiwi réplica (`k8s/20-kiwi-perf.yaml`)
- Misma imagen `pvsdk-registry.rose.rdlabs.hpecorp.net/sdk_qa/kiwi:v13.5.5-sdk` y mismas variables de entorno que `sts/kiwi` (ver `kubectl get sts kiwi -o yaml`), con `KIWI_DB_HOST=kiwi-perf-mariadb` y credenciales de `kiwi-perf-credentials`. `imagePullSecrets: regcred`.
- Recursos: requests 4 CPU / 8Gi; limits 8 CPU / 16Gi (igual al manifiesto de producción).
- Init container `migrate` como en producción **solo si** el restore deja la BD en un esquema anterior; si el backup ya está al día no ejecuta cambios.
- Volumen `media` en PVC propio y certificados internos: reutilizar el secret `kiwi-internal-certs` (solo lectura) o generar uno propio (decidido).
- Sin HTTPRoute/Ingress. Services `kiwi-perf-http` (8080) y `kiwi-perf-https` (8443) tipo ClusterIP.

### 5.4 Ubicación en nodos
Decisión del equipo: la réplica (`kiwi-perf-0` y `kiwi-perf-mariadb-0`) va en **`asic-x86-02`**, el mismo nodo que producción (`kiwi-0` y `kiwi-mariadb-0`), con `nodeSelector kubernetes.io/hostname: asic-x86-02` en ambos pods. El pod `k6-runner-perf` va en **`asic-x86-01`** (`nodeSelector kubernetes.io/hostname: asic-x86-01`) para que el generador de carga no compita por CPU con la réplica; el tráfico cruza la red entre nodos y esto se documenta como parte del ambiente. Verificar con `kubectl get pods -n kiwi -o wide` que producción sigue en `asic-x86-02` antes de desplegar (V2).

### 5.5 Restauración de datos y usuarios de prueba
1. **Dump golden (ya generado, 2026-11-18):** `dumps/golden-20261118/` (22 partes, 365 MB, `SHA256SUMS`; carpeta ignorada por git; detalles y orden de restauración en la tabla de resultados de la sección 4, fila V6). Se generó en caliente desde `kiwi-mariadb` (las partes grandes pueden diferir en segundos). Si se quiere uno más fresco antes de las pruebas, se repite el mismo procedimiento. Registrar nombre, tamaño y sha256 en `plan-ejecucion.md` (Tabla E1).
2. **Restauración** (`scripts/restore-db.sh`), adaptación a Kubernetes:
   1. Verificar `sha256sum -c SHA256SUMS` en la máquina del operador.
   2. Cargar las partes en el orden indicado en V6 con `zcat parteNN.sql.gz | kubectl exec -i -n kiwi kiwi-perf-mariadb-0 -- mariadb -uroot -p"$MARIADB_ROOT_PASSWORD"` (como root, porque el dump contiene `CREATE DATABASE`); transmitir por stdin evita escribir 365 MB en el `/tmp` del pod. Por fallos de websocket en transmisiones largas, ejecutar parte por parte con reintentos.
   3. Registrar el tiempo de cada paso.
3. Verificar: 71 tablas, 11,492 casos de prueba (`SELECT COUNT(*) FROM testcases_testcase`; el conteo exacto es el del dump usado), 18,541,264 filas en `testruns_historicaltestexecution` y tamaño ≈ 5.7 GB.
4. **Reinicio de la BD entre ejecuciones:** A1 escribe ejecuciones, por lo que la BD debe quedar idéntica antes de cada repetición. Decisión del equipo: en el ensayo previo se prueba primero **clonar un PVC "golden"** (PVC nuevo con `dataSource` apuntando al PVC golden, o el mecanismo equivalente que soporte `hpe-standard`) y se mide el tiempo. Mi cuenta no puede crear `VolumeSnapshot` ni leer la StorageClass `hpe-standard`, por lo que el clonado por PVC se verifica solo intentándolo; si el clonado falla o no es posible, se usa el restore del dump (punto 2) en cada repetición. Los 5 min entre repeticiones son un **mínimo**: las repeticiones se hacen tan rápido como permita el reset, y el tiempo real de reset se registra en la Tabla E3.
   - **Resultado del ensayo (2026-11-20):** el clonado funciona. Restore limpio con `restore-db.sh --reset`: 4,149 s (≈69 min); luego `seed-users.sh`; luego `pvc/mariadb-golden-perf` (32Gi, Bound en 2 s) clonado del PVC de trabajo recién restaurado y sin uso por pruebas. Ciclo de reseteo completo (borrar PVC de trabajo 1 s, clonar desde el golden, MariaDB Ready a los 25 s, Kiwi Ready a los 33 s): **33 s**. El clon contiene 71 tablas, 11,492 casos, 18,541,264 filas en `testruns_historicaltestexecution`, los 18 `perf_user_*` y ningún tag `perf-u4`. El golden incluye los usuarios de prueba, por lo que no hay que ejecutar `seed-users.sh` tras cada reseteo. Restore del dump (≈69 min) queda como alternativa.
   - Con clonado: se mantiene `pvc/mariadb-golden-perf` (BD restaurada y verificada, sin uso); antes de cada repetición se escala `kiwi-perf` y `kiwi-perf-mariadb` a 0, se elimina el PVC de trabajo, se crea uno nuevo clonado del golden y se vuelve a escalar. Hay que evitar que ambos pods monten el mismo volumen (RWO).
   - Con restore del dump: se recrea la base (`DROP DATABASE kiwi; CREATE DATABASE kiwi` con las mismas collation, utf8mb3) y se vuelve a cargar; se reinician los pods de la réplica para partir de caché fría de manera consistente en todas las repeticiones.
5. Usuarios de prueba (`scripts/seed-users.sh`), decididos por el equipo: 18 usuarios normales `perf_user_01..18` (uno por VU; en carga y resistencia se usan los 6 primeros) con permisos de lectura y escritura sobre TestCases, Test Executions, Test Plans y Bugs (mediante un grupo `perf_users` con esos permisos), más el usuario de automatización **`makise`** (superuser, como en producción; el username en la BD es minúscula), compartido por todas las corridas de A1. Se crean con `manage.py shell`/`createsuperuser` en el pod de la réplica y se recrean tras cada reinicio de la BD si el dump/clon no los incluye. Si `makise` ya existe en el dump, solo se le restablece la contraseña en la réplica (`manage.py changepassword makise`); nunca se usan credenciales de producción. Las contraseñas se guardan en `kiwi-perf-credentials`/secret de k6, no en el repo. El detalle exacto de permisos de un usuario QA típico se confirma en el ensayo comparando con la cuenta real (Admin > Users/Groups).

---

## 6. Generador de carga k6

### 6.1 Imagen y pod
- Imagen oficial `grafana/k6:2.4.0` (versión fijada; verificada en Docker Hub el 2026-11-19 y descargada en `asic-x86-01`, `k6 v2.4.0`; digest del índice `sha256:d41bb79cd8de5a7ad619b53570e3712633e8bff0937010a5c6c57a4747e7a276`; usar el tag, o el digest si se quiere inmutabilidad). Registrarla en `plan-ejecucion.md`. Pod `k6-runner-perf` en `kiwi` con requests 2 CPU / 4Gi y limits 8 CPU / 16Gi como valores iniciales (decisión del equipo). Se ajustan en el ensayo midiendo en Grafana: k6 no debe ser el cuello de botella (U1 devuelve ≈11,500 casos por respuesta, lo que consume memoria y CPU del cliente) y sus valores finales quedan registrados en `plan-ejecucion.md` y fijos para las tres pruebas.
- Scripts montados desde un ConfigMap (límite 1 MiB; si se excede, imagen propia o PVC). Resultados en PVC `k6-results-perf` (JSON/CSV por prueba) más `summary.json`.
- El pod solo ejecuta k6; no ejecuta `kubectl exec`. La recolección de MariaDB y del almacenamiento efímero se hace desde la máquina del operador (ver 7.3 y 7.3b).

### 6.2 Diseño de escenarios (derivado de la Tabla 5)

| Escenario | Executor k6 | Parámetros |
| --- | --- | --- |
| `users` (U1–U4) | `constant-vus` | 6 VUs (carga/resistencia) o 18 VUs (estrés); duración 60 min / 4 h / 30 min |
| `automation` (A1) | `constant-arrival-rate` (rate=1 por 10 min = `rate:1, timeUnit:'10m'`; en estrés `rate:1, timeUnit:'200s'`) | `preAllocatedVUs` y `maxVUs` suficientes para permitir solapamiento (una corrida dura ≈3 min, pero puede alargarse bajo carga) |
| `warmup` / `cooldown` | ver 6.4 | 5 min cada uno |

- **A1 (basado en `provision-qa/python_scripts/send_regressions_kiwi.py` y `tcms_api.plugin_helpers.Backend`, tcms-api / kiwitcms-junit.xml-plugin 12.8.1).** El script real recibe un XML JUnit y, por cada caso reportado, hace secuencialmente estas llamadas JSON-RPC (todas con el usuario `makise`):
  1. `TestCase.filter({summary, category__product})` — busca el caso por nombre (si no existe, `TestCase.create`; en la prueba **no** se crean casos: se usan solo casos existentes).
  2. `TestCase.filter({pk, plan})` — verifica si está en el plan; si no, `TestPlan.add_case(plan, case)`.
  3. `TestCase.update(case, {case_status: CONFIRMED})` solo si el caso no está CONFIRMED.
  4. `TestRun.add_case(run, case)` — crea el TestExecution.
  5. `TestExecution.update(execution, {status, start_date, tested_by})`.
  6. `TestExecution.add_comment(...)` solo si el estado es FAILED o ERROR.
  
  Una vez por corrida: `Build.filter({name})`, `TestRun.filter({summary__contains})`, `TestRun.create({summary: "Test Run for <build>, job <id>", plan, build, manager: "makise", start_date})` (un TestRun **nuevo** por corrida), y al final `TestRun.update(run, {stop_date})`. La inicialización de `Backend.configure()` añade unas pocas consultas (Product, Version, Build, Category, Priority, TestCaseStatus, TestExecutionStatus) que k6 hace una vez por corrida.
  
  **Diseño en k6:** cada iteración de A1 crea un TestRun en el plan `Regressions` (id 16 en la BD actual, 9,710 casos asociados; verificar el id en la réplica) y recorre secuencialmente una lista fija de ≈1,500 nombres de casos de ese plan (sin pausas entre llamadas, como la automatización real). Estado de cada ejecución: distribución observada en las últimas 8 corridas reales del plan (7,852 ejecuciones): PASSED 91.6 %, FAILED 5.5 %, SKIPPED 2.1 %, ERROR 0.9 %; k6 reproduce esas proporciones con una semilla fija. Cada corrida real tiene entre ≈84 y ≈1,410 ejecuciones (últimas 8); la prueba usa ≈1,500 por decisión de la metodología. **Resuelto (P10b):** las "1,500 llamadas individuales" son **1,500 casos** (un caso por prueba del XML JUnit, una iteración del bucle del script). Cada caso son ≥4 solicitudes HTTP (`TestCase.filter`, `TestCase.filter`, `TestRun.add_case`, `TestExecution.update`; más `add_comment` en FAILED/ERROR, y `TestCase.update`/`TestPlan.add_case` ocasionales), es decir ≈6,000–6,500 solicitudes por corrida de A1. Verificación con datos reales: las corridas de hoy duraron 117–174 s para 1,070–1,410 ejecuciones (≈0.12 s por caso, ≈8 casos/s), consistente con ≈3 min para 1,500; el máximo en 30 días fue 1,436 ejecuciones. Las métricas de A1 en k6 se reportan por solicitud (tag `op=A1_<método>`) y por corrida (duración total).
- **Mezcla de usuarios (decidido):** cada iteración de un VU ejecuta en secuencia U1, U2, U3 y U4, una vez cada una, con una **pausa aleatoria de 1 a 3 s entre operaciones** (*think time*). En el estrés la mezcla y el cálculo por usuario son idénticos; solo cambia el número de VUs (18) y la frecuencia de A1.
- **Datos de entrada (decididos, se guardan en `k6/lib/data.js`):**
  - **U1:** listado completo (≈11,500 casos), sin filtros.
  - **U2:** `TestCase.filter` con producto **PVSDK** (`category__product`), categoría **Functional** (tipo funcional), plan **Regressions** (id 16 en la BD actual; verificar el id en la réplica) y `summary__contains: "gibson"`. Devuelve **1,380 casos** (verificado el 2026-11-17; supera los 1,322 del plan `Features`, y por eso se eligió este filtro).
  - **U3:** búsqueda por nombre exacto de uno de estos tres casos: `test_gibson_mdio_cmd_list`, `test_guild_tcp_fsm_eviction_timers`, `test_tele_tunnel_rep_double_service_entry_config`. Nota: `test_guild_tcp_fsm_eviction_timers` está duplicado (ids 7355 y 15367), por lo que esa búsqueda devuelve **2 casos**; se mantiene así por decisión del equipo y se documenta. Selección por iteración: aleatoria con semilla fija entre los tres nombres (detalle de implementación; si se prefiere rotación fija se cambia en `data.js`).
  - **U4 (cambiado tras el smoke, decisión del usuario 2026-11-20):** `TestCase.update` por JSON-RPC falla siempre en Kiwi 13.5.5-sdk con «Object of type Platform is not JSON serializable» (la escritura se aplica, pero la respuesta no se serializa; probado con 3 casos y 4 campos). U4 alterna `TestCase.add_tag` y `TestCase.remove_tag` del tag `perf-u4` sobre el primer caso devuelto por U3 (una escritura por iteración, respuesta válida). El reinicio de la BD entre repeticiones (sección 5.5) restaura los tags. A1 solo llama a `TestCase.update` si un caso no está CONFIRMED; si ocurre, se contará como error (mismo bug).

### 6.3 Verificación por contenido y etiquetas
- Cada respuesta se valida con `check()` y un contador `Rate` propio: HTTP 200 **y** cuerpo JSON sin campo `error`, `result` presente y, para listados, longitud esperada (≈11,500 en U1, ≈1,500 en U2, 1 en U3).
- Tags por request: `op` (`U1`..`U4`,`A1`,`login`), `scenario`, `test_type`, `rep`, `phase`, `run_id` (identificador de cada corrida de A1, para contar corridas completas o con error) y, en las solicitudes fallidas, `error_type` (p. ej. `http_<código>`, `rpc_error`, `body_invalid`, `timeout`). Estas etiquetas alimentan los errores por operación (E8) y las corridas de A1 con error (E9). Con muchas corridas, `run_id` aumenta la cardinalidad del CSV; se confirma el tamaño en el ensayo.
- Métricas propias: `Trend` por operación (`dur_U1`…`dur_U4`, `dur_A1_call`), `Trend` `a1_run_duration`, `Rate` `rpc_ok`.
- Login (`Auth.login`) al inicio de cada VU y reutilización de sesión (cookie/`sessionid`). Excluir el login de las estadísticas de U1–U4.
- Umbrales k6 (solo informativos, no abortan): mediana de U1–U4 < 8 s (Tabla 7). Los criterios de aborto los aplica el operador (ver `plan-ejecucion.md`).

### 6.4 Warm-up y cooldown (decidido)
Cada prueba tiene tres fases, con tag `phase=warmup|measure|cooldown`:

| Fase | Duración | Usuarios (U1–U4) | A1 |
| --- | --- | --- | --- |
| Warm-up | 5 min | Rampa lineal de 0 al número de VUs (6 o 18) durante los 5 min (`ramping-vus`) | No corre |
| Medición | 60 min (carga), 240 min (resistencia), 30 min (estrés) | Número de VUs completo | Empieza en el minuto 0 de la medición (`startTime` = 5 min), con `constant-arrival-rate` |
| Cooldown | 5 min | 1 VU de sondeo: una petición U3 cada 10 s (sin think time aleatorio) | No se lanza ninguna corrida nueva |

- Warm-up y cooldown van **fuera** de los 60/30/240 min: se suman a la duración total (carga 5+60+5, resistencia 5+240+5, estrés 5+30+5). Las tablas de métricas usan solo `phase=measure`. El cooldown se usa solo para el tiempo de recuperación.
- Los usuarios de la medición se mantienen al 100 % hasta el final de la medición; al empezar el cooldown pasan de golpe de 6/18 VUs a 1 VU de sondeo.
- Una corrida de A1 iniciada durante la medición **se deja terminar** aunque la medición acabe (en estrés, con A1 cada 200 s, la última corrida puede solaparse con el cooldown). Sus solicitudes se etiquetan con la fase **en que empezó la corrida** (`phase=measure`), de modo que la duración total de cada corrida de A1 sea completa. El sondeo de recuperación se interpreta con esa corrida en curso; se anota en la Tabla E17 si había una corrida de A1 activa.
- Como el warm-up no incluye A1, la primera corrida de A1 encuentra los cachés calentados solo por las operaciones de usuario; es aceptable y queda documentado.

### 6.5 Salidas
`k6 run --out csv=/results/<test>_rep<N>.csv --summary-export /results/<test>_rep<N>_summary.json`. El CSV completo es la **fuente oficial de resultados** (p95/p99 por operación, mediana, promedio, máximo y throughput) y siempre se genera, haya o no vista en vivo.

**Sin vista en vivo de k6 en Grafana (P14, decidido el 2026-11-19):** no se usa remote-write. El progreso se sigue en la salida de la terminal de k6 (`kubectl logs -f` del pod `k6-runner-perf`) y los resultados salen del CSV al terminar; `report.py` grafica k6 junto con CPU/RAM después de cada repetición. Queda como mejora futura si se quiere un dashboard en vivo (requeriría que el administrador habilite el receptor remote-write de Prometheus).

---

## 7. Monitoreo

### 7.1 Métricas del cluster (Prometheus/Grafana)
Dashboards en `dashboards/` (JSON provisto en la implementación) con variables `datasource`, `namespace=kiwi`, `pod` (regex `kiwi-perf.*`, `kiwi-mariadb.*`, `kiwi-0`…), y `node`. Paneles y consultas base (ajustar nombres de etiquetas a lo que exista, V5):

**Dashboard A: "Kiwi Perf – Pods réplica"** (título exacto, para que el plan de ejecución lo referencie)

| Panel | PromQL (referencia) |
| --- | --- |
| CPU por pod (cores) | `sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="kiwi",pod=~"kiwi-perf.*",container!="",container!="POD"}[1m]))` |
| Memoria working set | `sum by (pod) (container_memory_working_set_bytes{namespace="kiwi",pod=~"kiwi-perf.*",container!=""})` |
| Ephemeral storage | `sum by (pod) (kubelet_container_log_filesystem_used_bytes{namespace="kiwi",pod=~"kiwi-perf.*"})` (solo logs; el almacenamiento efímero total está en `ephemeral.csv`, ver 7.3b) |
| Throttling de CPU (%) | `sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{...}[1m])) / sum by (pod) (rate(container_cpu_cfs_periods_total{...}[1m]))` |
| Reinicios | `sum by (pod) (kube_pod_container_status_restarts_total{namespace="kiwi",pod=~"kiwi-perf.*"})` |
| Desalojos / OOM | `kube_pod_status_reason{namespace="kiwi",reason="Evicted"}`, `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` |
| CPU/Mem vs limits | `kube_pod_container_resource_limits{resource=~"cpu|memory"}` |

**Dashboard B: "Kiwi Perf – Nodo y producción"**

| Panel | PromQL (referencia) |
| --- | --- |
| Uso de memoria del nodo (%) — criterio de aborto | `100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)` filtrado por el nodo de la réplica |
| CPU del nodo (%) | `100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[1m])))` |
| Reinicios de pods de producción | `kube_pod_container_status_restarts_total{namespace="kiwi",pod!~".*-perf.*"}` |
| Estado de pods de producción | `kube_pod_status_phase{namespace="kiwi",pod!~".*perf.*"}` |
| CPU/memoria de pods de producción | mismas consultas que el dashboard A con `pod!~".*perf.*"` |

Pasos de importación (los ejecuta el usuario; hay detalle de uso en el plan de ejecución):
1. Grafana → Dashboards → New → Import → subir `dashboards/<archivo>.json`.
2. Seleccionar el datasource Prometheus (V5).
3. Dejar el título propuesto (arriba), carpeta `Kiwi Perf`, guardar.
4. Verificar que cada panel muestra datos para los pods `kiwi-*` actuales (que ya existen) y que el selector de nodo incluye `asic-x86-01`/`-02`.

### 7.2 Alertas / aborto
Los criterios de aborto (sección 12 de la metodología) se vigilan manualmente en el dashboard B. El umbral de memoria del nodo definido por el equipo es **30 %** (reemplaza el [85]% del documento) y se define como memoria usada total del nodo (`MemTotal - MemAvailable`). Consumo basal observado en Grafana (últimas 25 h): mínimo 4.9 %, máximo 9 %, y ≈5 % a las 6:30 am, por lo que hay margen. Como la suma de limits de la réplica (28Gi) es ≈3 % de la memoria del nodo, este umbral es una cota de seguridad que protege contra cargas ajenas a la prueba. Opcional: regla de alerta de Grafana en el dashboard B con ese umbral (requiere permiso del usuario; no es necesario para ejecutar).

### 7.3 MariaDB (`SHOW GLOBAL STATUS`)
`scripts/collect-mariadb-status.sh` ejecuta cada **45 s**:
```
kubectl exec -n kiwi kiwi-perf-mariadb-0 -- mariadb -uroot -p"$ROOT_PW" -e "SHOW GLOBAL STATUS;" 
```
guardando una marca de tiempo y la salida cruda en `results/<test>/rep<N>/mariadb_status_<ts>.txt` (la contraseña se lee del secret `kiwi-perf-credentials`, no se escribe en el script). Variables de interés: `Queries`, `Questions`, `Com_select`, `Com_update`, `Com_insert`, `Created_tmp_tables`, `Created_tmp_disk_tables`, `Created_tmp_files`, `Threads_connected`, `Threads_running`, `Max_used_connections`, `Innodb_buffer_pool_reads`, `Innodb_buffer_pool_read_requests`, `Slow_queries`, `Aborted_connects`. `parse_mariadb.py` calcula deltas por intervalo. El proceso corre en la máquina del operador durante toda la prueba (también la de 4 h).

### 7.3b Almacenamiento efímero (decidido)
Prometheus no expone el uso de almacenamiento efímero de los contenedores (`container_fs_usage_bytes` sin datos; la métrica `kubelet_container_log_filesystem_used_bytes` solo cubre logs, ≈20.9 MB en `kiwi-0` el 2026-11-18). Por eso:
- `scripts/collect-ephemeral.sh <test> <N>` ejecuta cada **45 s**, por `kubectl exec`, en `kiwi-perf-0` y `kiwi-perf-mariadb-0`: `du -sk /tmp` y `du -sxk /` (sistema de archivos del contenedor sin volúmenes montados; incluye las capas de la imagen, ≈550 MB en kiwi y ≈415 MB en MariaDB al inicio, por lo que importa la variación; `df /` no sirve: mide el disco compartido del nodo, ≈1.18 TB), y escribe `results/<test>/rep<N>/ephemeral.csv` con marca de tiempo, pod, `tmp_kb` y `rootfs_du_kb`. Para MariaDB, `/tmp` es el `tmpdir` donde caen las tablas temporales en disco.
- Se complementa con `kubelet_container_log_filesystem_used_bytes` (logs) del Dashboard A.
- `parse_mariadb.py` o `report.py` calculan máximo y tendencia por pod para la columna "Ef. storage máx." de la Tabla E5.
- Los comandos `du`/`df` deben existir en las imágenes; se comprueban en el ensayo (si faltan, se usa `/proc` o `stat -f`).

### 7.3c Exportación de Prometheus a CSV (decidido en esta revisión)
`scripts/collect-prometheus.sh <test> <N> <inicio> <fin>` consulta la API `query_range` (paso 15 s) al terminar cada repetición y guarda en `results/<test>/rep<N>/prom_*.csv`: CPU, memoria (`working_set`), throttling y reinicios de `kiwi-perf-0`, `kiwi-perf-mariadb-0`, `kiwi-0` y `kiwi-mariadb-0`; memoria y CPU de `asic-x86-01` y `-02`. Con eso las columnas CPU/memoria/throttling prom., máx. y tendencia de E5 las calcula `report.py` y quedan datos crudos reproducibles; la lectura en Grafana pasa a ser comprobación visual. Las consultas son las verificadas en V5, con las etiquetas `namespace`, `pod`, `node`, `container!=""`.
- **Acceso (verificado 2026-11-19):** token de servicio de Grafana en el archivo `GRAFANA_TOKEN` (raíz del repo, ignorado por git, solo contiene el valor). Prueba exitosa: `GET https://pvsdk-metrics.rose.rdlabs.hpecorp.net/api/datasources/proxy/uid/prometheus/api/v1/query?query=up` con `Authorization: Bearer <token>` devuelve `status: success` (se necesitó `-k` por el certificado interno); `query_range` también funciona. `collect-prometheus.sh` lee el token del archivo (o de `GRAFANA_TOKEN` en el entorno) y nunca lo imprime. Falta confirmar que el token no expire antes de las pruebas.
- **Retención de Prometheus (medida):** hay datos de hasta ≈10 días atrás y ninguno a 11 días, así que la retención es ≈10 días. Las 3 jornadas (martes a jueves) y el análisis deben terminar dentro de ese plazo; por eso `collect-prometheus.sh` exporta el CSV al terminar **cada repetición**.

### 7.4 Eventos del cluster
`scripts/collect-k8s-events.sh`: `kubectl get events -n kiwi --sort-by=.lastTimestamp -o json` al inicio y fin, y `kubectl get pods -n kiwi -o wide` + `kubectl describe pod` de cada pod relevante al final de cada prueba (reinicios, desalojos y motivo).

---

## 8. Análisis (Python)

- `analysis/requirements.txt`: `pandas`, `numpy`, `matplotlib`, `jinja2`/`tabulate` (versiones fijadas al implementar).
- `parse_k6.py`: carga el CSV, separa por `phase`, calcula por operación mediana, promedio, máximo, p95, p99 con la regla de muestras de abajo, throughput (req/s) y tasa de error por contenido; duración de cada corrida A1 (mediana/mín/máx). Además: errores y tasa de error **por operación** (U1–U4 y A1), tipo de error más frecuente (por la etiqueta `error_type` de los checks), y de A1 las corridas iniciadas, completas y con error (con la llamada que falló), usando la etiqueta `run_id` de cada corrida.
- **Regla de percentiles (decidida):** por operación y por repetición, se reporta **p95 solo con ≥ 20 muestras** y **p99 solo con ≥ 100 muestras**; si no se alcanzan, se escribe `n/d` en esa repetición y se calcula en el **combinado de las 3 repeticiones** cuando ya cumpla el mínimo. Si ni así se cumple (p. ej. resistencia, 1 repetición), se reporta únicamente la mediana (junto con promedio y máximo, que siempre se reportan). `parse_k6.py` imprime `N muestras` junto a cada percentil. La unidad de muestra es la solicitud de la operación (en A1, cada llamada individual, por lo que p95/p99 de A1 siempre cumplen).
- `parse_mariadb.py`: deltas por intervalo de 45 s.
- `report.py`: genera las tablas por repetición y combinadas (criterio 2 de métricas), las tendencias de resistencia (mediana por hora, mediana de última hora vs primera: umbral 25%), el aumento de mediana y error del estrés frente a la línea base (umbral 50%) y los tiempos de recuperación.
- `risk_matrix.py`: lee un CSV/YAML con probabilidad e impacto (1–5) por hallazgo (los califica el equipo), calcula P×I, ordena el ranking de las tres áreas y genera la tabla final.
- Las gráficas que se generen se guardan en `results/<test>/figures/`.

---

## 9. Seguridad y mitigación (resumen de la sección de mitigación de la metodología)

1. Autorización del administrador y QA: otorgada (confirmado por el usuario); no se requiere registrar fecha ni medio.
2. Sin ResourceQuota (decisión): la contención se logra con **limits en cada pod** de la réplica y de k6. Suma de limits de la réplica: 8 + 6 = 14 CPU / 16 + 12 = 28Gi (más el pod k6, limits 8 CPU / 16Gi en `asic-x86-01`).
3. Verificación de capacidad del nodo antes de desplegar (`scripts/precheck.sh` + dashboard B).
4. Ventana: martes 6:30 am UTC-6 (carga), miércoles (resistencia) y jueves (estrés), misma hora.
5. Monitoreo de producción durante las pruebas (dashboard B).
6. Criterios de aborto (ver `plan-ejecucion.md`).
7. Orden: carga → resistencia → estrés.

---

## 10. Ensayo previo (smoke test) — obligatorio antes de la primera ventana

1. Desplegar la réplica, restaurar datos, crear usuarios.
2. `smoke.js`: 1 VU, un ciclo U1–U4 y una A1 reducida (p. ej. 20 llamadas) para validar login, formato JSON-RPC, verificación por contenido y escritura de CSV.
3. Medir: duración de U1 con 1 VU, tiempo de restore, que A1 con 1,500 llamadas dure ≈3 min y que el CSV generado sea procesable por `parse_k6.py`.
4. Comprobar que los dashboards muestran los pods de la réplica y el nodo.
5. Probar la recolección de MariaDB durante 5 min.
6. Probar el procedimiento de restauración entre ejecuciones y registrar el tiempo: clonado de PVC y, si falla, restore del dump. **Hecho (2026-11-20): clonado de PVC, 33 s; ver 5.5.**
7. Confirmar que el consumo del nodo y de producción no cambió significativamente durante el ensayo.

El ensayo se hace fuera de la ventana de pruebas (idealmente en horario de bajo uso) y con la misma autorización.

---

## 11. Limpieza

`k8s/90-cleanup.sh`: `kubectl delete sts,svc,pvc,secret,configmap,pod,job -n kiwi -l app.kubernetes.io/part-of=kiwi-perf`. Se ejecuta solo al terminar el proyecto y después de exportar los resultados; confirmar con el usuario que los PVC pueden eliminarse.

---

## 12. Estado de preparación

Decisiones P1–P19 resueltas; prechequeos V1–V7 verificados (sección 4). Veredicto: **listo para empezar la implementación**, con estas condiciones que no bloquean el inicio pero requieren confirmación manual:

| # | Pendiente | Quién | Cuándo |
| --- | --- | --- | --- |
| 2 | Confirmar que "79.8" de requests de CPU en los nodos son cores | Confirmado | — |
| 3 | Token `GRAFANA_TOKEN` expira el 2026-12-01: todas las ventanas y el análisis (retención ≈10 días) deben terminar antes | Usuario | Planificar fechas |
| 4 | Confirmar que el `url` de `tcms.conf` es el del Service/Ingress adecuado y que las credenciales de `makise` sirven en la réplica | Prueba de login previa o en ejecución | Ensayo |
| 6 | Decidir si el dump `golden-20261118` es el definitivo o se genera uno más reciente | Confirmado | — |
| 7 | Reportar al equipo que el CronJob `kiwi-database-backup` contiene una contraseña en texto plano | Usuario | Cuando convenga |

Se validan en el ensayo (sección 10): arranque de MariaDB sobre un clon del PVC con datos (**ya validado**, 33 s), login JSON-RPC con el host del Service (CSRF/Referer), dimensionamiento del pod de k6, disponibilidad de `du`/`df` en las imágenes, `tmux`/`screen` en la máquina de ejecución (disponibles en la del agente) y el impacto en nodo y cluster.
