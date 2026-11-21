#!/usr/bin/env bash
# Reinicia la BD de la réplica clonando el PVC golden (mariadb-golden-perf).
# Solo toca recursos -perf. Uso: reset-db.sh [--expect-cases N] [--expect-tables N]
# No cuenta filas de testruns_historicaltestexecution: con caché fría tarda >1 min y calentaría la caché de MariaDB.
# Imprime una fila lista para la Tabla E3 del plan de ejecución.
set -euo pipefail

NS=kiwi
GOLDEN=mariadb-golden-perf
WORK_PVC=mariadb-data-kiwi-perf-mariadb-0
STS_APP=kiwi-perf
STS_DB=kiwi-perf-mariadb
POD_APP=kiwi-perf-0
POD_DB=kiwi-perf-mariadb-0
EXPECT_TABLES=71
EXPECT_CASES=11492
EXPECT_USERS=18

while [[ $# -gt 0 ]]; do
  case "$1" in
    --expect-cases) EXPECT_CASES="$2"; shift ;;
    --expect-tables) EXPECT_TABLES="$2"; shift ;;
    *) echo "Opción desconocida: $1" >&2; exit 2 ;;
  esac
  shift
done

ts() { date +%H:%M:%S; }
die() { echo "ERROR: $*" >&2; exit 1; }
sql() { kubectl exec -n "$NS" "$POD_DB" -- sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -N -B kiwi -e "$1"' sh "$1"; }

[[ "$(kubectl get pvc "$GOLDEN" -n "$NS" -o jsonpath='{.status.phase}')" == "Bound" ]] || die "$GOLDEN no existe o no está Bound"

START_H=$(ts); T0=$SECONDS
echo "[$START_H] Escalando $STS_APP y $STS_DB a 0"
kubectl scale "sts/$STS_APP" "sts/$STS_DB" -n "$NS" --replicas=0 >/dev/null
kubectl wait --for=delete "pod/$POD_APP" "pod/$POD_DB" -n "$NS" --timeout=300s >/dev/null 2>&1 || true

echo "[$(ts)] Reemplazando $WORK_PVC por un clon de $GOLDEN"
kubectl delete pvc "$WORK_PVC" -n "$NS" --wait=true --timeout=180s >/dev/null
kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $WORK_PVC
  namespace: $NS
  labels:
    app.kubernetes.io/name: kiwi-perf-mariadb
    app.kubernetes.io/instance: kiwi-perf
    app.kubernetes.io/part-of: kiwi-perf
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: hpe-standard
  resources: {requests: {storage: 32Gi}}
  dataSource: {kind: PersistentVolumeClaim, name: $GOLDEN}
EOF

kubectl scale "sts/$STS_DB" -n "$NS" --replicas=1 >/dev/null
kubectl wait --for=condition=Ready "pod/$POD_DB" -n "$NS" --timeout=900s >/dev/null
echo "[$(ts)] MariaDB Ready ($((SECONDS - T0)) s)"
kubectl scale "sts/$STS_APP" -n "$NS" --replicas=1 >/dev/null
kubectl wait --for=condition=Ready "pod/$POD_APP" -n "$NS" --timeout=900s >/dev/null
DUR=$((SECONDS - T0)); END_H=$(ts)
echo "[$END_H] Kiwi Ready (total $DUR s)"

tables=$(sql "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='kiwi' AND table_type='BASE TABLE'")
cases=$(sql "SELECT COUNT(*) FROM testcases_testcase")
users=$(sql "SELECT COUNT(*) FROM auth_user WHERE username LIKE 'perf_user_%'")
echo "tablas=$tables (esperado $EXPECT_TABLES) casos=$cases (esperado $EXPECT_CASES) perf_users=$users (esperado $EXPECT_USERS)"
if [[ "$tables" -ne "$EXPECT_TABLES" || "$cases" -ne "$EXPECT_CASES" || "$users" -ne "$EXPECT_USERS" ]]; then
  die "VERIFICACIÓN FALLIDA"
fi
echo "Verificación OK"
echo "E3: clonado PVC | inicio $START_H | fin $END_H | ${DUR} s | $cases casos / $tables tablas"
