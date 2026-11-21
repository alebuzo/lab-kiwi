#!/usr/bin/env python3
"""Consolidate k6, MariaDB, ephemeral-storage and Prometheus data of a test into markdown tables (E5-E17) and figures."""
import argparse
import json
import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_k6 as pk  # noqa: E402
import parse_mariadb as pm  # noqa: E402
from parse_k6 import md_table, num  # noqa: E402

TESTS = ["load", "endurance", "stress"]
KIWI, DB = "kiwi-perf-0", "kiwi-perf-mariadb-0"
OPS = pk.OPS
STRESS_LIMIT_PCT = 50
ENDURANCE_LIMIT_PCT = 25
NODE_MEM_ABORT = 30
TREND_PCT = 10
IDLE_SECONDS = 60
NODE_STEP_MIN = {"load": 5, "endurance": 10, "stress": 3}
PROM_KINDS = [("node_mem_pct", "node_mem"), ("node_cpu_pct", "node_cpu"), ("log_fs", "log_fs"), ("log", "log_fs"),
              ("throttl", "throttling"), ("restart", "restarts"), ("cpu", "cpu"), ("mem", "memory")]
EVENT_TYPES = {"OOMKilling": "OOM", "OOMKilled": "OOM", "SystemOOM": "OOM", "Evicted": "desalojo", "Evicting": "desalojo",
               "Killing": "reinicio", "BackOff": "reinicio", "Unhealthy": "otro", "FailedScheduling": "otro",
               "NodeNotReady": "otro", "Failed": "otro"}
MIB = 1024 * 1024


def to_utc_series(s):
    n = pd.to_numeric(s, errors="coerce")
    if n.notna().mean() > 0.9:
        unit = "ms" if n.dropna().median() > 1e11 else "s"
        return pd.to_datetime(n, unit=unit, utc=True)
    return pd.to_datetime(s, utc=True, errors="coerce")


def label_value(text, key):
    m = re.search(rf'"?{key}"?\s*[:=]\s*"([^"]+)"', str(text))
    return m.group(1) if m else None


def load_prom_file(path):
    raw = pd.read_csv(path)
    if raw.empty:
        return None
    cols = {c.lower(): c for c in raw.columns}
    tcol = next((cols[c] for c in ("timestamp", "time", "ts", "_time", "datetime", "date") if c in cols), raw.columns[0])
    vcol = cols.get("value", raw.columns[-1])
    out = pd.DataFrame({"ts": to_utc_series(raw[tcol]), "value": pd.to_numeric(raw[vcol], errors="coerce")})
    rest = raw.drop(columns=[tcol, vcol])
    blob = rest.astype(str).agg(" ".join, axis=1) if rest.shape[1] else pd.Series("", index=raw.index)

    def label(name, alt=()):
        for n in (name, *alt):
            if n in rest.columns.str.lower():
                return rest[cols[n]].astype(str)
        extracted = blob.map(lambda t: label_value(t, name))
        return extracted if extracted.notna().any() else None

    pod = label("pod")
    if pod is None:
        m = re.search(r"(kiwi[\w-]*-0)", path.stem)
        pod = pd.Series(m.group(1) if m else None, index=raw.index, dtype="object")
    node = label("node", ("instance",))
    if node is None:
        m = re.search(r"(asic-x86-\d+)", path.stem)
        node = pd.Series(m.group(1) if m else None, index=raw.index, dtype="object")
    out["pod"] = pod.values
    out["node"] = pd.Series(node.values).astype("object").str.replace(r":\d+$", "", regex=True).values
    return out.dropna(subset=["ts", "value"])


def load_prom(rep_dir):
    parts = {}
    for f in sorted(Path(rep_dir).glob("prom_*.csv")):
        low = f.stem.lower()
        kind = next((k for sub, k in PROM_KINDS if sub in low), None)
        if kind is None:
            continue
        d = load_prom_file(f)
        if d is not None and len(d):
            parts.setdefault(kind, []).append(d)
    out = {k: pd.concat(v, ignore_index=True) for k, v in parts.items()}
    if "throttling" in out and out["throttling"]["value"].max() <= 1.0:
        out["throttling"]["value"] *= 100
    return out


