#!/usr/bin/env python3
"""Parse k6 CSV output into the E8/E9/E17 tables (phase=measure only)."""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl

import numpy as np
import pandas as pd

OPS = ["U1", "U2", "U3", "U4"]
MIN_P95 = 20
MIN_P99 = 100
THRESHOLD_MS = 8000
RECOVERY_WINDOW_S = 60
RECOVERY_MIN_SAMPLES = 3
TAG_COLS = ["op", "phase", "rep", "test_type", "run_id", "error_type", "calls_failed", "scenario", "status"]
BASE_COLS = ["metric_name", "timestamp", "metric_value", "extra_tags", "scenario", "status", "error", "error_code"]
ISO_RE = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"
EPOCH_RE = r"(?<![\d.])\d{10}(?:\.\d+)?(?![\d])"


def md_table(df):
    if df is None or df.empty:
        return "_Sin datos._\n"
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join("" if v is None else str(v).replace("|", "/") for v in row) + " |")
    return "\n".join(lines) + "\n"


def num(x, d=1):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/d"
    return f"{x:.{d}f}"


def to_ts(s):
    s = s.strip()
    if re.fullmatch(EPOCH_RE, s):
        return pd.Timestamp(float(s), unit="s", tz="UTC")
    t = pd.Timestamp(s.replace("Z", ""))
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def parse_times(path):
    """Return {phase: [start, end]} from times.txt; tolerant to key=value or free text lines."""
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        low = line.lower()
        m = re.search(r"warm-?up|measure|medici[oó]n|cooldown|enfriamiento", low)
        if not m:
            continue
        word = m.group(0)
        phase = "warmup" if word.startswith("warm") else "cooldown" if word in ("cooldown", "enfriamiento") else "measure"
        stamps = re.findall(ISO_RE, line) + re.findall(EPOCH_RE, line)
        if not stamps:
            continue
        entry = out.setdefault(phase, [None, None])
        if len(stamps) >= 2:
            entry[0], entry[1] = to_ts(stamps[0]), to_ts(stamps[1])
        elif re.search(r"end|fin|stop", low):
            entry[1] = to_ts(stamps[0])
        else:
            entry[0] = to_ts(stamps[0])
    return out


def load_k6_csv(path, name=None):
    path = Path(path)
    header = pd.read_csv(path, nrows=0).columns
    usecols = [c for c in BASE_COLS if c in header]
    keep = {"http_req_duration", "rpc_ok", "a1_run_duration", "a1_runs_started", "a1_runs_completed"}
    parts = []
    for chunk in pd.read_csv(path, usecols=usecols, chunksize=500_000, low_memory=False):
        parts.append(chunk[chunk["metric_name"].isin(keep) | chunk["metric_name"].str.startswith("dur_")])
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=usecols)
    for c in BASE_COLS:
        if c not in df.columns:
            df[c] = np.nan
    codes, uniques = pd.factorize(df["extra_tags"].fillna(""))
    parsed = pd.DataFrame([dict(parse_qsl(u)) for u in uniques], index=range(len(uniques)))
    for c in TAG_COLS:
        if c not in parsed.columns:
            parsed[c] = np.nan
    tags = parsed.reindex(columns=TAG_COLS).iloc[codes].reset_index(drop=True)
    for c in TAG_COLS:
        if c in ("scenario", "status"):
            df[c] = df[c].where(df[c].notna(), tags[c])
        else:
            df[c] = tags[c].values
    ts = pd.to_numeric(df["timestamp"], errors="coerce")
    unit = "ms" if len(ts) and ts.dropna().median() > 1e11 else "s"
    df["ts"] = pd.to_datetime(ts, unit=unit, utc=True)
    df["metric_value"] = pd.to_numeric(df["metric_value"], errors="coerce")
    df["op"] = df["op"].astype("object")
    df["opg"] = df["op"].where(~df["op"].astype(str).str.startswith("A1"), "A1")
    if df["phase"].isna().all():
        df["phase"] = "measure"
    df["run_id"] = df["run_id"].astype("object")
    df["rep_name"] = name or path.parent.name
    return df.drop(columns=["timestamp", "extra_tags"])


