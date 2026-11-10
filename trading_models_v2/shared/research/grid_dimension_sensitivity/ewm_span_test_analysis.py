"""EWM span test analysis (logs/ewm_span_test_20261031/PREREGISTRATION.md).

Per edge: the new run (Gate A + Gate C over EWM 100,150,200,300) and the last full runbook replay (EWM 20,50,100).
  1. reproduction   EWM 100 cells: new vs old resolved trades and exec_mean_R
  2. selection      Gate C winner (gate_c_pbo rule) per grid -- runbook {20,50,100}, A {100,150,200}, B {100,200,300},
                    all 6 spans -- and the best compound velocity with the span fixed (ratio to the 6-span best)
  3. paired tests   adjacent spans 100-150, 150-200, 200-300 (week-clustered t on matched trades)
  4. Gate A         mean / best gross_mean_R and net_mean_R and median Event_Vol (bps) by span
  5. PBO / CSCV     each 405-cell grid's full PBO and Prob[OOS <= 0] (gate_c_pbo, all trials, D051)

usage (from trading_models_v2): python shared/research/grid_dimension_sensitivity/ewm_span_test_analysis.py OUT.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(V2))
from shared.research.gate_c_pbo import MIN_COVERAGE, build_performance_matrix, compound_velocity_score, pbo_from_matrix  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.research.grid_dimension_sensitivity.gate_c_dimension_sensitivity import cluster_t  # noqa: E402

LOG = V2 / "logs" / "ewm_span_test_20261031"
EDGES = {  # edge -> (old full replay stamps, old Gate A grid stamp)
    "GBPJPY/dailyrangecompletionfade_long": (["20261026T181822Z", "20261026T183748Z", "20261026T185441Z", "20261026T190522Z"], "20261017T124333Z"),
    "HK/drawdownhazardcontinuation_short": (["20261026T180733Z", "20261026T181639Z", "20261026T182525Z", "20261026T183152Z"], "20261017T124631Z"),
    "JPN/rangestructure_long": (["20261026T181130Z", "20261026T182111Z", "20261026T183137Z", "20261026T183701Z"], "20261026T175846Z"),
    "JPN/volatilitycarry_long": (["20261026T184703Z", "20261026T185545Z", "20261026T190402Z", "20261026T190829Z"], "20261026T180045Z"),
    "XAUUSD/rangestructure_long": (["20261017T130525Z", "20261017T132521Z", "20261017T134354Z", "20261017T135723Z"], "20261017T124540Z"),
    "XAUUSD/volatilitycarry_long": (["20261017T140456Z", "20261017T142311Z", "20261017T144053Z", "20261017T145221Z"], "20261017T134502Z"),
}
GRIDS = {"runbook_20_50_100": (20, 50, 100), "A_100_150_200": (100, 150, 200), "B_100_200_300": (100, 200, 300)}


def summary(art: Path, stamps: list[str]) -> pd.DataFrame:
    s = pd.concat([pd.read_csv(art / f"v3_execution_velocity_summary_{x}.csv").assign(run_stamp=x) for x in stamps])
    s = s[~s.is_control.fillna(False).astype(bool)].drop_duplicates("label").reset_index(drop=True)
    s["ratio"] = (s.pt / s.sl).round(3)
    return s


def trades(art: Path, r) -> pd.DataFrame:
    t = pd.read_csv(art / f"v3_execution_trades_{r.label}_{r.run_stamp}.csv",
                    usecols=["status", "event_ts", "entry_ts", "exit_ts", "exec_r"])
    t = t[t.status == "resolved"].dropna(subset=["exec_r"])
    t["entry_ts"] = to_datetime_iso(t.entry_ts, utc=True)
    t["exit_ts"] = to_datetime_iso(t.exit_ts, utc=True)
    return t


def winner(cells: pd.DataFrame) -> dict | None:
    e = cells[(cells.exec_mean_R > 0) & (cells.coverage_rate >= MIN_COVERAGE)]
    if e.empty:
        return None
    w = e.loc[e.cv_score.idxmax()]
    return {"label": w.label, "span": int(w.event_vol_ewm_span), "cv_score": float(w.cv_score),
            "exec_mean_R": float(w.exec_mean_R), "n_eligible": int(len(e))}


def analyse(edge: str, old_stamps: list[str], old_grid: str) -> dict:
    asset, name = edge.split("/")
    art = V2 / "assets" / asset / "edges" / name / "artifacts" / "v3_horizon_lab"
    recs = [json.loads(x) for x in (LOG / f"{asset}_{name}_ewm.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    new_stamps = [r["stamp"] for r in recs]
    new_grid = recs[0]["grid_stamp"]
    old, new = summary(art, old_stamps), summary(art, new_stamps)
    out: dict = {"new_stamps": new_stamps, "new_grid": new_grid, "old_stamps": old_stamps, "old_grid": old_grid,
                 "cells_old": int(len(old)), "cells_new": int(len(new))}
    # 1. reproduction
    m = old[old.event_vol_ewm_span == 100].merge(new[new.event_vol_ewm_span == 100], on="label", suffixes=("_old", "_new"))
    out["reproduction_e100"] = {"cells": int(len(m)),
                                "trades_equal": int((m.resolved_trades_old == m.resolved_trades_new).sum()),
                                "max_abs_exec_mean_R_diff": float((m.exec_mean_R_old - m.exec_mean_R_new).abs().max())}
    # 2. selection over the pooled cells (EWM 100 taken from the new run)
    pool = pd.concat([old[old.event_vol_ewm_span < 100], new], ignore_index=True)
    tr = {r.label: trades(art, r) for r in pool.itertuples()}
    pool["cv_score"] = pool.label.map({k: compound_velocity_score(v) if len(v) else np.nan for k, v in tr.items()})
    best_all = winner(pool)
    out["winner_all_6_spans"] = best_all
    out["winner_by_grid"] = {g: winner(pool[pool.event_vol_ewm_span.isin(s)]) for g, s in GRIDS.items()}
    out["best_cv_ratio_span_fixed"] = {}
    for sp in sorted(pool.event_vol_ewm_span.unique()):
        w = winner(pool[pool.event_vol_ewm_span == sp])
        out["best_cv_ratio_span_fixed"][str(int(sp))] = (None if (w is None or best_all is None)
                                                         else round(w["cv_score"] / best_all["cv_score"], 3))
    # 3. paired tests between adjacent spans
    out["paired"] = {}
    for a, b in ((20, 50), (50, 100), (100, 150), (150, 200), (200, 300)):
        ts, ident, diffs = [], [], []
        for _, g in pool[pool.event_vol_ewm_span.isin([a, b])].groupby(["event_vol_lookback", "vertical_horizon", "ratio"]):
            if len(g) != 2:
                continue
            la = g[g.event_vol_ewm_span == a].label.iloc[0]
            lb = g[g.event_vol_ewm_span == b].label.iloc[0]
            j = tr[la].set_index("event_ts")[["exec_r"]].join(tr[lb].set_index("event_ts")[["exec_r"]], how="inner",
                                                                lsuffix="_a", rsuffix="_b")
            if len(j) < 30:
                continue
            d = (j.exec_r_b - j.exec_r_a).to_numpy()   # positive = the longer span is better
            wk = to_datetime_iso(j.index, utc=True).tz_convert(None).to_period("W").astype(str).to_numpy()
            ts.append(cluster_t(d, wk)); ident.append(float((np.abs(d) < 1e-9).mean())); diffs.append(float(d.mean()))
        t = np.array(ts)
        out["paired"][f"{a}-{b}"] = {"pairs": int(len(t)), "share_longer_better_t_gt_1_96": round(float((t > 1.96).mean()), 3),
                                     "share_longer_worse_t_lt_-1_96": round(float((t < -1.96).mean()), 3),
                                     "median_identical": round(float(np.median(ident)), 3),
                                     "mean_diff_R_longer_minus_shorter": round(float(np.mean(diffs)), 4)}
    # 4. Gate A by span
    ga = pd.concat([pd.read_csv(art / f"v3_hybrid_fast_grid_{old_grid}.csv").query("event_vol_ewm_span < 100"),
                    pd.read_csv(art / f"v3_hybrid_fast_grid_{new_grid}.csv")], ignore_index=True)
    out["gate_a_by_span"] = {str(int(k)): {"mean_gross_R": round(float(v.gross_mean_R.mean()), 4),
                                          "best_gross_R": round(float(v.gross_mean_R.max()), 4),
                                          "mean_net_R": round(float(v.net_mean_R.mean()), 4),
                                          "best_net_R": round(float(v.net_mean_R.max()), 4),
                                          "median_event_vol_bps": round(float(v.median_event_vol_bps.median()), 2)}
                             for k, v in ga.groupby("event_vol_ewm_span")}
    # 5. full PBO per 405-cell grid
    out["pbo_by_grid"] = {}
    for g, spans in GRIDS.items():
        cells = pool[pool.event_vol_ewm_span.isin(spans)]
        if g == "runbook_20_50_100":   # the actual runbook replay, all from the old run
            cells = old
        perf, _ = build_performance_matrix(art, cells[["label", "run_stamp"]])
        p = pbo_from_matrix(perf)
        out["pbo_by_grid"][g] = {"n_trials": int(perf.shape[1]), "pbo": round(float(p["pbo"]), 4),
                                 "prob_oos_nonpositive": round(float(p["prob_oos_winner_nonpositive"]), 4)}
    return out


def main(out_path: Path) -> None:
    only = sys.argv[2:] or list(EDGES)
    res = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    for e in only:
        res[e] = analyse(e, *EDGES[e])
        out_path.write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(e, json.dumps(res[e])[:3000], flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
