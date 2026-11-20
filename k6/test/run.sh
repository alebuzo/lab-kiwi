#!/bin/sh
# Usage: k6/test/run.sh <out_dir> [extra -e KEY=VAL ...]; run from repo root against the local mock.
OUT=$1; shift
mkdir -p "$OUT"
docker run --rm --user "$(id -u):$(id -g)" --network host -v "$PWD/k6:/scripts" -v "$PWD/$OUT:/results" \
  -e BASE_URL="${BASE_URL:-http://localhost:18443}" -e MAKISE_USER=makise -e MAKISE_PASSWORD=x -e PERF_USER_PASSWORD=y \
  "$@" grafana/k6:2.4.0 run --out csv=/results/k6.csv --summary-export /results/summary.json /scripts/main.js