class K6Rep:
    def __init__(self, df, times, name):
        self.df, self.times, self.name = df, times, name
        self.measure = df[df["phase"] == "measure"]
        self.window = self._window()
        self.cooldown_start = self._cooldown_start()

    def _window(self):
        t = self.times.get("measure")
        if t and t[0] is not None and t[1] is not None:
            return t[0], t[1]
        m = self.measure[self.measure["opg"] != "A1"]
        m = m if len(m) else self.measure
        if m.empty:
            return None, None
        start = m["ts"].min()
        cd = self.df[self.df["phase"] == "cooldown"]
        end = cd["ts"].min() if len(cd) else m["ts"].max()
        return start, end

    def _cooldown_start(self):
        t = self.times.get("cooldown")
        if t and t[0] is not None:
            return t[0]
        cd = self.df[self.df["phase"] == "cooldown"]
        return cd["ts"].min() if len(cd) else None

    @property
    def seconds(self):
        s, e = self.window
        return (e - s).total_seconds() if s is not None and e is not None else np.nan


def load_rep(path):
    path = Path(path)
    if path.is_dir():
        csv, times_path, name = path / "k6.csv", path / "times.txt", path.name
    else:
        csv, times_path, name = path, path.parent / "times.txt", path.stem
    return K6Rep(load_k6_csv(csv, name), parse_times(times_path), name)


def combine(reps):
    if len(reps) == 1:
        return reps[0]
    c = K6Rep(pd.concat([r.df for r in reps], ignore_index=True), {}, "combinado")
    c.window = (None, None)
    c.seconds_override = sum(r.seconds for r in reps)
    return c


def samples(df, op):
    """Response-time rows (ms) of an operation, preferring the dur_* Trend metrics."""
    if op == "A1":
        s = df[df["metric_name"] == "dur_A1_call"]
        if s.empty:
            s = df[(df["metric_name"] == "http_req_duration") & (df["opg"] == "A1")]
    else:
        s = df[df["metric_name"] == f"dur_{op}"]
        if s.empty:
            s = df[(df["metric_name"] == "http_req_duration") & (df["opg"] == op)]
    return s


def stats(values):
    v = np.asarray(values, dtype=float)
    n = len(v)
    if n == 0:
        return dict(n=0, median=np.nan, mean=np.nan, max=np.nan, p95=np.nan, p99=np.nan)
    return dict(
        n=n, median=float(np.median(v)), mean=float(v.mean()), max=float(v.max()),
        p95=float(np.percentile(v, 95)) if n >= MIN_P95 else np.nan,
        p99=float(np.percentile(v, 99)) if n >= MIN_P99 else np.nan,
    )


def top_error(failures):
    if failures is None or len(failures) == 0:
        return "—"
    t, c = Counter(failures.fillna("desconocido")).most_common(1)[0]
    return f"{t} ({c})"


def op_stats(rep):
    rpc = rep.measure[rep.measure["metric_name"] == "rpc_ok"]
    out = {}
    for op in OPS + ["A1"]:
        s = stats(samples(rep.measure, op)["metric_value"])
        r = rpc[rpc["opg"] == op]
        bad = r[r["metric_value"] == 0]
        s.update(checks=len(r), errors=len(bad), err_rate=100 * len(bad) / len(r) if len(r) else np.nan,
                 top_error=top_error(bad["error_type"]))
        out[op] = s
    return out


def pct_cell(value, n):
    return f"{num(value)} (N={n})" if not np.isnan(value) else f"n/d (N={n})"


def op_table(rep):
    rows = []
    for op, s in op_stats(rep).items():
        rows.append({
            "Operación": "A1 (por llamada)" if op == "A1" else op,
            "N muestras": s["n"], "Mediana": num(s["median"]), "Promedio": num(s["mean"]), "Máximo": num(s["max"]),
            "p95": pct_cell(s["p95"], s["n"]), "p99": pct_cell(s["p99"], s["n"]),
            "¿Mediana < 8 s?": "n/a" if op == "A1" else ("n/d" if np.isnan(s["median"]) else "sí" if s["median"] < THRESHOLD_MS else "no"),
            "Errores": s["errors"], "Tasa de error (%)": num(s["err_rate"], 2),
            "Tipo de error más frecuente": s["top_error"],
        })
    return pd.DataFrame(rows)


