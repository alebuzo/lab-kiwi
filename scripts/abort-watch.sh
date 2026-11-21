#!/usr/bin/env bash
# Uso: abort-watch.sh   (INTERVAL=30, MAX_ITER=0 = infinito, MEM_THRESHOLD=30)
# Por defecto solo lectura: sale con código 2 si un pod de producción se reinicia.
# Aborto automático (desatendido): AUTO_ABORT_TEST=<prueba> AUTO_ABORT_REP=<N>. Ante A-1 (memoria de un nodo >= MEM_THRESHOLD)
# o A-2 (pod productivo reiniciado/desalojado/OOM) detiene k6 en k6-runner-perf, escribe results/<prueba>/rep<N>/ABORTED.txt y sale con 2.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

INTERVAL="${INTERVAL:-30}"
MAX_ITER="${MAX_ITER:-0}"
MEM_THRESHOLD="${MEM_THRESHOLD:-30}"
WATCH_PODS=("${PROD_PODS[@]}" "$KIWI_POD" "$MARIADB_POD" "$K6_POD")
PY_PODS='
import sys, json
want = sys.argv[1:]
items = {p["metadata"]["name"]: p for p in json.load(sys.stdin)["items"]}
for n in want:
    p = items.get(n)
    if p is None:
        print("%s\tMISSING\t-1\t-" % n); continue
    st = p.get("status", {})
    cs = st.get("containerStatuses", [])
    fl = set()
    if st.get("reason") == "Evicted": fl.add("Evicted")
    for c in cs:
        for k in ("lastState", "state"):
            t = c.get(k, {}).get("terminated")
            if t and t.get("reason") == "OOMKilled": fl.add("OOMKilled")
    print("%s\t%s\t%d\t%s" % (n, st.get("phase"), sum(c.get("restartCount", 0) for c in cs), ",".join(sorted(fl)) or "-"))
'
QMEM="max by (nodename) ((100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * on(instance) group_left(nodename) node_uname_info{nodename=~\"$NODE_REGEX\"})"

pods_state() { kread get pods -o json | python3 -c "$PY_PODS" "${WATCH_PODS[@]}"; }

mem_state() {
  prom_get query --data-urlencode "query=$QMEM" | python3 -c '
import sys, json
d = json.load(sys.stdin)
if d.get("status") != "success": sys.exit(1)
for s in d["data"]["result"]:
    print("%s\t%.1f" % (s["metric"].get("nodename", "?"), float(s["value"][1])))
'
}

if [[ "$DRY_RUN" == 1 ]]; then
  kread get pods -o json
  echo "[dry-run] query Prometheus: $QMEM" >&2
  exit 0
fi

declare -A base
state="$(pods_state)" || die "no se pudo leer el estado de los pods"
while IFS=$'\t' read -r n _ r _; do base[$n]="$r"; done <<<"$state"
log "Baseline de reinicios: $(for p in "${PROD_PODS[@]}"; do printf '%s=%s ' "$p" "${base[$p]:--1}"; done)"

is_prod() { local p; for p in "${PROD_PODS[@]}"; do [[ "$p" == "$1" ]] && return 0; done; return 1; }

auto_abort() {
  [[ -n "${AUTO_ABORT_TEST:-}" && -n "${AUTO_ABORT_REP:-}" ]] || return 0
  local rdir="/results/$AUTO_ABORT_TEST/rep$AUTO_ABORT_REP" d
  d="$(result_dir "$AUTO_ABORT_TEST" "$AUTO_ABORT_REP")"
  kexec "$K6_POD" sh -c "[ -f $rdir/k6.pid ] && kill \$(cat $rdir/k6.pid) 2>/dev/null; pkill -x k6 2>/dev/null; true" >/dev/null 2>&1 || true
  { echo "abort_utc=$(ts_utc)"; echo "abort_local=$(ts_local)"; echo "motivo=$1"; echo "linea=$line"; } >"$d/ABORTED.txt"
}

i=0
while :; do
  i=$((i + 1))
  line="$(ts_utc) (local $(ts_local))"
  alert=0; abort=0; reason=""
  if state="$(pods_state 2>/dev/null)"; then
    while IFS=$'\t' read -r n phase r fl; do
      s="$n:$phase/r=$r"
      [[ "$fl" == "-" ]] || s+="/$fl"
      bad=0
      if is_prod "$n"; then
        [[ "$r" == "${base[$n]:-$r}" ]] || { bad=1; abort=1; reason="A-2: $n cambió su restartCount"; }
        [[ "$fl" == "-" ]] || { bad=1; abort=1; reason="A-2: $n marcado $fl"; }
      fi
      [[ "$phase" == Running || "$n" == "$K6_POD" ]] || bad=1
      [[ "$fl" == "-" ]] || bad=1
      (( bad )) && { s="${RED}${s} <!!>${RST}"; alert=1; }
      line+=" | $s"
    done <<<"$state"
  else
    line+=" | ${RED}error leyendo pods${RST}"
  fi
  if mem="$(mem_state 2>/dev/null)"; then
    while IFS=$'\t' read -r node pct; do
      s="$node mem=${pct}%"
      if awk -v v="$pct" -v t="$MEM_THRESHOLD" 'BEGIN{exit !(v>=t)}'; then s="${RED}${s} >= ${MEM_THRESHOLD}% <!!>${RST}"; alert=1; abort=1; reason="A-1: $node mem=${pct}% >= ${MEM_THRESHOLD}%"; fi
      line+=" | $s"
    done <<<"$mem"
  else
    line+=" | mem nodo: n/d"
  fi
  echo "$line"
  if (( abort )); then
    echo "${RED}############################################################" >&2
    echo "# ABORTAR: $reason" >&2
    echo "# Detener run-test.sh (Ctrl-C) y avisar al usuario             #" >&2
    echo "############################################################${RST}" >&2
    auto_abort "$reason"
    exit 2
  fi
  (( MAX_ITER > 0 && i >= MAX_ITER )) && break
  isleep "$INTERVAL"
done
