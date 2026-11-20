#!/usr/bin/env bash
# Crea los secrets de la réplica (solo si no existen). Nunca imprime valores.
set -euo pipefail

NS=kiwi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="${TCMS_CONF:-$ROOT/tcms.conf}"

conf_get() {
  awk -F= -v k="$1" '
    /^\[/ {next}
    { key=$1; gsub(/^[ \t]+|[ \t]+$/, "", key)
      if (key==k) { v=substr($0, index($0,"=")+1); gsub(/^[ \t]+|[ \t\r]+$/, "", v); print v; exit } }' "$CONF"
}

rand() { head -c 96 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-"${1:-32}"; }

# El JSON se arma en memoria y se envía por stdin (los valores no aparecen en argv).
create_secret() {
  local name="$1"; shift
  python3 -c '
import json, os, sys
name = sys.argv[1]
data = {k: os.environ[e] for k, e in (a.split("=", 1) for a in sys.argv[2:])}
print(json.dumps({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
  "metadata": {"name": name, "namespace": "kiwi", "labels": {"app.kubernetes.io/part-of": "kiwi-perf"}},
  "stringData": data}))' "$name" "$@" | kubectl create -n "$NS" -f - >/dev/null
  echo "secret/$name creado"
}

exists() { kubectl get secret "$1" -n "$NS" >/dev/null 2>&1; }

if exists kiwi-perf-credentials; then
  echo "secret/kiwi-perf-credentials ya existe, se omite"
else
  export V_SECRET_KEY V_ROOT_PW V_DB_USER V_DB_PW
  V_SECRET_KEY="$(rand 50)"; V_ROOT_PW="$(rand 32)"; V_DB_USER=kiwi; V_DB_PW="$(rand 32)"
  create_secret kiwi-perf-credentials secret_key=V_SECRET_KEY mariadb-root-password=V_ROOT_PW \
    mariadb-username=V_DB_USER mariadb-password=V_DB_PW
fi

if exists k6-perf-credentials; then
  echo "secret/k6-perf-credentials ya existe, se omite"
else
  [[ -r "$CONF" ]] || { echo "No se puede leer $CONF" >&2; exit 1; }
  export V_MK_USER V_MK_PW V_PERF_PW
  V_MK_USER="$(conf_get username)"; V_MK_PW="$(conf_get password)"; V_PERF_PW="$(rand 32)"
  [[ -n "$V_MK_USER" && -n "$V_MK_PW" ]] || { echo "tcms.conf sin username/password" >&2; exit 1; }
  create_secret k6-perf-credentials makise-username=V_MK_USER makise-password=V_MK_PW \
    perf-user-password=V_PERF_PW
fi
