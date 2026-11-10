"""Gate 0 geometry sensitivity panel (logs/gate0_geometry_panel_20261102/PREREGISTRATION.md).

stage1 ASSET EDGE OUT   build the edge's event sets (edge + inverted, locked trigger config) and the random pool once
stage2 ASSET EDGE CELL OUT   label every set at one cell ("L" = locked cell, else "H<lookback>_V<vertical>", 1:1, EWM 100)
launch OUT              stage1 for all 6 edges in parallel, then every stage2 job, 8 at a time (BelowNormal)
analyse OUT             statistics + decision summary

usage (from trading_models_v2): python shared/research/counterfactual_tests/run_gate0_panel.py <command> ...
"""
from __future__ import annotations

import importlib
import json
import pickle
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[3]))
    from shared.io.timeparse import to_datetime_iso

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_counterfactuals as rc  # noqa: E402
import run_drift_baseline as db  # noqa: E402

V2 = rc.V2
EDGES = [("GBPJPY", "dailyrangecompletionfade_long"), ("HK", "drawdownhazardcontinuation_short"),
         ("JPN", "rangestructure_long"), ("JPN", "volatilitycarry_long"),
         ("XAUUSD", "rangestructure_long"), ("XAUUSD", "volatilitycarry_long")]
LOOKBACKS = (1.0, 4.0, 8.0)
VERTICALS = (4.0, 24.0, 96.0)
PANEL = [f"H{int(h)}_V{int(v)}" for h in LOOKBACKS for v in VERTICALS]
EWM_SPAN = 100
BELOW_NORMAL = 0x00004000
WORKERS = 8


def _modules(asset: str, edge: str):
    root = V2 / "assets" / asset / "edges" / edge
    sys.path.insert(0, str(V2)); sys.path.insert(0, str(root))
    runtime = importlib.import_module("runtime")
    ce = importlib.import_module("candidate_events")
    mr = importlib.import_module("model_rows")
    setattr(mr, rc.FAMILIES[edge]["validator"], lambda *a, **k: None)
    return root, runtime, ce, mr


def stage1(asset: str, edge: str, out: Path) -> None:
    root, runtime, ce, mr = _modules(asset, edge)
    fam = rc.FAMILIES[edge]
    stem = f"{asset.lower()}_{edge}"
    feats = pd.read_csv(root / "artifacts" / "research_dataset" / f"{stem}_feature_matrix.csv")
    try:
        expanded = getattr(ce, fam["add"])(feats)
    except ce.CandidateEventError:
        expanded = getattr(ce, fam["add"])(runtime.build_features(drop_warmup_rows=False))
    sets = {}
    for test in [None] + fam["tests"]:
        g = fam["gate"](ce, expanded, test)
        sets["edge" if test is None else test] = rc.primary_side(edge, getattr(ce, fam["extract"])(g))
    ev = sets["edge"]
    g = fam["gate"](ce, expanded, None)
    pool = pd.DataFrame({"Datetime": to_datetime_iso(g["Datetime"], utc=True),
                         "Event_Vol": pd.to_numeric(g["Event_Vol"], errors="coerce")})
    pool = pool[np.isfinite(pool.Event_Vol) & (pool.Event_Vol > 0)
                & pool.Datetime.between(ev["Datetime"].min(), ev["Datetime"].max())
                & ~pool.Datetime.isin(set(ev["Datetime"]))].drop_duplicates("Datetime")
    rng = np.random.default_rng(db.SEED)   # the same 20,000 bars as run_drift_baseline.py
    pool = pool.iloc[np.sort(rng.choice(len(pool), size=min(db.POOL, len(pool)), replace=False))].copy()
    pool["side"] = int(ev["side"].iloc[0])
    pool["edge_id"] = None
    if edge == "rangestructure_long":   # pass-through tag for the labeler's post-check (t > 0); not used in labeling
        pool["Structure_Bias_TStat"] = 1.0
    sets["random"] = pool.reset_index(drop=True)
    price = pd.DataFrame({"Datetime": to_datetime_iso(expanded["Datetime"], utc=True),
                          "Close": pd.to_numeric(expanded["Close"], errors="coerce")})
    payload = {"sets": sets, "price": price, "min_vol": float(getattr(ce, "MIN_VOL", getattr(ce, "MIN_EVENT_VOL", 1e-8))),
               "locked": {"lookback_h": float(ce.EVENT_VOL_LOOKBACK_HOURS), "ewm": int(ce.EVENT_VOL_EWM_SPAN),
                          "vertical_h": float(mr.VERTICAL_HORIZON_HOURS), "pt": float(mr.PT_MULTIPLIER),
                          "sl": float(mr.SL_MULTIPLIER)}}
    out.mkdir(parents=True, exist_ok=True)
    with open(out / f"stage1_{asset}_{edge}.pkl", "wb") as fh:
        pickle.dump(payload, fh)
    print(f"stage1 {asset}/{edge}: " + ", ".join(f"{k} {len(v)}" for k, v in sets.items()), flush=True)


