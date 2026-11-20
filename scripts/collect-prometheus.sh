#!/usr/bin/env bash
# Uso: collect-prometheus.sh <test> <rep> <inicio> <fin>   (ISO 8601 UTC o epoch)
# Exporta series de Prometheus (solo lectura) a results/<test>/rep<N>/prom_<nombre>.csv
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[[ $# -eq 4 ]] || die "uso: $0 <test> <rep> <inicio> <fin>"
TEST="$1"; REP="$2"
validate_test_name "$TEST"; validate_rep "$REP"
START="$(to_epoch "$3")"; END="$(to_epoch "$4")"
(( END > START )) || die "fin debe ser posterior a inicio"
STEP="${STEP:-15}"
MAX_POINTS=10000
CHUNK=$(( MAX_POINTS * STEP ))

DIR="$(result_dir "$TEST" "$REP")"
PODS='kiwi-perf-0|kiwi-perf-mariadb-0|kiwi-0|kiwi-mariadb-0'
SEL="namespace=\"kiwi\",pod=~\"$PODS\",container!=\"\",container!=\"POD\""
NODES="node_uname_info{nodename=~\"$NODE_REGEX\"}"

declare -A Q
Q[cpu]="sum by (pod,container) (rate(container_cpu_usage_seconds_total{$SEL}[1m]))"
Q[mem_working_set]="sum by (pod,container) (container_memory_working_set_bytes{$SEL})"
Q[cpu_throttling]="sum by (pod,container) (rate(container_cpu_cfs_throttled_periods_total{$SEL}[1m])) / sum by (pod,container) (rate(container_cpu_cfs_periods_total{$SEL}[1m]))"
Q[restarts]="sum by (pod,container) (kube_pod_container_status_restarts_total{$SEL})"
Q[log_fs_used]="sum by (pod,container) (kubelet_container_log_filesystem_used_bytes{namespace=\"kiwi\",pod=~\"$PODS\"})"
Q[node_mem_pct]="max by (nodename) ((100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * on(instance) group_left(nodename) $NODES)"
Q[node_cpu_pct]="max by (nodename) ((100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode=\"idle\"}[1m])))) * on(instance) group_left(nodename) $NODES)"

log "Exportando $(epoch_to_utc "$START") -> $(epoch_to_utc "$END") (UTC) a $DIR"
fail=0
for name in cpu mem_working_set cpu_throttling restarts log_fs_used node_mem_pct node_cpu_pct; do
  out="$DIR/prom_$name.csv"
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "[dry-run] query_range $name: ${Q[$name]}" >&2
    continue
  fi
  echo "timestamp,series,value" >"$out"
  t="$START"
  while (( t < END )); do
    e=$(( t + CHUNK )); (( e > END )) && e="$END"
    if ! prom_get query_range --data-urlencode "query=${Q[$name]}" \
        --data-urlencode "start=$t" --data-urlencode "end=$e" \
        --data-urlencode "step=$STEP" | prom_json_to_csv >>"$out"; then
      warn "falló la consulta $name ($t..$e)"; fail=1
    fi
    t=$(( e + STEP ))
  done
  rows=$(( $(wc -l <"$out") - 1 ))
  if (( rows <= 0 )); then warn "prom_$name.csv sin datos"; else log "prom_$name.csv: $rows filas"; fi
done

[[ "$DRY_RUN" == 1 ]] && exit 0

# Pico de memoria de nodo (último valor de cada serie por máximo)
{
  echo "# node memory peak (%), ventana $(epoch_to_utc "$START") .. $(epoch_to_utc "$END") UTC"
  awk -F, 'NR>1 && ($3+0)>=m[$2] {m[$2]=$3+0; at[$2]=$1}
           END {for (s in m) printf "node_mem_peak_pct %s %.2f at %s\n", s, m[s], at[s]}' "$DIR/prom_node_mem_pct.csv" | sort
} >"$DIR/prom_summary.txt"
cat "$DIR/prom_summary.txt"
exit "$fail"
