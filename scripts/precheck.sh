#!/usr/bin/env bash
# Prechequeos de solo lectura antes de desplegar/ejecutar. No modifica nada en el cluster.
set -uo pipefail

NS=kiwi
EXPECT_NODE=asic-x86-02
KIWI_IMG_DIGEST=sha256:162e4cb8fd3224e0772de88e83c2636cd18def14806669263c59abff3a103e7e
MARIADB_IMG_DIGEST=sha256:e101f9db31916a5d4d7d594dd0dd092fb23ab4f499f1d7a7425d1afd4162c4bc
WARN=0; FAIL=0
ok()   { echo "  [OK]   $*"; }
warn() { echo "  [WARN] $*"; WARN=$((WARN + 1)); }
bad()  { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }

echo "== 1. Pods de producción (sin sufijo perf)"
while IFS='|' read -r name phase ready restarts node; do
  [[ "$phase" == Running && "$ready" == True ]] && s=ok || s=bad
  $s "$name phase=$phase ready=$ready reinicios=$restarts nodo=$node"
  [[ "$node" == "$EXPECT_NODE" ]] || warn "$name no está en $EXPECT_NODE"
done < <(kubectl get pods -n "$NS" -o json | python3 -c '
import json, sys
for p in json.load(sys.stdin)["items"]:
    n = p["metadata"]["name"]
    if "perf" in n or n.startswith("k6-"):
        continue
    st = p["status"]; phase = st.get("phase")
    if phase == "Succeeded":
        continue
    cs = st.get("containerStatuses", [])
    restarts = sum(c["restartCount"] for c in cs)
    ready = all(c.get("ready") for c in cs) if cs else False
    print("%s|%s|%s|%d|%s" % (n, phase, ready, restarts, p["spec"].get("nodeName")))
')

echo "== 2. Requests de CPU/memoria de los pods en $NS"
kubectl get pods -n "$NS" -o json | python3 -c '
import json, sys
print("  %-40s %-14s %-8s %s" % ("pod", "nodo", "cpu_req", "mem_req"))
for p in json.load(sys.stdin)["items"]:
    if p["status"].get("phase") in ("Succeeded", "Failed"):
        continue
    cpu, mem = [], []
    for c in p["spec"]["containers"]:
        r = c.get("resources", {}).get("requests", {})
        cpu.append(r.get("cpu", "-")); mem.append(r.get("memory", "-"))
    print("  %-40s %-14s %-8s %s" % (p["metadata"]["name"], p["spec"].get("nodeName", "-"), "+".join(cpu), "+".join(mem)))
'

echo "== 3. Imágenes y digests de producción vs plan (sección 1)"
check_digest() {
  local sts="$1" want="$2" img got
  img=$(kubectl get sts "$sts" -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
  [[ -n "$img" ]] || { bad "sts/$sts no encontrado"; return; }
  got=$(kubectl get pod "$sts-0" -n "$NS" -o jsonpath='{.status.containerStatuses[0].imageID}' 2>/dev/null)
  if [[ "$got" == *"$want"* ]]; then ok "$sts $img digest = plan"
  else warn "$sts $img: digest del pod distinto del plan (deriva: ${got##*@})"; fi
}
check_digest kiwi "$KIWI_IMG_DIGEST"
check_digest kiwi-mariadb "$MARIADB_IMG_DIGEST"

echo "== 4. Recursos de la réplica (no deben colisionar)"
for spec in sts/kiwi-perf sts/kiwi-perf-mariadb svc/kiwi-perf-http svc/kiwi-perf-https svc/kiwi-perf-mariadb \
            pvc/kiwi-perf-media pvc/mariadb-data-kiwi-perf-mariadb-0 pvc/k6-results-perf pod/k6-runner-perf \
            secret/kiwi-perf-credentials secret/k6-perf-credentials configmap/k6-scripts-perf; do
  if kubectl get "$spec" -n "$NS" >/dev/null 2>&1; then
    lbl=$(kubectl get "$spec" -n "$NS" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/part-of}')
    [[ "$lbl" == kiwi-perf ]] && echo "  [INFO] $spec ya existe (de la réplica)" || bad "$spec existe SIN etiqueta part-of=kiwi-perf (colisión)"
  fi
done
echo "  Servicios de producción deben seguir apuntando solo a pods de producción:"
for s in kiwi-http kiwi-https kiwi-mariadb; do
  eps=$(kubectl get endpoints "$s" -n "$NS" -o jsonpath='{range .subsets[*].addresses[*]}{.targetRef.name} {end}' 2>/dev/null)
  [[ "$eps" == *perf* ]] && bad "svc/$s tiene endpoints de la réplica: $eps" || ok "svc/$s -> ${eps:-(sin endpoints)}"
done

echo "== 5. Capacidad del nodo"
echo "  No se pueden listar nodos ni usar 'kubectl top' con esta cuenta; revisar memoria/CPU del nodo"
echo "  en el dashboard B de Grafana (umbral de aborto 30 % de memoria usada)."

echo
echo "Resultado: $FAIL fallos, $WARN advertencias"
[[ $FAIL -eq 0 ]]