def stage2(asset: str, edge: str, cell: str, out: Path) -> None:
    root, runtime, ce, mr = _modules(asset, edge)
    from shared.labels.calendar_time_barriers import calendar_time_event_vol
    with open(out / f"stage1_{asset}_{edge}.pkl", "rb") as fh:
        payload = pickle.load(fh)
    bars = runtime._load_asset_bars()  # noqa: SLF001
    lk = payload["locked"]
    if cell == "L":
        vertical, vol = lk["vertical_h"], None
    else:
        h, v = cell[1:].split("_V")
        vertical = float(v)
        mr.PT_MULTIPLIER = 1.0
        mr.SL_MULTIPLIER = 1.0
        price = payload["price"]
        arr = calendar_time_event_vol(price["Datetime"].dt.tz_convert(None).to_numpy(dtype="datetime64[ns]").view("int64"),
                                      price["Close"].to_numpy(dtype="float64"), hours=float(h), ewm_span=EWM_SPAN,
                                      min_vol=payload["min_vol"])
        vol = pd.Series(arr, index=price["Datetime"]).groupby(level=0).last()
    rows = []
    for name, frame in payload["sets"].items():
        f = frame.copy()
        if vol is not None:
            f["Event_Vol"] = f["Datetime"].map(vol)
            f = f[np.isfinite(pd.to_numeric(f["Event_Vol"], errors="coerce")) & (f["Event_Vol"] > 0)]
        if f.empty:
            continue
        lab = mr.apply_triple_barrier_labels(bars, f.reset_index(drop=True), vertical_horizon_hours=vertical)
        lab = lab[0] if isinstance(lab, tuple) else lab
        rows.append(pd.DataFrame({"set": name, "Datetime": to_datetime_iso(lab["Datetime"], utc=True),
                                  "realized_r": pd.to_numeric(lab["realized_r"], errors="coerce")}))
        print(f"{asset}/{edge} {cell} {name}: {len(lab)} labeled", flush=True)
    pd.concat(rows).to_csv(out / f"cell_{asset}_{edge}_{cell}.csv", index=False)
    print("written", flush=True)


def launch(out: Path) -> None:
    py = sys.executable
    me = str(Path(__file__).resolve())
    out.mkdir(parents=True, exist_ok=True)

    def start(args, log):
        fh = open(out / log, "w", encoding="utf-8")
        return subprocess.Popen([py, me, *args], cwd=str(V2), stdout=fh, stderr=subprocess.STDOUT,
                                creationflags=BELOW_NORMAL), fh

    procs = [start(["stage1", a, e, str(out)], f"stage1_{a}_{e}.log") for a, e in EDGES
             if not (out / f"stage1_{a}_{e}.pkl").exists()]
    for p, fh in procs:
        p.wait(); fh.close()
    print("stage1 exit codes", [p.returncode for p, _ in procs], flush=True)
    ready = [(a, e) for a, e in EDGES if (out / f"stage1_{a}_{e}.pkl").exists()]
    # longest first: locked cells (168 h on GBPJPY / HK) and 96 h verticals
    jobs = [(a, e, "L") for a, e in ready] + [(a, e, c) for c in sorted(PANEL, key=lambda c: -int(c.split("_V")[1]))
                                              for a, e in ready]
    running: list = []
    while jobs or running:
        while jobs and len(running) < WORKERS:
            a, e, c = jobs.pop(0)
            if (out / f"cell_{a}_{e}_{c}.csv").exists():
                continue
            p, fh = start(["stage2", a, e, c, str(out)], f"stage2_{a}_{e}_{c}.log")
            running.append((p, fh, f"{a}/{e} {c}"))
        time.sleep(5)
        for item in list(running):
            if item[0].poll() is not None:
                item[1].close(); running.remove(item)
                print(f"done {item[2]} exit {item[0].returncode}", flush=True)
    print("ALL STAGE2 DONE", flush=True)


