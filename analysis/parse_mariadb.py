#!/usr/bin/env python3
"""Turn periodic SHOW GLOBAL STATUS dumps into per-interval deltas (Tabla E10)."""
import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

COUNTERS = [
    "Queries", "Questions", "Com_select", "Com_update", "Com_insert", "Created_tmp_tables",
    "Created_tmp_disk_tables", "Created_tmp_files", "Slow_queries", "Aborted_connects",
    "Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_requests",
]
LEVELS = ["Threads_connected", "Threads_running", "Max_used_connections"]
TS_RE = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})[T_ -]?(\d{2})[-:]?(\d{2})[-:]?(\d{2})")


def file_ts(path):
    m = TS_RE.search(path.stem.replace("mariadb_status_", ""))
    if m:
        return pd.Timestamp(*(int(g) for g in m.groups()), tz="UTC")
    return pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC")


def parse_status(text):
    out = {}
    for line in text.splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) == 2 and parts[0] in COUNTERS + LEVELS:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return out


def load_samples(rep_dir):
    rows = []
    for p in sorted(Path(rep_dir).glob("mariadb_status_*.txt")):
        vals = parse_status(p.read_text(errors="replace"))
        if vals:
            rows.append({"ts": file_ts(p), **vals})
    df = pd.DataFrame(rows)
    return df.sort_values("ts").reset_index(drop=True) if len(df) else df


def intervals(samples):
    """Per-interval deltas; a negative delta (counter reset, e.g. restart) is flagged and set to NaN."""
    if len(samples) < 2:
        return pd.DataFrame()
    s = samples
    out = pd.DataFrame({"ts": s["ts"].iloc[1:].reset_index(drop=True),
                        "dt_s": s["ts"].diff().dt.total_seconds().iloc[1:].reset_index(drop=True)})
    reset = pd.Series(False, index=out.index)
    for c in COUNTERS:
        if c not in s:
            continue
        d = s[c].diff().iloc[1:].reset_index(drop=True)
        reset |= d < 0
        out[c] = d.where(d >= 0)
    for c in LEVELS:
        if c in s:
            out[c] = s[c].iloc[1:].values
    out["reset"] = reset
    out["qps"] = out["Queries"] / out["dt_s"] if "Queries" in out else np.nan
    if {"Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_requests"} <= set(out.columns):
        out["bp_miss_ratio"] = out["Innodb_buffer_pool_reads"] / out["Innodb_buffer_pool_read_requests"].replace(0, np.nan)
        out["bp_hit_ratio"] = 1 - out["bp_miss_ratio"]
    return out


def summarize(iv, start=None, end=None):
    """Totals over [start, end]; intervals flagged as resets are excluded from sums and rates."""
    if iv is None or iv.empty:
        return {}
    if start is not None:
        iv = iv[iv["ts"] >= start]
    if end is not None:
        iv = iv[iv["ts"] <= end]
    ok = iv[~iv["reset"]]
    g = lambda c: float(ok[c].sum()) if c in ok else np.nan
    secs = ok["dt_s"].sum()
    reads, reqs = g("Innodb_buffer_pool_reads"), g("Innodb_buffer_pool_read_requests")
    mx = lambda c: float(iv[c].max()) if c in iv and len(iv) else np.nan
    return dict(
        intervals=len(iv), resets=int(iv["reset"].sum()), queries=g("Queries"),
        qps=g("Queries") / secs if secs else np.nan, tmp_tables=g("Created_tmp_tables"),
        tmp_disk_tables=g("Created_tmp_disk_tables"), tmp_files=g("Created_tmp_files"),
        slow_queries=g("Slow_queries"), aborted_connects=g("Aborted_connects"),
        threads_running_max=mx("Threads_running"), threads_connected_max=mx("Threads_connected"),
        max_used_connections=mx("Max_used_connections"),
        bp_ratio=reads / reqs if reqs else np.nan, bp_hit=1 - reads / reqs if reqs else np.nan,
    )


def main():
    ap = argparse.ArgumentParser(description="Deltas por intervalo de SHOW GLOBAL STATUS.")
    ap.add_argument("rep_dir", help="results/<prueba>/rep<N>")
    ap.add_argument("--csv", help="escribe los deltas por intervalo en este CSV")
    args = ap.parse_args()
    iv = intervals(load_samples(args.rep_dir))
    if iv.empty:
        print("Sin muestras suficientes (se necesitan al menos 2 archivos mariadb_status_*.txt).")
        return 1
    if args.csv:
        iv.to_csv(args.csv, index=False)
    for k, v in summarize(iv).items():
        print(f"{k}: {v}")
    if iv["reset"].any():
        print("AVISO: intervalos con reinicio de contadores (delta<0), excluidos de los totales:")
        print(iv.loc[iv["reset"], "ts"].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