def a1_runs(rep):
    d = rep.measure
    a1 = d[(d["opg"] == "A1") | d["metric_name"].isin(["a1_runs_started", "a1_runs_completed", "a1_run_duration"])]
    a1 = a1[a1["run_id"].notna()].copy()
    a1["key"] = a1["rep_name"].astype(str) + "/" + a1["run_id"].astype(str)
    started = set(a1["key"])
    dur = a1[a1["metric_name"] == "a1_run_duration"].drop_duplicates("key")
    done = a1[a1["metric_name"] == "a1_runs_completed"].drop_duplicates("key")
    fails = a1[(a1["metric_name"] == "rpc_ok") & (a1["metric_value"] == 0)]
    status = {r.key: r.status for r in done.itertuples()}
    status.update({r.key: r.status for r in dur.itertuples()})
    finished = set(dur["key"]) | set(done["key"])
    calls_failed = pd.to_numeric(dur["calls_failed"], errors="coerce").fillna(0)
    err_keys = {k for k, s in status.items() if s == "error"} | set(dur["key"][calls_failed.values > 0]) | set(fails["key"])
    detail = []
    for k in sorted(err_keys):
        f = fails[fails["key"] == k]
        rid = k.split("/", 1)[1]
        if len(f):
            (op, et), _ = Counter(zip(f["op"].fillna("?"), f["error_type"].fillna("desconocido"))).most_common(1)[0]
            detail.append(f"{rid}: {et} en {op}")
        else:
            detail.append(f"{rid}: llamada no identificada")
    durations = dur["metric_value"].to_numpy() / 1000.0
    ends = dur.set_index("key")["ts"]
    intervals = sorted((ends[k] - pd.Timedelta(seconds=v), ends[k], k) for k, v in zip(dur["key"], durations))
    overlapped = set()
    for i, (_, e1, k1) in enumerate(intervals):
        for s2, _, k2 in intervals[i + 1:]:
            if s2 < e1:
                overlapped.update((k1, k2))
    have = len(durations) > 0
    return dict(
        started=len(started), complete=len(finished - err_keys), with_error=len(err_keys & started),
        unfinished=len(started - finished), error_detail="; ".join(detail) if detail else "—",
        dur_median=float(np.median(durations)) if have else np.nan,
        dur_min=float(durations.min()) if have else np.nan,
        dur_max=float(durations.max()) if have else np.nan,
        overlapped=len(overlapped), intervals=intervals,
    )


def global_stats(rep):
    d = rep.measure
    reqs = int((d["metric_name"] == "http_req_duration").sum())
    rpc = d[d["metric_name"] == "rpc_ok"]
    bad = rpc[rpc["metric_value"] == 0]
    if reqs == 0:
        reqs = len(rpc)
    secs = getattr(rep, "seconds_override", rep.seconds)
    types = Counter(bad["error_type"].fillna("desconocido"))
    start = rep.window[0]
    return dict(
        requests=reqs, throughput=reqs / secs if secs and not np.isnan(secs) else np.nan,
        errors=len(bad), err_rate=100 * len(bad) / len(rpc) if len(rpc) else np.nan,
        error_types=", ".join(f"{t} ({c})" for t, c in types.most_common()) or "—",
        first_error_min=(bad["ts"].min() - start).total_seconds() / 60 if len(bad) and start is not None else np.nan,
    )


def runs_table(rep):
    a, g = a1_runs(rep), global_stats(rep)
    return pd.DataFrame([{
        "Corridas A1 iniciadas": a["started"], "Corridas A1 completas": a["complete"],
        "Corridas A1 con error (tipo y llamada que falló)": f"{a['with_error']} ({a['error_detail']})" if a["with_error"] else 0,
        "Duración mediana (s)": num(a["dur_median"]), "Duración mín. (s)": num(a["dur_min"]), "Duración máx. (s)": num(a["dur_max"]),
        "Corridas solapadas": a["overlapped"], "Throughput (req/s)": num(g["throughput"], 2),
        "Solicitudes totales": g["requests"], "Errores por contenido": g["errors"],
        "Tasa de error (%)": num(g["err_rate"], 2), "Tipos de error observados": g["error_types"],
    }])


