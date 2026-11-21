#!/usr/bin/env bash
# Ejecuta una o varias repeticiones de una prueba de forma desatendida.
# Uso: run-campaign.sh <load|endurance|stress> <rep> [<rep> ...]
# Por repetición: pausa (solo entre repeticiones) -> reset-db.sh -> chequeo previo (A-1/A-2) -> eventos start ->
# collectors + vigilante con aborto automático (A-1, A-2) -> run-test.sh -> cierre (eventos end, Prometheus).
# Si una repetición se aborta o falla, la campaña se detiene (no se reintenta ni se sigue). Al terminar todas, ejecuta report.py.
# Variables: PAUSE_SEC=300, PYTHON=/tmp/venv/bin/python, MEM_THRESHOLD=30, WATCH_INTERVAL=30.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[[ $# -ge 2 ]] || die "uso: $0 <load|endurance|stress> <rep> [<rep> ...]"
TEST="$1"; shift
validate_test_name "$TEST"
REPS=("$@")
for r in "${REPS[@]}"; do validate_rep "$r"; done

PAUSE_SEC="${PAUSE_SEC:-300}"
PYTHON="${PYTHON:-/tmp/venv/bin/python}"
export MEM_THRESHOLD="${MEM_THRESHOLD:-30}"
WATCH_INTERVAL="${WATCH_INTERVAL:-30}"
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$RESULTS_ROOT/$TEST"
STATUS="$RESULTS_ROOT/$TEST/CAMPAIGN_STATUS.txt"
BG_PIDS=()

status() { echo "[$(ts_utc)] $*" | tee -a "$STATUS" >&2; }
stop_bg() {
  local p
  for p in "${BG_PIDS[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null; done
  BG_PIDS=()
}
trap 'status "campaña interrumpida (señal)"; stop_bg; exit 130' INT TERM

status "=== Campaña $TEST reps: ${REPS[*]} (pausa entre reps ${PAUSE_SEC}s, umbral memoria nodo ${MEM_THRESHOLD}%) ==="

first=1
for REP in "${REPS[@]}"; do
  DIR="$RESULTS_ROOT/$TEST/rep$REP"
  if [[ -s "$DIR/k6.csv" ]]; then status "ERROR: ya existe $DIR/k6.csv; campaña detenida"; exit 1; fi
  mkdir -p "$DIR"

  if (( ! first )); then status "rep$REP: pausa de ${PAUSE_SEC}s"; sleep "$PAUSE_SEC"; fi
  first=0

  status "rep$REP: reset de la BD"
  "$SCRIPTS/reset-db.sh" >"$DIR/reset.log" 2>&1 || { status "ERROR: reset-db.sh falló (ver $DIR/reset.log); campaña detenida"; exit 1; }
  tail -n 1 "$DIR/reset.log" | tee -a "$STATUS" >&2

  status "rep$REP: chequeo previo de producción y nodo"
  if ! MAX_ITER=1 "$SCRIPTS/abort-watch.sh" >"$DIR/precheck_watch.log" 2>&1; then
    status "ERROR: el chequeo previo no pasa (ver $DIR/precheck_watch.log); no se inicia la prueba"; exit 1
  fi

  "$SCRIPTS/collect-k8s-events.sh" "$TEST" "$REP" start >/dev/null 2>&1 || status "AVISO: eventos start no se pudieron guardar"

  "$SCRIPTS/collect-mariadb-status.sh" "$TEST" "$REP" >"$DIR/collector_mariadb.log" 2>&1 & BG_PIDS+=($!)
  "$SCRIPTS/collect-ephemeral.sh" "$TEST" "$REP" >"$DIR/collector_ephemeral.log" 2>&1 & BG_PIDS+=($!)
  INTERVAL="$WATCH_INTERVAL" AUTO_ABORT_TEST="$TEST" AUTO_ABORT_REP="$REP" \
    "$SCRIPTS/abort-watch.sh" >"$DIR/abort_watch.log" 2>&1 & BG_PIDS+=($!)

  status "rep$REP: iniciando run-test.sh"
  "$SCRIPTS/run-test.sh" "$TEST" "$REP" >"$DIR/run_test.log" 2>&1
  RC=$?
  stop_bg
  status "rep$REP: run-test.sh terminó con código $RC"

  "$SCRIPTS/collect-k8s-events.sh" "$TEST" "$REP" end >/dev/null 2>&1 || status "AVISO: eventos end no se pudieron guardar"
  T_START="$(sed -n 's/^start_utc=//p' "$DIR/times.txt" 2>/dev/null | head -n 1)"
  T_END="$(sed -n 's/^end_utc=//p' "$DIR/times.txt" 2>/dev/null | head -n 1)"
  if [[ -n "$T_START" && -n "$T_END" ]]; then
    T_END_PLUS="$(epoch_to_utc $(( $(to_epoch "$T_END") + 60 )))"
    "$SCRIPTS/collect-prometheus.sh" "$TEST" "$REP" "$T_START" "$T_END_PLUS" >"$DIR/collect_prometheus.log" 2>&1 \
      || status "AVISO: collect-prometheus.sh falló (ver $DIR/collect_prometheus.log); repetir a mano antes de 10 días"
  else
    status "AVISO: sin times.txt completo; collect-prometheus.sh no se ejecutó"
  fi

  if [[ -f "$DIR/ABORTED.txt" ]]; then
    status "ABORTADA rep$REP: $(sed -n 's/^motivo=//p' "$DIR/ABORTED.txt"); campaña detenida; no cuenta como repetición válida"
    exit 2
  fi
  if (( RC != 0 )); then status "FALLÓ rep$REP (código $RC); campaña detenida"; exit 1; fi
  status "rep$REP: COMPLETA"
done

status "Generando reporte"
"$PYTHON" "$SCRIPTS/../analysis/report.py" --test "$TEST" --results "$RESULTS_ROOT" --baseline-test "$TEST" >"$RESULTS_ROOT/$TEST/report.log" 2>&1 \
  || status "AVISO: report.py falló (ver $RESULTS_ROOT/$TEST/report.log)"
status "=== Campaña $TEST terminada: reps ${REPS[*]} completas ==="