def load_events(rep_dir):
    rows = []
    for f in sorted(Path(rep_dir).glob("*event*.json")):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        for it in data.get("items", []) if isinstance(data, dict) else []:
            reason = it.get("reason", "")
            if reason in EVENT_TYPES or it.get("type") == "Warning":
                rows.append({"pod": it.get("involvedObject", {}).get("name", "?"), "tipo": EVENT_TYPES.get(reason, "otro"),
                             "reason": reason, "ts": it.get("lastTimestamp") or it.get("eventTime") or "",
                             "msg": (it.get("message") or "")[:120], "count": it.get("count", 1)})
    return pd.DataFrame(rows, columns=["pod", "tipo", "reason", "ts", "msg", "count"])


class Rep:
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        self.num = int(re.sub(r"\D", "", self.name) or 0)
        self.k6 = pk.load_rep(self.path) if (self.path / "k6.csv").exists() else None
        self.times = self.k6.times if self.k6 else pk.parse_times(self.path / "times.txt")
        self.prom = load_prom(self.path)
        eph = self.path / "ephemeral.csv"
        self.eph = pd.read_csv(eph) if eph.exists() else pd.DataFrame()
        if len(self.eph):
            self.eph["ts"] = pd.to_datetime(self.eph["timestamp_utc"], utc=True, errors="coerce")
        self.mdb = pm.intervals(pm.load_samples(self.path))
        self.events = load_events(self.path)
        self.start, self.end = self._window()

    def spans(self):
        ts = [d["ts"] for d in self.prom.values()]
        if self.k6:
            ts.append(self.k6.df["ts"])
        return (min(t.min() for t in ts), max(t.max() for t in ts)) if ts else (None, None)

    def _window(self):
        if self.k6 and self.k6.window[0] is not None:
            return self.k6.window
        m = self.times.get("measure")
        if m and m[0] is not None and m[1] is not None:
            return m
        return self.spans()

    @property
    def t0(self):
        w = self.times.get("warmup")
        return w[0] if w and w[0] is not None else self.spans()[0]

    @property
    def tend(self):
        c = self.times.get("cooldown")
        return c[1] if c and c[1] is not None else self.spans()[1]

    @property
    def cd_start(self):
        return self.k6.cooldown_start if self.k6 else None

    def series(self, kind, pod=None, node=None, window=True):
        d = self.prom.get(kind)
        if d is None:
            return pd.DataFrame(columns=["ts", "value"])
        if pod is not None:
            d = d[d["pod"] == pod]
        if node is not None:
            d = d[d["node"] == node]
        if window and self.start is not None:
            d = d[(d["ts"] >= self.start) & (d["ts"] <= self.end)]
        if kind in ("cpu", "memory", "log_fs"):
            return d.groupby("ts", as_index=False)["value"].sum()
        return d.sort_values("ts")[["ts", "value"]]

    def eph_vals(self, pod, col):
        if self.eph.empty or col not in self.eph:
            return pd.Series(dtype=float)
        return self.eph.loc[self.eph["pod"] == pod, col]


def find_reps(results, test):
    d = Path(results) / test
    reps = [Rep(p) for p in d.glob("rep*") if p.is_dir()] if d.exists() else []
    return sorted(reps, key=lambda r: r.num)