def recovery(rep, baseline_ms, window_s=RECOVERY_WINDOW_S, min_samples=RECOVERY_MIN_SAMPLES):
    """Seconds from cooldown start until the rolling median (window_s) of recovery_probe U3 durations is <= baseline_ms.

    The window needs at least min_samples probes; probes with rpc_ok=0 count as infinite latency.
    """
    start = rep.cooldown_start
    res = dict(cooldown_start=start, probes=0, recovered=False, seconds=np.nan, back=None, baseline=baseline_ms, a1_active=False)
    if start is None:
        return res
    cd = rep.df[rep.df["phase"] == "cooldown"]
    probe = cd[(cd["scenario"] == "recovery_probe") | (cd["opg"] == "U3")]
    dur = probe[probe["metric_name"] == "dur_U3"]
    if dur.empty:
        dur = probe[probe["metric_name"] == "http_req_duration"]
    dur = dur.sort_values("ts")[["ts", "metric_value"]].reset_index(drop=True)
    bad = probe[(probe["metric_name"] == "rpc_ok") & (probe["metric_value"] == 0)].sort_values("ts")
    if len(bad) and len(dur):
        m = pd.merge_asof(bad[["ts"]], dur.reset_index().rename(columns={"index": "i"}), on="ts",
                          direction="nearest", tolerance=pd.Timedelta(seconds=5))
        dur.loc[m["i"].dropna().astype(int), "metric_value"] = np.inf
    res["probes"] = len(dur)
    res["a1_active"] = any(e > start for _, e, _ in a1_runs(rep)["intervals"])
    if dur.empty or baseline_ms is None or np.isnan(baseline_ms):
        return res
    for i in range(len(dur)):
        t = dur.at[i, "ts"]
        win = dur[(dur["ts"] > t - pd.Timedelta(seconds=window_s)) & (dur["ts"] <= t)]
        if len(win) >= min_samples and win["metric_value"].median() <= baseline_ms:
            res.update(recovered=True, back=t, seconds=(t - start).total_seconds())
            break
    return res


def baseline_u3(reps):
    s = samples(combine(reps).measure, "U3")["metric_value"]
    return float(s.median()) if len(s) else np.nan


def recovery_table(rep, baseline_ms):
    r = recovery(rep, baseline_ms)
    fmt = lambda t: "n/d" if t is None else t.strftime("%Y-%m-%d %H:%M:%S UTC")
    return pd.DataFrame([{
        "Rep.": rep.name, "Hora fin de la carga": fmt(r["cooldown_start"]), "Hora de retorno a la línea base": fmt(r["back"]),
        "Segundos de recuperación": num(r["seconds"], 0) if r["recovered"] else "no recuperó",
        "¿Recuperó en el cooldown?": "sí" if r["recovered"] else ("n/d" if not r["probes"] else "no"),
        "¿Corrida de A1 activa durante el sondeo?": "sí" if r["a1_active"] else "no",
        "Línea base U3 (ms)": num(baseline_ms), "Sondeos": r["probes"],
    }])


def main():
    ap = argparse.ArgumentParser(description="Tablas E8/E9/E17 a partir del CSV de k6 (solo phase=measure).")
    ap.add_argument("paths", nargs="+", help="directorios results/<prueba>/rep<N> o archivos CSV")
    ap.add_argument("--baseline-u3-ms", type=float, default=None,
                    help="mediana de U3 de la línea base (carga, combinado) para el tiempo de recuperación; por defecto la de estas repeticiones")
    args = ap.parse_args()
    reps = [load_rep(p) for p in args.paths]
    base = args.baseline_u3_ms if args.baseline_u3_ms is not None else baseline_u3(reps)
    for r in reps + ([combine(reps)] if len(reps) > 1 else []):
        print(f"## {r.name}\n\n### Tabla E8 (ms)\n\n{md_table(op_table(r))}\n### Tabla E9\n\n{md_table(runs_table(r))}")
    print("### Tabla E17\n")
    print(md_table(pd.concat([recovery_table(r, base) for r in reps], ignore_index=True)))


if __name__ == "__main__":
    sys.exit(main())
