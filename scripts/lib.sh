#!/usr/bin/env bash
# Helpers compartidos (se usa con `source`). No ejecutar directamente.

NS="${NS:-kiwi}"
KUBECTL="${KUBECTL:-kubectl}"
DRY_RUN="${DRY_RUN:-0}"

KIWI_POD="${KIWI_POD:-kiwi-perf-0}"
MARIADB_POD="${MARIADB_POD:-kiwi-perf-mariadb-0}"
K6_POD="${K6_POD:-k6-runner-perf}"
PROD_PODS=(kiwi-0 kiwi-mariadb-0)

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$LIB_DIR/.." && pwd)"
RESULTS_ROOT="${RESULTS_ROOT:-$REPO_ROOT/results}"

PROM_URL="${PROM_URL:-https://pvsdk-metrics.rose.rdlabs.hpecorp.net/api/datasources/proxy/uid/prometheus/api/v1}"
NODE_REGEX="${NODE_REGEX:-asic-x86-0[12]}"

if [[ -t 2 ]]; then
  RED=$'\033[1;31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
else
  RED=""; GRN=""; YLW=""; RST=""
fi

ts_utc()      { date -u +%Y-%m-%dT%H:%M:%SZ; }
ts_compact()  { date -u +%Y%m%dT%H%M%SZ; }
ts_local()    { TZ=Etc/GMT+6 date +%Y-%m-%dT%H:%M:%S-06:00; }
epoch_to_utc()   { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }
epoch_to_local() { TZ=Etc/GMT+6 date -d "@$1" +%Y-%m-%dT%H:%M:%S-06:00; }

log()  { echo "[$(ts_utc)] $*" >&2; }
warn() { echo "${YLW}[$(ts_utc)] AVISO: $*${RST}" >&2; }
die()  { echo "${RED}[$(ts_utc)] ERROR: $*${RST}" >&2; exit 1; }

# Acepta ISO 8601 o epoch; imprime epoch.
to_epoch() {
  if [[ "$1" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    echo "${1%.*}"
  else
    date -u -d "$1" +%s 2>/dev/null || die "fecha inválida: $1"
  fi
}

validate_test_name() {
  case "$1" in
    load | endurance | stress | smoke) ;;
    *) die "prueba inválida '$1' (load|endurance|stress|smoke)" ;;
  esac
}

validate_rep() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]] || die "repetición inválida '$1' (entero >= 1)"
}

result_dir() {
  local d="$RESULTS_ROOT/$1/rep$2"
  [[ "$DRY_RUN" == 1 ]] || mkdir -p "$d"
  echo "$d"
}

is_perf_pod() { [[ "$1" =~ ^[a-z0-9-]*perf[a-z0-9-]*$ ]]; }

# Ejecuta kubectl o lo imprime en DRY_RUN.
kc() {
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "[dry-run] $KUBECTL $*" >&2
    return 0
  fi
  "$KUBECTL" "$@"
}

# Solo lectura: permitido sobre cualquier pod (producción incluida).
kread() {
  case "${1:-}" in
    get | describe | events) ;;
    *) die "kread solo permite get/describe/events (recibido '${1:-}')" ;;
  esac
  kc "$@" -n "$NS"
}

# exec: SOLO en pods de la réplica (-perf). Uso: kexec <pod> <cmd...>
kexec() {
  local pod="$1"
  shift
  is_perf_pod "$pod" || die "se rechaza exec en pod no -perf: '$pod'"
  kc exec -n "$NS" "$pod" -- "$@"
}

# Verifica que los pods dados existan y estén Running.
require_running() {
  local p phase
  [[ "$DRY_RUN" == 1 ]] && return 0
  for p in "$@"; do
    phase="$("$KUBECTL" get pod -n "$NS" "$p" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    [[ "$phase" == "Running" ]] || die "pod $p no está Running (fase: ${phase:-inexistente})"
  done
}

# Espera interrumpible por señales.
isleep() {
  sleep "$1" &
  wait $! 2>/dev/null || true
}

# --- Prometheus (Grafana proxy), solo lectura ---
load_token() {
  if [[ -z "${GRAFANA_TOKEN:-}" ]]; then
    [[ -r "$REPO_ROOT/GRAFANA_TOKEN" ]] || die "falta GRAFANA_TOKEN (variable o archivo en la raíz del repo)"
    GRAFANA_TOKEN="$(tr -d ' \r\n' <"$REPO_ROOT/GRAFANA_TOKEN")"
  fi
  [[ -n "$GRAFANA_TOKEN" ]] || die "GRAFANA_TOKEN vacío"
}

# prom_get <endpoint> [--data-urlencode k=v ...]; el token no aparece en la línea de comandos.
prom_get() {
  local ep="$1"
  shift
  load_token
  curl -sk --max-time "${PROM_TIMEOUT:-60}" -G \
    -H @<(printf 'Authorization: Bearer %s\n' "$GRAFANA_TOKEN") \
    "$PROM_URL/$ep" "$@"
}

# Convierte JSON de query_range (stdin) a filas CSV timestamp,series,value.
prom_json_to_csv() {
  python3 -c '
import sys, json, datetime
d = json.load(sys.stdin)
if d.get("status") != "success":
    sys.stderr.write("Prometheus: %s\n" % str(d)[:300]); sys.exit(2)
for s in d["data"]["result"]:
    m = {k: v for k, v in s["metric"].items() if k != "__name__"}
    lab = ";".join("%s=%s" % (k, m[k]) for k in sorted(m))
    for t, v in s["values"]:
        if v in ("NaN", "+Inf", "-Inf"):
            continue
        ts = datetime.datetime.fromtimestamp(float(t), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print("%s,%s,%s" % (ts, lab, v))
'
}