def trend(s):
    v = s["value"].to_numpy() if len(s) else np.array([])
    if len(v) < 8:
        return "n/d"
    q = max(1, len(v) // 4)
    a, b = v[:q].mean(), v[-q:].mean()
    if a == 0:
        return "n/d"
    ch = 100 * (b - a) / abs(a)
    return "sube" if ch > TREND_PCT else "baja" if ch < -TREND_PCT else "estable"


def idle(rep, kind, pod):
    s = rep.series(kind, pod, window=False)
    if s.empty:
        return np.nan
    return float(s[s["ts"] <= s["ts"].iloc[0] + pd.Timedelta(seconds=IDLE_SECONDS)]["value"].mean())


def pod_stats(rep, pod):
    cpu, mem = rep.series("cpu", pod), rep.series("memory", pod)
    thr = rep.series("throttling", pod)
    eph = rep.eph_vals(pod, "rootfs_du_kb")
    tmp = rep.eph_vals(pod, "tmp_kb")
    full = rep.series("restarts", pod, window=False)
    return dict(
        cpu_idle=idle(rep, "cpu", pod), cpu_avg=cpu["value"].mean() if len(cpu) else np.nan,
        cpu_max=cpu["value"].max() if len(cpu) else np.nan, mem_idle=idle(rep, "memory", pod) / MIB,
        mem_avg=mem["value"].mean() / MIB if len(mem) else np.nan, mem_max=mem["value"].max() / MIB if len(mem) else np.nan,
        eph_max=eph.max() / 1024 if len(eph) else np.nan, tmp_max=tmp.max() / 1024 if len(tmp) else np.nan,
        thr_max=thr["value"].max() if len(thr) else np.nan,
        restarts=float(full["value"].max() - full["value"].iloc[0]) if len(full) else np.nan,
        trend=f"mem: {trend(mem)}; cpu: {trend(cpu)}",
    )


def pods_in(rep, perf):
    found = set()
    for k in ("cpu", "memory"):
        if k in rep.prom:
            found |= set(rep.prom[k]["pod"].dropna().unique())
    return sorted(p for p in found if ("perf" in p) == perf)


def hhmm(t):
    return "n/d" if t is None or pd.isna(t) else t.strftime("%H:%M:%S")


def table_e5(test, reps, perf=True):
    rows = []
    for r in reps:
        for pod in pods_in(r, perf):
            s = pod_stats(r, pod)
            eph = "n/d" if np.isnan(s["eph_max"]) else f"{num(s['eph_max'])} (tmp {num(s['tmp_max'])})"
            rows.append({"Prueba": test, "Rep.": r.num, "Pod": pod, "Hora inicio": hhmm(r.start), "Hora fin": hhmm(r.end),
                         "CPU reposo": num(s["cpu_idle"], 3), "CPU prom.": num(s["cpu_avg"], 3), "CPU máx.": num(s["cpu_max"], 3),
                         "Mem. reposo": num(s["mem_idle"]), "Mem. prom.": num(s["mem_avg"]), "Mem. máx.": num(s["mem_max"]),
                         "Ef. storage máx.": eph, "Throttling máx. (%)": num(s["thr_max"]), "Tendencia (sube/estable/baja)": s["trend"]})
    return pd.DataFrame(rows)


def table_e6(test, reps):
    step = NODE_STEP_MIN[test]
    rows = []
    for r in reps:
        mem, cpu = r.prom.get("node_mem"), r.prom.get("node_cpu")
        if mem is None or r.start is None:
            continue
        rs = r.prom.get("restarts")
        prod = rs[~rs["pod"].fillna("").str.contains("perf")] if rs is not None else None
        t, hi = r.t0 or r.start, r.tend or r.end
        while t < hi:
            nxt = t + pd.Timedelta(minutes=step)
            m = mem[(mem["ts"] >= t) & (mem["ts"] < nxt)]
            if len(m):
                top = m.loc[m["value"].idxmax()]
                c = cpu[(cpu["ts"] >= t) & (cpu["ts"] < nxt)] if cpu is not None else None
                rest = "n/d"
                if prod is not None and len(prod):
                    base = prod.sort_values("ts").groupby("pod")["value"].first()
                    cur = prod[prod["ts"] < nxt].sort_values("ts").groupby("pod")["value"].last()
                    rest = int((cur - base.reindex(cur.index)).clip(lower=0).sum())
                rows.append({"Prueba": test, "Rep.": r.num, "Hora": hhmm(t), "Mem. nodo (%)": f"{num(top['value'])} ({top['node']})",
                             "CPU nodo (%)": num(c["value"].max()) if c is not None and len(c) else "n/d", "Reinicios prod.": rest,
                             "Pods prod. OK": "n/d (ver dashboard B)",
                             "Comentarios": f"supera {NODE_MEM_ABORT} % (criterio A-1)" if top["value"] > NODE_MEM_ABORT else ""})
            t = nxt
    return pd.DataFrame(rows)


def table_e7(test, reps):
    rows = []
    for r in reps:
        for (pod, tipo, reason), g in r.events.groupby(["pod", "tipo", "reason"]):
            rows.append({"Prueba": test, "Rep.": r.num, "Pod": pod, "Tipo (reinicio/desalojo/OOM/otro)": tipo, "Hora": str(g["ts"].min()),
                         "Motivo (de `describe`/eventos)": f"{reason}: {g['msg'].iloc[0]}", "Cantidad": int(g["count"].sum())})
        rs = r.prom.get("restarts")
        if rs is None:
            continue
        for pod, g in rs.sort_values("ts").groupby("pod"):
            d = g["value"].diff().fillna(0)
            if (d > 0).any():
                rows.append({"Prueba": test, "Rep.": r.num, "Pod": pod, "Tipo (reinicio/desalojo/OOM/otro)": "reinicio",
                             "Hora": hhmm(g.loc[d[d > 0].index[0], "ts"]),
                             "Motivo (de `describe`/eventos)": "contador de reinicios de Prometheus; ver `describe` para el motivo",
                             "Cantidad": int(d[d > 0].sum())})
    return pd.DataFrame(rows)


def table_e10(test, reps):
    rows = []
    for r in reps:
        s = pm.summarize(r.mdb, r.start, r.end)
        if not s:
            continue
        rows.append({"Prueba": test, "Rep.": r.num, "Δ Queries": num(s["queries"], 0), "Queries/s prom.": num(s["qps"], 1),
                     "Δ Created_tmp_tables": num(s["tmp_tables"], 0), "Δ Created_tmp_disk_tables": num(s["tmp_disk_tables"], 0),
                     "Δ Created_tmp_files": num(s["tmp_files"], 0), "Threads_running máx.": num(s["threads_running_max"], 0),
                     "Max_used_connections": num(s["max_used_connections"], 0),
                     "Ratio buffer pool (reads/requests)": num(s["bp_ratio"], 5), "Slow_queries Δ": num(s["slow_queries"], 0),
                     "Hit ratio (%)": num(100 * s["bp_hit"], 3), "Intervalos con reinicio de contadores": s["resets"]})
    return pd.DataFrame(rows)


def with_prefix(df, test, rep_label):
    df = df.copy()
    df.insert(0, "Rep.", rep_label)
    df.insert(0, "Prueba", test)
    return df


def summarize(reps):
    """Flat numeric summary of a list of repetitions (one rep or the combined set)."""
    reps = [r for r in reps if r.k6]
    if not reps:
        return {}
    k = pk.combine([r.k6 for r in reps])
    ops, g, a = pk.op_stats(k), pk.global_stats(k), pk.a1_runs(k)
    users = np.concatenate([pk.samples(k.measure, o)["metric_value"].to_numpy() for o in OPS])
    out = {f"{key}_{o}": ops[o][key] for o in OPS for key in ("median", "p95", "p99", "n")}
    out.update(mean_users=float(users.mean()) if len(users) else np.nan, a1_dur_median=a["dur_median"],
               throughput=g["throughput"], err_rate=g["err_rate"], requests=g["requests"])
    for role, pod in (("kiwi", KIWI), ("db", DB)):
        st = [pod_stats(r, pod) for r in reps]

        def pick(key, fn):
            vals = [s[key] for s in st if not np.isnan(s[key])]
            return float(fn(vals)) if vals else np.nan

        out.update({f"{role}_cpu_avg": pick("cpu_avg", np.mean), f"{role}_cpu_max": pick("cpu_max", np.max),
                    f"{role}_mem_avg": pick("mem_avg", np.mean), f"{role}_mem_max": pick("mem_max", np.max),
                    f"{role}_eph_max": pick("eph_max", np.max), f"{role}_restarts": pick("restarts", np.sum),
                    f"{role}_thr_max": pick("thr_max", np.max)})
    return out


def pctcell(v, n):
    return num(v) if not np.isnan(v) else f"n/d (N={int(n)})"


def table_e11(reps):
    sets = [(f"Rep. {r.num}", [r]) for r in reps] + ([("Combinado", reps)] if len(reps) > 1 else [])
    sums = [(lbl, summarize(rs)) for lbl, rs in sets]
    spec = [(f"Mediana {o} (ms)", lambda s, o=o: num(s[f"median_{o}"])) for o in OPS]
    spec += [("Promedio global usuarios (ms)", lambda s: num(s["mean_users"])),
             ("p95 / p99 por operación (ms)",
              lambda s: "; ".join(f"{o}: {pctcell(s[f'p95_{o}'], s[f'n_{o}'])} / {pctcell(s[f'p99_{o}'], s[f'n_{o}'])}" for o in OPS)),
             ("Duración mediana A1 (s)", lambda s: num(s["a1_dur_median"])),
             ("Throughput (req/s)", lambda s: num(s["throughput"], 2)), ("Tasa de error (%)", lambda s: num(s["err_rate"], 2))]
    for role, name in (("kiwi", "kiwi"), ("db", "MariaDB")):
        spec += [(f"CPU {name} prom./máx. (cores)", lambda s, r=role: f"{num(s[f'{r}_cpu_avg'], 3)} / {num(s[f'{r}_cpu_max'], 3)}"),
                 (f"Mem. {name} prom./máx. (MiB)", lambda s, r=role: f"{num(s[f'{r}_mem_avg'])} / {num(s[f'{r}_mem_max'])}")]
    spec += [("Almacenamiento efímero MariaDB máx. (MiB)", lambda s: num(s["db_eph_max"])),
             ("Reinicios kiwi / MariaDB; throttling máx. % kiwi / MariaDB",
              lambda s: f"{num(s['kiwi_restarts'], 0)} / {num(s['db_restarts'], 0)}; {num(s['kiwi_thr_max'])} / {num(s['db_thr_max'])}")]
    return pd.DataFrame([{"Métrica": lbl, **{c: fn(s) for c, s in sums}} for lbl, fn in spec])


def pct_increase(new, base):
    return np.nan if base is None or np.isnan(base) or base == 0 or np.isnan(new) else 100 * (new - base) / base


def table_e16(reps, base):
    sums = [(f"Estrés rep. {r.num}", summarize([r])) for r in reps] + [("Estrés combinado", summarize(reps))]
    comb = sums[-1][1]
    metrics = [(f"Mediana {o} (ms)", f"median_{o}", "pct", 0) for o in OPS]
    metrics += [("Tasa de error (%)", "err_rate", "pp", 2), ("Duración mediana A1 (s)", "a1_dur_median", "info", 1),
                ("Throughput (req/s)", "throughput", "info", 2), ("CPU kiwi máx. (cores)", "kiwi_cpu_max", "info", 3),
                ("Mem. kiwi máx. (MiB)", "kiwi_mem_max", "info", 1), ("CPU MariaDB máx. (cores)", "db_cpu_max", "info", 3),
                ("Mem. MariaDB máx. (MiB)", "db_mem_max", "info", 1)]
    rows = []
    for lbl, key, kind, d in metrics:
        b = base.get(key, np.nan) if base else np.nan
        c = comb.get(key, np.nan)
        if kind == "pp":
            inc = c - b
            incs, flag = f"{num(inc, 2)} pp", "n/d" if np.isnan(inc) else "sí" if inc > STRESS_LIMIT_PCT else "no"
        else:
            inc = pct_increase(c, b)
            incs = f"{num(inc)} %"
            flag = "n/d" if np.isnan(inc) else ("sí" if inc > STRESS_LIMIT_PCT else "no") if kind == "pct" else "informativo"
        rows.append({"Métrica": lbl, "Línea base (E11 comb.)": num(b, d), **{c_: num(s.get(key, np.nan), d) for c_, s in sums},
                     "Aumento (%)": incs, "¿> 50 %?": flag})
    return pd.DataFrame(rows)


def table_e15(reps):
    rows = []
    for r in reps:
        g = pk.global_stats(r.k6)
        rows.append({"Rep.": r.num, "Minuto de inicio de fallos": num(g["first_error_min"]) if g["errors"] else "sin fallos",
                     "Tipo de fallo (timeout, HTTP 5xx, error JSON-RPC, conexión rechazada, OOM, throttling, otro)": g["error_types"],
                     "Pod afectado": "n/d (completar con eventos)", "Evidencia (captura/log)": "", "Cantidad": g["errors"]})
    return pd.DataFrame(rows)


def hour_windows(r):
    n = max(1, int((r.end - r.start).total_seconds() // 3600))
    return [(r.start + pd.Timedelta(hours=h), r.start + pd.Timedelta(hours=h + 1)) for h in range(n)]


def slice_stats(r, a, b):
    d = r.k6.measure
    d = d[(d["ts"] >= a) & (d["ts"] < b)]
    meds = {o: float(np.median(pk.samples(d, o)["metric_value"])) if len(pk.samples(d, o)) else np.nan for o in OPS}
    rpc = d[d["metric_name"] == "rpc_ok"]
    err = 100 * (rpc["metric_value"] == 0).mean() if len(rpc) else np.nan
    return meds, err


def window_vals(r, kind, pod, a, b):
    s = r.series(kind, pod, window=False)
    return s[(s["ts"] >= a) & (s["ts"] < b)]["value"]


def cumulative_restarts(r, pod, until):
    s = r.series("restarts", pod, window=False)
    return float(s[s["ts"] < until]["value"].max() - s["value"].iloc[0]) if len(s) else np.nan


def endurance_tables(r):
    rows, first = [], None
    for h, (a, b) in enumerate(hour_windows(r), 1):
        meds, err = slice_stats(r, a, b)
        first = first or meds
        row = {"Hora de prueba": h, **{f"Mediana {o}": num(meds[o]) for o in OPS}, "Tasa de error (%)": num(err, 2)}
        for pod, name in ((KIWI, "kiwi"), (DB, "MariaDB")):
            m = window_vals(r, "memory", pod, a, b) / MIB
            row[f"Mem. {name} (prom./máx.)"] = f"{num(m.mean())} / {num(m.max())}" if len(m) else "n/d"
        for pod, name in ((KIWI, "kiwi"), (DB, "MariaDB")):
            c = window_vals(r, "cpu", pod, a, b)
            row[f"CPU {name} (prom.)"] = num(c.mean(), 3) if len(c) else "n/d"
        row["Reinicios acumulados (kiwi / MariaDB)"] = f"{num(cumulative_restarts(r, KIWI, b), 0)} / {num(cumulative_restarts(r, DB, b), 0)}"
        rows.append(row)
    last, _ = slice_stats(r, r.end - pd.Timedelta(hours=1), r.end)
    e13 = []
    for o in OPS:
        inc = pct_increase(last[o], first[o])
        e13.append({"Umbral": f"Degradación del tiempo {o}",
                    "Cálculo": f"(mediana última hora {num(last[o])} ms − primera hora {num(first[o])} ms) / primera hora",
                    "Resultado": f"{num(inc)} %", "¿Se supera?": "n/d" if np.isnan(inc) else "sí" if inc > ENDURANCE_LIMIT_PCT else "no"})
    e14 = []
    for pod, name in ((KIWI, "kiwi"), (DB, "MariaDB")):
        full = r.series("memory", pod, window=False)
        if full.empty:
            continue
        before = idle(r, "memory", pod) / MIB
        peak = r.series("memory", pod)["value"].max() / MIB
        tail = full[full["ts"] >= full["ts"].iloc[-1] - pd.Timedelta(seconds=IDLE_SECONDS)]["value"].mean() / MIB
        var = pct_increase(tail, before)
        e14.append({"Pod": pod, "Mem. antes (MiB)": num(before), "Mem. máx. en prueba": num(peak), "Mem. 5 min tras cooldown": num(tail),
                    "Variación vs antes (%)": num(var)})
        e13.append({"Umbral": f"Degradación de memoria {name}", "Cálculo": "memoria tras cooldown vs nivel previo",
                    "Resultado": f"{num(var)} %", "¿Se supera?": "n/d" if np.isnan(var) else "sí" if var > ENDURANCE_LIMIT_PCT else "no"})
    meds = {o: float(np.median(pk.samples(r.k6.measure, o)["metric_value"])) for o in OPS if len(pk.samples(r.k6.measure, o))}
    e13.append({"Umbral": "Mediana de usuarios < 8 s", "Cálculo": "mediana de cada operación en toda la medición",
                "Resultado": ", ".join(f"{o}={num(v)} ms" for o, v in meds.items()) or "n/d",
                "¿Se supera?": "n/d" if not meds else "sí" if any(v >= pk.THRESHOLD_MS for v in meds.values()) else "no"})
    return pd.DataFrame(rows), pd.DataFrame(e13), pd.DataFrame(e14)


def shade(ax, r):
    if r.t0 is not None and r.start is not None:
        ax.axvspan(r.t0, r.start, color="grey", alpha=0.15, label="warm-up")
    if r.cd_start is not None and r.tend is not None:
        ax.axvspan(r.cd_start, r.tend, color="orange", alpha=0.15, label="cooldown")


def save(fig, path, made):
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    made.append(path)


def figures(test, reps, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    made = []
    for r in reps:
        tag = f"rep{r.num}"
        if r.k6:
            d = r.k6.df
            fig, ax = plt.subplots(figsize=(11, 4.5))
            for o in OPS + ["A1"]:
                s = pk.samples(d, o)
                if len(s):
                    ax.plot(s.set_index("ts")["metric_value"].resample("1min").median(), label=o, marker=".", ms=3)
            ax.axhline(pk.THRESHOLD_MS, color="red", ls="--", lw=0.8, label="8 s")
            shade(ax, r)
            ax.set_yscale("log")
            ax.set(title=f"Tiempo de respuesta (mediana por minuto) - {test} {tag}", ylabel="ms (escala log)", xlabel="hora UTC")
            ax.legend(ncol=4, fontsize=8)
            save(fig, outdir / f"{tag}_latencia.png", made)
            fig, ax = plt.subplots(figsize=(11, 3.5))
            req = d[d["metric_name"] == "http_req_duration"]
            req = req if len(req) else d[d["metric_name"] == "rpc_ok"]
            ax.plot(req.set_index("ts")["metric_value"].resample("1min").count() / 60, color="tab:blue")
            shade(ax, r)
            ax.set(title=f"Throughput (req/s, promedio por minuto) - {test} {tag}", ylabel="req/s", xlabel="hora UTC")
            save(fig, outdir / f"{tag}_throughput.png", made)
        panels = [p for p in (("cpu", "CPU (cores)", 1), ("memory", "Memoria working set (MiB)", MIB), ("node_mem", "Memoria del nodo (%)", 1))
                  if p[0] in r.prom]
        if panels:
            fig, axes = plt.subplots(len(panels), 1, figsize=(11, 3.2 * len(panels)), sharex=True, squeeze=False)
            for ax, (kind, title, div) in zip(axes[:, 0], panels):
                for name, g in r.prom[kind].groupby("node" if kind == "node_mem" else "pod"):
                    g = g.sort_values("ts")
                    ax.plot(g["ts"], g["value"] / div, label=name, lw=1)
                if kind == "node_mem":
                    ax.axhline(NODE_MEM_ABORT, color="red", ls="--", lw=0.8, label=f"aborto {NODE_MEM_ABORT} %")
                shade(ax, r)
                ax.set_ylabel(title)
                ax.legend(fontsize=7, ncol=3)
            axes[0, 0].set_title(f"Recursos (Prometheus) - {test} {tag}")
            axes[-1, 0].set_xlabel("hora UTC")
            save(fig, outdir / f"{tag}_recursos.png", made)
    return made


def section(title, df, note=None):
    return f"### {title}\n\n{md_table(df)}\n" + (f"> {note}\n\n" if note else "")


def build_report(test, results, baseline_test, base_u3, make_figs=True):
    reps = find_reps(results, test)
    if not reps:
        return f"## Prueba: {test}\n\n_Sin datos en {Path(results) / test}._\n"
    k6reps = [r for r in reps if r.k6]
    out = [f"## Prueba: {test} ({len(reps)} repetición(es))\n"]
    out.append(section("Tabla E5. Recursos por pod y repetición (réplica)", table_e5(test, reps, True),
                       "CPU en cores; memoria y almacenamiento en MiB. Reposo = promedio de los primeros 60 s de la serie; prom./máx. en la medición; "
                       "Ef. storage = máx. de `rootfs_du_kb` (`du -sx /`, incluye las capas de la imagen) de `ephemeral.csv` en toda la repetición (entre paréntesis, `/tmp`); "
                       "tendencia = último vs primer cuarto de la medición (±10 %)."))
    prod = table_e5(test, reps, False)
    if len(prod):
        out.append(section("Tabla E5b. Recursos de producción (contexto)", prod))
    out.append(section("Tabla E6. Monitoreo del nodo y producción durante la prueba", table_e6(test, reps),
                       f"Máximos por intervalo; se marca el criterio A-1 si la memoria del nodo supera {NODE_MEM_ABORT} %."))
    out.append(section("Tabla E7. Reinicios, desalojos, OOM y eventos", table_e7(test, reps)))
    base_sum = None
    if baseline_test:
        base_reps = [r for r in find_reps(results, baseline_test) if r.k6]
        if base_reps:
            base_sum = summarize(base_reps)
            base_u3 = base_u3 if base_u3 is not None else base_sum["median_U3"]
    sets = [(r.num, [r]) for r in k6reps] + ([("combinado", k6reps)] if len(k6reps) > 1 else [])
    for lbl, rs in sets:
        k = pk.combine([r.k6 for r in rs])
        out.append(section(f"Tabla E8. Tiempos de respuesta por operación (ms) - {test}, rep. {lbl}", with_prefix(pk.op_table(k), test, lbl),
                           "p95 solo con N ≥ 20 y p99 solo con N ≥ 100 por operación y repetición; si no se alcanza, n/d (N entre paréntesis)."))
    if sets:
        e9 = pd.concat([with_prefix(pk.runs_table(pk.combine([r.k6 for r in rs])), test, lbl) for lbl, rs in sets], ignore_index=True)
        out.append(section("Tabla E9. Corridas de A1, throughput y errores", e9))
    out.append(section("Tabla E10. Análisis de base de datos", table_e10(test, reps),
                       "Δ en la ventana de medición; los intervalos con reinicio de contadores (delta < 0) se excluyen de los totales."))
    if test == "load" and k6reps:
        out.append(section("Tabla E11. Línea base (carga) - repeticiones por separado y combinadas", table_e11(k6reps)))
    if test == "endurance":
        for r in k6reps:
            e12, e13, e14 = endurance_tables(r)
            out.append(section(f"Tabla E12. Resistencia - tendencia por hora (rep. {r.num})", e12,
                               "Horas completas desde el inicio de la medición; E13 compara la primera hora con los últimos 60 min. Memoria en MiB, CPU en cores."))
            out.append(section("Tabla E13. Evaluación de umbrales de resistencia", e13, f"Umbral de superación: > {ENDURANCE_LIMIT_PCT} %."))
            out.append(section("Tabla E14. Comparación de memoria pre/post", e14,
                               "'Tras cooldown' es el promedio de los últimos 60 s de la serie exportada; verificar que la exportación llegue a 5 min tras el cooldown."))
    if test == "stress" and k6reps:
        out.append(section("Tabla E15. Estrés - fallos observados", table_e15(k6reps)))
        out.append(section(f"Tabla E16. Estrés vs línea base ({baseline_test or 'sin línea base'})", table_e16(k6reps, base_sum),
                           f"Aumento relativo en medianas (umbral > {STRESS_LIMIT_PCT} %); la tasa de error se reporta en puntos porcentuales absolutos (P18) "
                           f"y su aumento se compara con {STRESS_LIMIT_PCT} pp."))
        rec = pd.concat([pk.recovery_table(r.k6, base_u3) for r in k6reps], ignore_index=True)
        out.append(section("Tabla E17. Tiempo de recuperación", rec,
                           "Regla: segundos desde el inicio del cooldown hasta que la mediana móvil de 60 s (mín. 3 sondeos; sondeo fallido = ∞) "
                           f"de U3 del sondeo es ≤ la mediana de U3 de la línea base ({num(base_u3)} ms)."))
    if make_figs:
        figs = figures(test, reps, Path(results) / test / "figures")
        out.append("### Figuras\n\n" + "\n".join(f"- `{f}`" for f in figs) + "\n")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Reporte de una prueba: tablas E5-E17 en markdown y figuras.")
    ap.add_argument("test_pos", nargs="?", choices=TESTS + ["all"], help="atajo: report.py all")
    ap.add_argument("--test", choices=TESTS + ["all"], default=None)
    ap.add_argument("--results", default="results")
    ap.add_argument("--baseline-test", default="load", help="prueba usada como línea base (por defecto load)")
    ap.add_argument("--baseline-u3-ms", type=float, default=None, help="mediana de U3 de la línea base para el tiempo de recuperación")
    ap.add_argument("--no-figures", action="store_true")
    args = ap.parse_args()
    test = args.test or args.test_pos
    if not test:
        ap.error("indique --test {load,endurance,stress,all}")
    parts = []
    for t in (TESTS if test == "all" else [test]):
        text = build_report(t, args.results, args.baseline_test, args.baseline_u3_ms, not args.no_figures)
        parts.append(text)
        if (Path(args.results) / t).exists():
            (Path(args.results) / t / "report.md").write_text(text)
    full = "\n".join(parts)
    if test == "all":
        (Path(args.results) / "report_all.md").write_text(full)
    print(full)


if __name__ == "__main__":
    main()
