#!/usr/bin/env bash
# Uso: run-test.sh <load|endurance|stress|smoke> <rep> [--force]
# Ejecuta k6 DENTRO de k6-runner-perf (nohup + pid + exit_code, resistente a caídas del websocket de kubectl exec)
# y copia los resultados a results/<test>/rep<N>/.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

FORCE=0
ARGS=()
for a in "$@"; do
  if [[ "$a" == "--force" ]]; then FORCE=1; else ARGS+=("$a"); fi
done
[[ ${#ARGS[@]} -eq 2 ]] || die "uso: $0 <load|endurance|stress|smoke> <rep> [--force]"
TEST="${ARGS[0]}"; REP="${ARGS[1]}"
validate_test_name "$TEST"; validate_rep "$REP"

# Fases (min) según 6.4 del plan: warm-up 5, medición, cooldown 5.
case "$TEST" in
  load)      MEASURE_MIN=60 ;;
  endurance) MEASURE_MIN=240 ;;
  stress)    MEASURE_MIN=30 ;;
  smoke)     MEASURE_MIN=0 ;;
esac
if [[ "$TEST" == smoke ]]; then WARM_MIN=0; COOL_MIN=0; else WARM_MIN=5; COOL_MIN=5; fi
TOTAL_SEC=$(( (WARM_MIN + MEASURE_MIN + COOL_MIN) * 60 ))
# Margen para A1 solapada con el cooldown y arranque de k6 (smoke: sin duración conocida)
MAX_WAIT=$(( TOTAL_SEC + ${EXTRA_WAIT_SEC:-900} ))
POLL="${POLL:-10}"
FETCH_RETRIES="${FETCH_RETRIES:-3}"

RDIR="/results/$TEST/rep$REP"
DIR="$RESULTS_ROOT/$TEST/rep$REP"
if [[ "$DRY_RUN" != 1 && -s "$DIR/k6.csv" && $FORCE -eq 0 ]]; then
  die "ya existe $DIR/k6.csv (use --force para sobrescribir)"
fi

require_running "$K6_POD" "$KIWI_POD" "$MARIADB_POD"
if [[ "$DRY_RUN" != 1 && $FORCE -eq 0 ]]; then
  if kexec "$K6_POD" test -s "$RDIR/k6.csv" 2>/dev/null; then
    die "ya existe $RDIR/k6.csv en el pod (use --force para sobrescribir)"
  fi
fi
DIR="$(result_dir "$TEST" "$REP")"
TIMES="$DIR/times.txt"

pod_kill_k6() {
  kexec "$K6_POD" sh -c "[ -f $RDIR/k6.pid ] && kill \$(cat $RDIR/k6.pid) 2>/dev/null; pkill -x k6 2>/dev/null; true" >/dev/null 2>&1 || true
}

tail_pid=""
cleanup_tail() {
  if [[ -n "$tail_pid" ]]; then kill "$tail_pid" 2>/dev/null || true; fi
  tail_pid=""
}

interrupted=0
on_int() {
  interrupted=1
  echo >&2
  warn "interrumpido: deteniendo k6 en $K6_POD"
  cleanup_tail
  pod_kill_k6
}
trap on_int INT TERM
trap cleanup_tail EXIT

