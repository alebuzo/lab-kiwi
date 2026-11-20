#!/usr/bin/env bash
# Despliega la réplica en orden. Uso: deploy.sh [--dry-run]
# Requiere que existan los secrets (scripts/seed-secrets.sh). Solo toca recursos con sufijo -perf.
set -euo pipefail

NS=kiwi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K8S="$ROOT/k8s"
DRY=()
[[ "${1:-}" == "--dry-run" ]] && DRY=(--dry-run=server)

guard() {
  # Todos los recursos de un manifiesto deben terminar en -perf (o k6-*-perf) y llevar part-of=kiwi-perf.
  python3 - "$@" <<'PY'
import json, subprocess, sys
for f in sys.argv[1:]:
    out = subprocess.check_output(["kubectl", "apply", "--dry-run=client", "-f", f, "-o", "json"])
    d = json.loads(out)
    for o in (d["items"] if d["kind"] == "List" else [d]):
        n = o["metadata"]["name"]
        if "perf" not in n or o["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") != "kiwi-perf":
            sys.exit("Rechazado: %s/%s no tiene sufijo -perf o etiqueta part-of" % (o["kind"], n))
PY
}

echo "== Validando manifiestos"
guard "$K8S/10-mariadb-perf.yaml" "$K8S/20-kiwi-perf.yaml" "$K8S/30-k6-runner.yaml"

echo "== Verificando secrets"
for s in kiwi-perf-credentials k6-perf-credentials; do
  kubectl get secret "$s" -n "$NS" >/dev/null 2>&1 || { echo "Falta secret/$s: ejecute scripts/seed-secrets.sh" >&2; exit 1; }
done
kubectl get secret kiwi-internal-certs -n "$NS" >/dev/null

echo "== MariaDB"
kubectl apply -n "$NS" "${DRY[@]}" -f "$K8S/10-mariadb-perf.yaml"
if [[ ${#DRY[@]} -eq 0 ]]; then
  kubectl rollout status sts/kiwi-perf-mariadb -n "$NS" --timeout=600s
fi

echo "== Kiwi"
kubectl apply -n "$NS" "${DRY[@]}" -f "$K8S/20-kiwi-perf.yaml"
if [[ ${#DRY[@]} -eq 0 ]]; then
  kubectl rollout status sts/kiwi-perf -n "$NS" --timeout=900s
fi

echo "== ConfigMap de scripts k6 (tar.gz de k6/)"
[[ -d "$ROOT/k6" ]] || { echo "No existe $ROOT/k6" >&2; exit 1; }
SUM=$(tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -C "$ROOT/k6" -cf - . | gzip -n | sha256sum | cut -c1-64)
cm_yaml() {
  local b64
  b64=$(tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -C "$ROOT/k6" -cf - . | gzip -n | base64 -w0)
  cat <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: k6-scripts-perf
  namespace: $NS
  labels:
    app.kubernetes.io/part-of: kiwi-perf
  annotations:
    kiwi-perf/scripts-sha256: "$SUM"
binaryData:
  scripts.tar.gz: $b64
YAML
}
cm_yaml | kubectl apply "${DRY[@]}" -f -

echo "== k6-runner"
if [[ ${#DRY[@]} -eq 0 ]]; then
  cur=$(kubectl get pod k6-runner-perf -n "$NS" -o jsonpath='{.metadata.annotations.kiwi-perf/scripts-sha256}' 2>/dev/null || true)
  if [[ -n "$cur" && "$cur" != "$SUM" ]]; then
    echo "Scripts cambiaron: recreando pod k6-runner-perf"
    kubectl delete pod k6-runner-perf -n "$NS" --wait=true
  fi
fi
kubectl apply -n "$NS" "${DRY[@]}" -f "$K8S/30-k6-runner.yaml"
if [[ ${#DRY[@]} -eq 0 ]]; then
  kubectl annotate pod k6-runner-perf -n "$NS" --overwrite "kiwi-perf/scripts-sha256=$SUM" >/dev/null
  kubectl wait --for=condition=Ready pod/k6-runner-perf -n "$NS" --timeout=300s
  kubectl get pods -n "$NS" -l app.kubernetes.io/part-of=kiwi-perf -o wide
fi
