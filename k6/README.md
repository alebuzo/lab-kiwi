# k6 — carga JSON-RPC sobre Kiwi TCMS (réplica `-perf`)

Todo el árbol `k6/` se monta en `/scripts` (ConfigMap/PVC); `main.js` elige el escenario por `TEST_TYPE`.

```
k6 run -e TEST_TYPE=<load|endurance|stress|smoke> -e REP=<N> \
  --out csv=/results/<TEST_TYPE>/rep<N>/k6.csv \
  --summary-export /results/<TEST_TYPE>/rep<N>/summary.json /scripts/main.js
```

## Variables de entorno
| Variable | Default | Uso |
| --- | --- | --- |
| `TEST_TYPE`, `REP` | `smoke`, `1` | prueba y repetición (tags `test_type`, `rep`) |
| `BASE_URL` | `https://kiwi-perf-https.kiwi.svc:8443` | endpoint = `<BASE_URL>/json-rpc/` |
| `MAKISE_USER` / `MAKISE_PASSWORD` | `makise` / — | A1 y `setup()` |
| `PERF_USER_PASSWORD` | — | clave compartida de `perf_user_01..18`; VU *i* usa `perf_user_<i 2 dígitos>` |
| `A1_CASES` | 1500 (smoke: 20) | casos por corrida de A1 |
| `SEED` | 20261119 | semilla (think time, U3, estados A1, orden de casos) |
| `U1_MIN`, `U2_MIN`, `U2_MAX` | 10000, 1000, 2000 | cotas de validación de longitud (U3: 1–2) |
| `PLAN_ID`, `PRIORITY_MEDIUM_ID`, `PRIORITY_HIGH_ID` | 16, 16, 15 | solo respaldo si `setup()` no resuelve por nombre |
| `REQ_TIMEOUT` | `120s` | timeout por request (→ `error_type=timeout`) |
| Overrides para ensayos cortos | | `WARMUP_S` (300), `MEASURE_S` (3600/14400/1800), `COOLDOWN_S` (300), `A1_PERIOD_S` (600/200), `VUS` (6/18), `PROBE_PERIOD_S` (10) |

Duración total = warm-up + medición + cooldown (5+60+5, 5+240+5, 5+30+5 min). Smoke: 1 VU, un ciclo U1–U4, A1 de 20 casos, sondeo de 20 s; ≈1 min.

## Escenarios
`users_warmup` (ramping-vus 0→N, fase `warmup`), `users` (constant-vus, `measure`), `automation` (constant-arrival-rate, rate 1 por 600 s / 200 s desde el inicio de la medición, `gracefulStop` 15 min, nunca inicia corridas en cooldown), `recovery_probe` (1 VU, U3 cada 10 s, `phase=cooldown`). Think time 1–3 s (semilla) tras cada operación de usuario. Cookies persisten entre iteraciones (`noCookiesReset`).

## Contrato de métricas
- Tags en cada request y métrica propia: `op` (`login|U1|U2|U3|U4|A1_<Método>`, p. ej. `A1_TestCase.filter`), `scenario`, `test_type`, `rep`, `phase`, `run_id` (solo A1, `<rep>-<n>`), `error_type` (solo fallas: `http_<código>|rpc_error|body_invalid|timeout`).
- Las requests de `setup()` llevan `op=setup`/`phase=setup` (y su login `op=login`, `phase=setup`): excluirlas.
- Trend: `dur_U1..dur_U4`, `dur_A1_call`, `a1_run_duration` (tags `run_id, phase, status=ok|error, calls_failed`). Rate: `rpc_ok`. Counters: `a1_runs_started` (`run_id`), `a1_runs_completed` (`run_id`, `status`). Una corrida es `error` si alguna de sus llamadas falló.
- Los tags aparecen en el CSV en la columna `extra_tags` (querystring, **orden variable**; parsear con `parse_qsl`); `scenario` y `status` (solo counters/trend A1) tienen columna propia. Ver `test/sample_k6.csv`.
- Umbrales informativos: mediana `dur_U1..U4 < 8000 ms` (sin abortar).

## Validación por contenido
HTTP 200, JSON sin `error`, `result` presente; U1 ≥ `U1_MIN`, U2 entre `U2_MIN` y `U2_MAX`, U3 1–2 casos, login devuelve cadena no vacía.

## Prueba local con el mock
```
MOCK_PORT=18443 MOCK_ERROR_RATE=0.05 python3 k6/test/mock_server.py &
BASE_URL=http://<ip-del-host>:18443 k6/test/run.sh <dir_salida> -e TEST_TYPE=smoke -e REP=1
```
(`run.sh` usa Docker con `grafana/k6:2.4.0`; con Docker Desktop/WSL usar la IP de WSL, no `localhost`.) Mock: ~11,500 casos, 1,380 `gibson` en `Regressions`, duplicado `test_guild_tcp_fsm_eviction_timers`, `MOCK_LATENCY_MS` (5), `MOCK_ERROR_RATE`, `MOCK_DEBUG`.

## Validar en ensayo
- **Protocolo**: `tcms-api` 12.8.1 usa XML-RPC, no JSON-RPC; se asume `POST <BASE_URL>/json-rpc/` con cuerpo JSON-RPC 2.0 y `params` posicionales (django-modern-rpc de Kiwi), sin CSRF (se envía `Referer`), y que `Auth.login` devuelve el session id y fija la cookie `sessionid` (el script además la fija en el jar).
- Nombres de filtros: `TestPlan.filter{name}`, `Priority.filter{value__iexact}`, `TestCaseStatus.filter{name:'CONFIRMED'}`, `Build.filter{}` (se usa el build de mayor id), U2/A1 con `category__product__name`, `category__name`, `summary__contains`; `TestRun.create{manager:'makise'}` como en el script real; `TestExecution.update` sin `tested_by`.
- Que el plan `Regressions` sea el de PVSDK/Functional y que U2 devuelva 1,380 (ajustar `U2_MIN/MAX`).
- `TestPlan.add_case` y `TestCase.update(case_status)` solo se llaman si los datos lo requieren (caso fuera del plan / no CONFIRMED), como el script real.
- Mapeo VU→usuario (`perf_user_NN`) usa `idInTest` módulo N; con VUs de A1/sondeo intercalados puede repetirse un usuario.
- `a1_runs_*` no llevan `phase` (siempre `measure`).