START_EPOCH="$(date -u +%s)"
if [[ "$DRY_RUN" != 1 ]]; then
  {
    echo "test=$TEST"
    echo "rep=$REP"
    echo "start_utc=$(epoch_to_utc "$START_EPOCH")"
    echo "start_local=$(epoch_to_local "$START_EPOCH")"
    if (( TOTAL_SEC > 0 )); then
      t=$START_EPOCH
      for ph in "warmup_end:$WARM_MIN" "measure_end:$MEASURE_MIN" "cooldown_end:$COOL_MIN"; do
        t=$(( t + ${ph#*:} * 60 ))
        echo "expected_${ph%%:*}_utc=$(epoch_to_utc "$t")"
        echo "expected_${ph%%:*}_local=$(epoch_to_local "$t")"
      done
    fi
  } >"$TIMES"
fi
log "Inicio $TEST rep$REP: $(epoch_to_utc "$START_EPOCH") UTC / $(epoch_to_local "$START_EPOCH") local; duración esperada $((TOTAL_SEC / 60)) min"

K6_CMD="k6 run -e TEST_TYPE=$TEST -e REP=$REP --out csv=$RDIR/k6.csv --summary-export $RDIR/summary.json /scripts/main.js"
# k6 en segundo plano dentro del pod: su pid queda en k6.pid y su código de salida en exit_code
LAUNCH="mkdir -p $RDIR && cd $RDIR && rm -f exit_code k6.pid k6.csv summary.json k6.log && \
nohup sh -c '$K6_CMD > $RDIR/k6.log 2>&1 & echo \$! > $RDIR/k6.pid; wait \$!; echo \$? > $RDIR/exit_code' >/dev/null 2>&1 </dev/null &"
kexec "$K6_POD" sh -c "$LAUNCH"

if [[ "$DRY_RUN" == 1 ]]; then
  echo "[dry-run] seguimiento: tail -f $RDIR/k6.log con reconexión; espera de $RDIR/exit_code; luego copia" >&2
fi

follow_log() {
  local cpid="" off="+1"
  trap 'if [[ -n "$cpid" ]]; then kill "$cpid" 2>/dev/null || true; fi; exit 0' TERM INT
  while :; do
    "$KUBECTL" exec -n "$NS" "$K6_POD" -- tail -n "$off" -f "$RDIR/k6.log" 2>/dev/null &
    cpid=$!
    wait "$cpid" || true
    off=0
    sleep 3 &
    wait $! || true
  done
}

RC=""
if [[ "$DRY_RUN" != 1 ]]; then
  follow_log &
  tail_pid=$!
  waited=0; fails=0
  while (( ! interrupted )); do
    if out="$(timeout 30 "$KUBECTL" exec -n "$NS" "$K6_POD" -- sh -c \
        "if [ -f $RDIR/exit_code ]; then echo EXIT:\$(cat $RDIR/exit_code); elif kill -0 \$(cat $RDIR/k6.pid 2>/dev/null) 2>/dev/null; then echo RUNNING; else echo DEAD; fi" 2>/dev/null)"; then
      fails=0
      case "$out" in
        EXIT:*) RC="${out#EXIT:}"; break ;;
        DEAD)   warn "el proceso k6 ya no existe y no hay exit_code"; RC="dead"; break ;;
      esac
    else
      fails=$((fails + 1))
      if (( fails % 6 == 1 )); then warn "no se pudo consultar el pod (intento $fails)"; fi
    fi
    if (( waited >= MAX_WAIT )); then warn "tiempo máximo excedido ($MAX_WAIT s)"; pod_kill_k6; RC="timeout"; break; fi
    sleep "$POLL" & wait $! || true
    waited=$((waited + POLL))
  done
  cleanup_tail
  if (( interrupted )); then RC="interrupted"; fi
fi

END_EPOCH="$(date -u +%s)"
if [[ "$DRY_RUN" != 1 ]]; then
  {
    echo "end_utc=$(epoch_to_utc "$END_EPOCH")"
    echo "end_local=$(epoch_to_local "$END_EPOCH")"
    echo "k6_exit=${RC:-unknown}"
    echo "duration_sec=$(( END_EPOCH - START_EPOCH ))"
  } >>"$TIMES"
fi

# Copia pod -> local: stream con gzip (o cat) y verificación de tamaño/sha256; kubectl cp como alternativa.
fetch_file() {
  local f="$1" dest="$DIR/$1" attempt rsize rsum lsize lsum
  if [[ "$DRY_RUN" == 1 ]]; then echo "[dry-run] copiar $RDIR/$f -> $dest" >&2; return 0; fi
  rsize="$(kexec "$K6_POD" sh -c "wc -c < $RDIR/$f" 2>/dev/null | tr -d ' \r\n')" || rsize=""
  [[ -n "$rsize" ]] || { warn "$f no existe en el pod"; return 1; }
  rsum="$(kexec "$K6_POD" sh -c "sha256sum $RDIR/$f 2>/dev/null | cut -d' ' -f1" 2>/dev/null | tr -d ' \r\n')" || rsum=""
  for attempt in $(seq 1 "$FETCH_RETRIES"); do
    if kexec "$K6_POD" sh -c "command -v gzip >/dev/null 2>&1 || exit 3; gzip -c $RDIR/$f" >"$dest.gz" 2>/dev/null \
        && gzip -dc "$dest.gz" >"$dest" 2>/dev/null; then :
    elif kexec "$K6_POD" cat "$RDIR/$f" >"$dest" 2>/dev/null; then :
    elif kc cp -n "$NS" "$K6_POD:$RDIR/$f" "$dest" >/dev/null 2>&1; then :
    else sleep 3; continue; fi
    rm -f "$dest.gz"
    lsize="$(wc -c <"$dest" | tr -d ' ')"
    lsum="$(sha256sum "$dest" | cut -d' ' -f1)"
    if [[ "$lsize" == "$rsize" && ( -z "$rsum" || "$lsum" == "$rsum" ) ]]; then
      log "copiado $f ($lsize bytes${rsum:+, sha256 ok})"; return 0
    fi
    warn "verificación fallida para $f (intento $attempt: local $lsize vs pod $rsize)"
  done
  warn "no se pudo copiar $f"; return 1
}

copy_fail=0
for f in k6.log summary.json exit_code k6.csv; do
  fetch_file "$f" || copy_fail=1
done

echo
echo "=== Resumen $TEST rep$REP ==="
echo "k6 exit code : ${RC:-dry-run}"
echo "Inicio       : $(epoch_to_utc "$START_EPOCH") UTC  |  $(epoch_to_local "$START_EPOCH") local"
echo "Fin          : $(epoch_to_utc "$END_EPOCH") UTC  |  $(epoch_to_local "$END_EPOCH") local"
echo "Para Prometheus: scripts/collect-prometheus.sh $TEST $REP $(epoch_to_utc "$START_EPOCH") $(epoch_to_utc "$(( END_EPOCH + 60 ))")"
echo "Resultados   : $DIR"
if (( copy_fail )); then warn "algunos archivos no se copiaron; siguen en el pod en $RDIR"; fi
[[ "$RC" == "0" || "$DRY_RUN" == 1 ]] || exit 1
exit 0
