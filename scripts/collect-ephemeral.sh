#!/usr/bin/env bash
# Uso: collect-ephemeral.sh <test> <rep>   (INTERVAL=45; DRY_RUN=1 una sola iteración)
# du -sk /tmp y du -sxk / de kiwi-perf-0 y kiwi-perf-mariadb-0 -> ephemeral.csv
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[[ $# -eq 2 ]] || die "uso: $0 <test> <rep>"
TEST="$1"; REP="$2"
validate_test_name "$TEST"; validate_rep "$REP"
INTERVAL="${INTERVAL:-45}"
EXEC_TIMEOUT="${EXEC_TIMEOUT:-30}"
DIR="$(result_dir "$TEST" "$REP")"
CSV="$DIR/ephemeral.csv"
PODS=("$KIWI_POD" "$MARIADB_POD")
require_running "${PODS[@]}"

# Salida del pod: "<tmp_kb> <rootfs_du_kb> <método_tmp> <método_rootfs>"
REMOTE='
tmp=""; tm=du
if command -v du >/dev/null 2>&1; then tmp=$(du -sk /tmp 2>/dev/null | cut -f1); fi
if [ -z "$tmp" ]; then
  tm=find-stat
  tmp=$(find /tmp -type f -exec stat -c %s {} + 2>/dev/null | awk "{s+=\$1} END {printf \"%d\", s/1024}")
fi
[ -n "$tmp" ] || { tmp=NA; tm=none; }
rt=""; rm=du
# du -x no cruza montajes (excluye volúmenes como /var/lib/mysql); df / mediría el disco compartido del nodo
if command -v du >/dev/null 2>&1; then rt=$(du -sxk / 2>/dev/null | cut -f1); fi
[ -n "$rt" ] || { rt=NA; rm=none; }
echo "$tmp $rt $tm $rm"
'

stop=0
trap 'stop=1; log "señal recibida, terminando..."' INT TERM
[[ "$DRY_RUN" == 1 || -s "$CSV" ]] || echo "timestamp_utc,pod,tmp_kb,rootfs_du_kb" >"$CSV"

declare -A seen_method
ok=0; err=0
log "Recolectando almacenamiento efímero de ${PODS[*]} cada ${INTERVAL}s en $CSV"
while (( ! stop )); do
  t0=$SECONDS
  for pod in "${PODS[@]}"; do
    ts="$(ts_utc)"
    if [[ "$DRY_RUN" == 1 ]]; then
      kexec "$pod" sh -c "$REMOTE"; continue
    fi
    if out="$(timeout "$EXEC_TIMEOUT" "$KUBECTL" exec -n "$NS" "$pod" -- sh -c "$REMOTE" 2>/dev/null)" \
        && read -r tmp_kb root_kb tm rm_ <<<"$out" && [[ -n "${root_kb:-}" ]]; then
      echo "$ts,$pod,$tmp_kb,$root_kb" >>"$CSV"
      ok=$((ok + 1))
      if [[ -z "${seen_method[$pod]:-}" ]]; then
        seen_method[$pod]=1
        log "$pod: método /tmp=$tm, rootfs=$rm_"
        [[ "$tm" == du && "$rm_" == du ]] || warn "$pod: se usó método alterno (/tmp=$tm, rootfs=$rm_)"
      fi
    else
      err=$((err + 1))
      warn "fallo transitorio en $pod (errores acumulados: $err)"
    fi
  done
  log "latido: ok=$ok errores=$err"
  [[ "$DRY_RUN" == 1 ]] && break
  (( stop )) && break
  left=$(( INTERVAL - (SECONDS - t0) ))
  (( left > 0 )) && isleep "$left"
done
log "Fin: ok=$ok errores=$err"
