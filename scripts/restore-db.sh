#!/usr/bin/env bash
# Restaura el dump golden en kiwi-perf-mariadb-0 (única réplica permitida).
# Uso: restore-db.sh [dump_dir] [--reset] [--expect-cases N] [--expect-rows N] [--expect-tables N]
#   --reset  borra la BD kiwi aunque tenga datos (uso entre repeticiones).
# Reintentos: part00/part01 son idempotentes (DROP TABLE IF EXISTS). Las partes grandes (part02+)
# traen en su cabecera `-- WHERE: history_id>=A and history_id<B`; antes de cada reintento se
# borra ese rango, así que son seguras de repetir.
set -euo pipefail

NS=kiwi
POD=kiwi-perf-mariadb-0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DUMP_DIR="$ROOT/dumps/golden-20261118"
RESET=0
EXPECT_TABLES=71
EXPECT_CASES=11492
EXPECT_ROWS=18541264
MAX_TRIES=3

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset) RESET=1 ;;
    --expect-cases) EXPECT_CASES="$2"; shift ;;
    --expect-rows) EXPECT_ROWS="$2"; shift ;;
    --expect-tables) EXPECT_TABLES="$2"; shift ;;
    -*) echo "Opción desconocida: $1" >&2; exit 2 ;;
    *) DUMP_DIR="$1" ;;
  esac
  shift
done

[[ "$POD" == "kiwi-perf-mariadb-0" ]] || { echo "Pod no permitido: $POD" >&2; exit 1; }
[[ -d "$DUMP_DIR" ]] || { echo "No existe $DUMP_DIR" >&2; exit 1; }

# Contraseña root tomada de la variable del propio pod; nunca sale del contenedor.
mdb() { kubectl exec -i -n "$NS" "$POD" -- sh -c 'exec mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" "$@"' sh "$@"; }
sql() { printf '%s\n' "$1" | mdb -N -B; }
ts() { date +%H:%M:%S; }
T0=$SECONDS

echo "[$(ts)] Verificando SHA256SUMS en $DUMP_DIR"
(cd "$DUMP_DIR" && sha256sum --quiet -c SHA256SUMS) || { echo "SHA256SUMS no coincide" >&2; exit 1; }

kubectl get pod "$POD" -n "$NS" >/dev/null
echo "[$(ts)] Probando conexión"
sql "SELECT 1" >/dev/null

existing=$(sql "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='kiwi'")
if [[ "$existing" -gt 0 && $RESET -ne 1 ]]; then
  echo "La BD kiwi ya tiene $existing tablas; use --reset para recrearla" >&2
  exit 1
fi

t=$SECONDS
sql "DROP DATABASE IF EXISTS kiwi; CREATE DATABASE kiwi CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
echo "[$(ts)] BD kiwi recreada ($((SECONDS - t)) s)"

mapfile -t PARTS < <(cd "$DUMP_DIR" && ls part[0-9][0-9]_*.sql.gz | sort)
[[ ${#PARTS[@]} -gt 0 ]] || { echo "Sin partes en $DUMP_DIR" >&2; exit 1; }

load_part() {
  local f="$1" db_args=() range lo hi attempt
  # part00 trae CREATE DATABASE/USE; el resto necesita la BD por defecto.
  [[ "$f" == part00_* ]] || db_args=(kiwi)
  range=$(zcat "$DUMP_DIR/$f" | head -c 4096 | sed -n 's/^-- WHERE:  history_id>=\([0-9]*\) and history_id<\([0-9]*\).*/\1 \2/p' | head -1) || true
  for attempt in $(seq 1 "$MAX_TRIES"); do
    if [[ -n "$range" ]]; then
      read -r lo hi <<<"$range"
      sql "DELETE FROM kiwi.testruns_historicaltestexecution WHERE history_id>=$lo AND history_id<$hi" || true
    elif [[ "$f" != part00_* && "$f" != part01_* ]]; then
      echo "Parte $f sin rango WHERE: no se puede reintentar de forma segura; use --reset" >&2
      return 1
    fi
    if zcat "$DUMP_DIR/$f" | mdb "${db_args[@]}"; then
      return 0
    fi
    echo "[$(ts)] FALLO $f intento $attempt/$MAX_TRIES" >&2
    sleep 5
  done
  return 1
}

for f in "${PARTS[@]}"; do
  t=$SECONDS
  load_part "$f" || { echo "No se pudo cargar $f tras $MAX_TRIES intentos; repita con --reset" >&2; exit 1; }
  echo "[$(ts)] OK $f ($((SECONDS - t)) s)"
done
echo "[$(ts)] Carga completa en $((SECONDS - T0)) s"

tables=$(sql "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='kiwi' AND table_type='BASE TABLE'")
cases=$(sql "SELECT COUNT(*) FROM kiwi.testcases_testcase")
rows=$(sql "SELECT COUNT(*) FROM kiwi.testruns_historicaltestexecution")
echo "tablas=$tables (esperado $EXPECT_TABLES) casos=$cases (esperado $EXPECT_CASES) historical_exec=$rows (esperado $EXPECT_ROWS)"
if [[ "$tables" -ne "$EXPECT_TABLES" || "$cases" -ne "$EXPECT_CASES" || "$rows" -ne "$EXPECT_ROWS" ]]; then
  echo "VERIFICACIÓN FALLIDA" >&2
  exit 1
fi
echo "Verificación OK (tiempo total $((SECONDS - T0)) s)"
