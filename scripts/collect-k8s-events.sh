#!/usr/bin/env bash
# Uso: collect-k8s-events.sh <test> <rep> <start|end>
# Solo lectura (get/describe/events) sobre pods de la réplica y de producción.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[[ $# -eq 3 ]] || die "uso: $0 <test> <rep> <start|end>"
TEST="$1"; REP="$2"; PHASE="$3"
validate_test_name "$TEST"; validate_rep "$REP"
[[ "$PHASE" == start || "$PHASE" == end ]] || die "fase debe ser start|end"
DIR="$(result_dir "$TEST" "$REP")"

log "Eventos ($PHASE) -> $DIR/events_$PHASE.json"
if [[ "$DRY_RUN" == 1 ]]; then
  kread get events --sort-by=.lastTimestamp -o json
else
  kread get events --sort-by=.lastTimestamp -o json >"$DIR/events_$PHASE.json"
fi

[[ "$PHASE" == end ]] || exit 0

ALL_PODS=("$KIWI_POD" "$MARIADB_POD" "$K6_POD" "${PROD_PODS[@]}")
if [[ "$DRY_RUN" == 1 ]]; then
  kread get pods -o wide
  for p in "${ALL_PODS[@]}"; do kread describe pod "$p"; done
  exit 0
fi

kread get pods -o wide >"$DIR/pods_end.txt"
for p in "${ALL_PODS[@]}"; do
  kread describe pod "$p" >"$DIR/describe_$p.txt" 2>&1 || warn "no se pudo describir $p"
done

# Resumen de reinicios / OOMKilled / Evicted
kread get pods -o json | python3 -c '
import sys, json
pods = json.load(sys.stdin)["items"]
print("pod\tphase\trestarts\tlast_terminated_reason\tevicted\toomkilled")
bad = 0
for p in sorted(pods, key=lambda x: x["metadata"]["name"]):
    n = p["metadata"]["name"]
    st = p.get("status", {})
    if "backup" in n:
        continue
    cs = st.get("containerStatuses", [])
    restarts = sum(c.get("restartCount", 0) for c in cs)
    reasons = set()
    for c in cs:
        for k in ("lastState", "state"):
            t = c.get(k, {}).get("terminated")
            if t and t.get("reason"):
                reasons.add(t["reason"])
    ev = st.get("reason") == "Evicted"
    oom = "OOMKilled" in reasons
    if restarts or ev or oom:
        bad += 1
    print("%s\t%s\t%d\t%s\t%s\t%s" % (n, st.get("phase"), restarts, ",".join(sorted(reasons)) or "-", ev, oom))
print("# pods con reinicios/evicted/oomkilled: %d" % bad)
' | tee "$DIR/restarts_summary.txt"
log "Archivos escritos en $DIR"
