#!/usr/bin/env python3
"""Risk matrix: P x I per finding, area score = MAX of its findings (decision P19), ranking of 3 areas."""
import argparse
import sys
from pathlib import Path

import pandas as pd

AREAS = ["carga", "estres", "resistencia"]
LABELS = {"carga": "Carga", "estres": "Estrés", "resistencia": "Resistencia"}
ALIASES = {"estrés": "estres"}
EXAMPLE_PREFIX = "EXAMPLE"


def load(path, include_examples=False):
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    missing = {"hallazgo", "area", "probabilidad", "impacto", "justificacion"} - set(df.columns)
    if missing:
        raise ValueError(f"Faltan columnas: {sorted(missing)}")
    df = df[(df["hallazgo"].str.strip() != "") | (df["probabilidad"].str.strip() != "")].copy()
    df["ejemplo"] = df["hallazgo"].str.strip().str.upper().str.startswith(EXAMPLE_PREFIX)
    if not include_examples:
        df = df[~df["ejemplo"]]
    errors, areas, ps, impacts = [], [], [], []
    for i, r in df.iterrows():
        line = i + 2
        a = r["area"].strip().lower()
        a = ALIASES.get(a, a)
        if a not in AREAS:
            errors.append(f"línea {line}: área '{r['area']}' inválida (use {'|'.join(AREAS)})")
        vals = []
        for col in ("probabilidad", "impacto"):
            try:
                v = int(r[col])
                if not 1 <= v <= 5:
                    raise ValueError
            except ValueError:
                errors.append(f"línea {line}: {col} '{r[col]}' fuera de rango (entero 1-5)")
                v = None
            vals.append(v)
        areas.append(a)
        ps.append(vals[0])
        impacts.append(vals[1])
    if errors:
        raise ValueError("\n".join(errors))
    df["area"], df["probabilidad"], df["impacto"] = areas, ps, impacts
    df["puntuacion"] = df["probabilidad"] * df["impacto"]
    return df.reset_index(drop=True)


def ranking(df):
    rows = []
    for a in AREAS:
        sub = df[df["area"] == a]
        if sub.empty:
            rows.append({"area": a, "puntuacion": None, "hallazgo": "sin hallazgos calificados"})
            continue
        top = sub.sort_values("puntuacion", ascending=False).iloc[0]
        rows.append({"area": a, "puntuacion": int(top["puntuacion"]), "hallazgo": top["hallazgo"]})
    r = pd.DataFrame(rows)
    r["_s"] = r["puntuacion"].fillna(-1)
    r = r.sort_values(["_s", "area"], ascending=[False, True], kind="stable").drop(columns="_s").reset_index(drop=True)
    r.insert(0, "posicion", range(1, len(r) + 1))
    return r


def md(df):
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in df.itertuples(index=False):
        out.append("| " + " | ".join("n/d" if pd.isna(v) else str(v).replace("|", "/") for v in row) + " |")
    return "\n".join(out)


def render(df):
    f = df[["area", "hallazgo", "probabilidad", "impacto", "puntuacion"]].copy()
    f["area"] = f["area"].map(LABELS)
    f = f.rename(columns={"area": "Área", "hallazgo": "Hallazgo", "probabilidad": "Probabilidad (1–5)",
                          "impacto": "Impacto (1–5)", "puntuacion": "Puntuación (P × I)"})
    r = ranking(df)
    r["area"] = r["area"].map(LABELS)
    r = r.rename(columns={"posicion": "Posición", "area": "Área", "puntuacion": "Puntuación (máximo de sus hallazgos)",
                          "hallazgo": "Hallazgo determinante"})
    r["Recomendación para QA"] = ""
    return f"### Tabla E19. Hallazgos y calificación\n\n{md(f)}\n\n### Tabla E20. Ranking y recomendación\n\n{md(r)}\n"


def main():
    default = Path(__file__).with_name("risk_input.csv")
    ap = argparse.ArgumentParser(description="Matriz de riesgo y ranking de las tres áreas.")
    ap.add_argument("--input", default=str(default))
    ap.add_argument("--output", help="archivo markdown de salida (además de stdout)")
    ap.add_argument("--include-examples", action="store_true", help="incluye las filas EXAMPLE (solo para probar)")
    args = ap.parse_args()
    try:
        df = load(args.input, args.include_examples)
    except ValueError as e:
        print(f"Error en {args.input}:\n{e}", file=sys.stderr)
        return 1
    if df.empty:
        print("No hay hallazgos calificados (las filas EXAMPLE se ignoran); complete analysis/risk_input.csv.", file=sys.stderr)
        return 1
    text = render(df)
    print(text)
    if args.output:
        Path(args.output).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