def joint_diff(a: pd.DataFrame, b: pd.DataFrame) -> dict:
    """mean(a) - mean(b) with one week-clustered SE over both sets (the approved D075 counterfactual rule)."""
    ma, mb = a.realized_r.mean(), b.realized_r.mean()
    wa = a.Datetime.dt.tz_convert(None).dt.to_period("W").astype(str)
    wb = b.Datetime.dt.tz_convert(None).dt.to_period("W").astype(str)
    g = pd.concat([((a.realized_r - ma) / len(a)).groupby(wa.values).sum(),
                   (-(b.realized_r - mb) / len(b)).groupby(wb.values).sum()]).groupby(level=0).sum()
    se = float(np.sqrt((g ** 2).sum()))
    d = float(ma - mb)
    return {"diff": round(d, 5), "ci_lo": round(d - 1.96 * se, 5), "ci_hi": round(d + 1.96 * se, 5)}


def analyse(out: Path) -> None:
    res: dict = {}
    for a, e in EDGES:
        er = res.setdefault(f"{a}/{e}", {})
        for c in ["L"] + PANEL:
            f = out / f"cell_{a}_{e}_{c}.csv"
            if not f.exists():
                er[c] = "missing"; continue
            df = pd.read_csv(f)
            df["Datetime"] = to_datetime_iso(df["Datetime"], utc=True)
            df = df[np.isfinite(df.realized_r)]
            sets = {k: v for k, v in df.groupby("set")}
            edge, pool = sets["edge"], sets["random"]
            if c == "L":   # reproduction: the locked cell must give the stored model rows' realized_r
                st = pd.read_csv(V2 / "assets" / a / "edges" / e / "artifacts" / "research_dataset"
                                 / f"{a.lower()}_{e}_model_rows.csv", usecols=["Datetime", "realized_r"])
                st["Datetime"] = to_datetime_iso(st["Datetime"], utc=True)
                m = st.merge(edge, on="Datetime", suffixes=("_stored", ""))
                er["reproduction"] = {"stored": int(len(st)), "matched": int(len(m)),
                                      "max_abs_diff": float((m.realized_r - m.realized_r_stored).abs().max()),
                                      "PASS": bool(len(m) == len(st) and (m.realized_r - m.realized_r_stored).abs().max() < 1e-9)}
            cr: dict = {"edge": rc.clustered_ci(edge.realized_r, edge.Datetime),
                        "random_all": rc.clustered_ci(pool.realized_r, pool.Datetime)}
            to_month = lambda s: s.dt.tz_convert(None).dt.to_period("M").astype(str).to_numpy()  # noqa: E731
            draws = db.matched_draws(to_month(edge.Datetime), to_month(pool.Datetime),
                                     pool.realized_r.to_numpy(dtype="float64"), np.random.default_rng(db.SEED + 2))
            dm = db.compare(cr["edge"], float(draws.mean()), float(draws.std(ddof=1)), draws)
            cr["drift"] = {**dm, "PASS": dm["diff_ci_lo"] > 0}
            for t in [k for k in sets if k not in ("edge", "random")]:
                inv = sets[t]
                jd = joint_diff(edge, inv)
                cr[t] = {"n_inverted": int(len(inv)), "inverted_mean_R": round(float(inv.realized_r.mean()), 5), **jd,
                         "verdict": "INCONCLUSIVE" if len(inv) < rc.MIN_EVENTS else ("PASS" if jd["ci_lo"] > 0 else "FAIL")}
            er[c] = cr
    # decision rule
    summary = {}
    for k, er in res.items():
        tests = ["drift"] + sorted({t for c in PANEL if isinstance(er.get(c), dict) for t in er[c]
                                    if t not in ("edge", "random_all", "drift")})
        for t in tests:
            v = []
            for c in PANEL:
                x = er.get(c)
                if not isinstance(x, dict) or t not in x:
                    continue
                v.append("PASS" if (x[t]["PASS"] if t == "drift" else x[t]["verdict"] == "PASS") else
                         ("INCONCLUSIVE" if t != "drift" and x[t]["verdict"] == "INCONCLUSIVE" else "FAIL"))
            if not v or all(s == "INCONCLUSIVE" for s in v):
                continue
            n_pass = v.count("PASS")
            summary[f"{k} {t}"] = {"cells": len(v), "pass": n_pass, "stable": max(n_pass, len(v) - n_pass) >= 8}
    res["_summary"] = summary
    res["_decision"] = "STABLE" if summary and all(s["stable"] for s in summary.values()) else "UNSTABLE"
    (out / "gate0_panel_results.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=1), "\ndecision", res["_decision"], flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "stage1":
        stage1(sys.argv[2], sys.argv[3], Path(sys.argv[4]))
    elif cmd == "stage2":
        stage2(sys.argv[2], sys.argv[3], sys.argv[4], Path(sys.argv[5]))
    elif cmd == "launch":
        launch(Path(sys.argv[2]))
    else:
        analyse(Path(sys.argv[2]))
