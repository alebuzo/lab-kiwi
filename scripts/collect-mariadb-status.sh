#!/usr/bin/env bash
# Uso: collect-mariadb-status.sh <test> <rep>   (INTERVAL=45 por defecto; DRY_RUN=1 hace una sola iteración)
# SHOW GLOBAL STATUS de kiwi-perf-mariadb-0; la contraseña root viene del entorno del contenedor.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[[ $# -eq 2 ]] || die "uso: $0 <test> <rep>"
TEST="$1"; REP="$2"
validate_test_name "$TEST"; validate_rep "$REP"
INTERVAL="${INTERVAL:-45}"
EXEC_TIMEOUT="${EXEC_TIMEOUT:-30}"
DIR="$(result_dir "$TEST" "$REP")"
require_running "$MARIADB_POD"

stop=0
trap 'stop=1; log "señal recibida, terminando..."' INT TERM

ok=0; err=0
log "Recolectando SHOW GLOBAL STATUS de $MARIADB_POD cada ${INTERVAL}s en $DIR (Ctrl-C para detener)"
while (( ! stop )); do
  t0=$SECONDS
  ts="$(ts_compact)"
  tmp="$DIR/.mariadb_status_$ts.part"
  if [[ "$DRY_RUN" == 1 ]]; then
    kexec "$MARIADB_POD" sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -e "SHOW GLOBAL STATUS;"'
    log "dry-run: archivo destino $DIR/mariadb_status_$ts.txt"
    break
  fi
  if timeout "$EXEC_TIMEOUT" "$KUBECTL" exec -n "$NS" "$MARIADB_POD" -- sh -c \
      'c=$(command -v mariadb || command -v mysql); "$c" -uroot -p"${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}" -e "SHOW GLOBAL STATUS;"' \
      >"$tmp" 2>/dev/null && grep -q '^Uptime' "$tmp"; then
    mv "$tmp" "$DIR/mariadb_status_$ts.txt"
    ok=$((ok + 1))
  else
    rm -f "$tmp"
    err=$((err + 1))
    warn "fallo transitorio al leer estado de MariaDB (errores acumulados: $err)"
  fi
  log "latido: ok=$ok errores=$err último=$ts"
  (( stop )) && break
  left=$(( INTERVAL - (SECONDS - t0) ))
  (( left > 0 )) && isleep "$left"
done
log "Fin: ok=$ok errores=$err"
