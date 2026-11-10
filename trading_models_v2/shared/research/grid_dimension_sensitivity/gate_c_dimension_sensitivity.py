"""Gate C (trade level): does changing one geometry dimension change the trades' outcomes? (owner question 2026-10-31)

Per edge, per full Gate C replay (405 non-control cells), per dimension P (L lookback, E EWM span, V vertical, R pt:sl):
  paired test  for every pair of cells that differ ONLY in P at adjacent levels, match trades on event_ts and test the
               mean of d = exec_r(a) - exec_r(b) with a week-clustered standard error (trades overlap in time);
               under "P does not matter" about 5 % of pairs show |t| > 1.96
  identical    share of matched trades with the same exec_r (|d| < 1e-9) -- the barrier change did not touch them
  selection    Gate C winner (argmax compound velocity among exec_mean_R > 0, coverage >= 0.99 cells, gate_c_pbo.py)
               vs the winner with P fixed at one level: does the selected geometry or its score change?

usage (from trading_models_v2): python shared/research/grid_dimension_sensitivity/gate_c_dimension_sensitivity.py OUT.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(V2))
from shared.research.gate_c_pbo import MIN_COVERAGE, compound_velocity_score  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

EDGES = ["GBPJPY/dailyrangecompletionfade_long", "HK/drawdownhazardcontinuation_short", "JPN/rangestructure_long",
         "JPN/volatilitycarry_long", "XAUUSD/rangestructure_long", "XAUUSD/volatilitycarry_long"]
DIMS = {"L": "event_vol_lookback", "E": "event_vol_ewm_span", "V": "vertical_horizon", "R": "ratio"}


def full_runs(art: Path) -> list[list[str]]:
    """Groups of stamps (same UTC day) whose summaries together hold the 405 non-control runbook cells."""
    by_day: dict[str, list[str]] = {}
    for f in sorted(art.glob("v3_execution_velocity_summary_*.csv")):
        stamp = f.stem.rsplit("_", 1)[-1]
        by_day.setdefault(stamp[:8], []).append(stamp)
    out = []
    for stamps in by_day.values():
        s = pd.concat([pd.read_csv(art / f"v3_execution_velocity_summary_{x}.csv").assign(run_stamp=x) for x in stamps])
        s = s[~s.is_control.fillna(False).astype(bool)].drop_duplicates("label")
        if len(s) == 405:
            out.append(stamps)
    return out


def cluster_t(d: np.ndarray, week: np.ndarray) -> float:
    n = len(d)
    m = d.mean()
    e = pd.Series(d - m).groupby(week).sum().to_numpy()
    g = len(e)
    if g < 2:
        return float("nan")
    var = (e ** 2).sum() / n ** 2 * g / (g - 1)
    return float(m / np.sqrt(var)) if var > 0 else (0.0 if m == 0 else float("inf"))


def analyse_run(art: Path, stamps: list[str]) -> dict:
    summ = pd.concat([pd.read_csv(art / f"v3_execution_velocity_summary_{s}.csv").assign(run_stamp=s) for s in stamps])
    summ = summ[~summ.is_control.fillna(False).astype(bool)].drop_duplicates("label").reset_index(drop=True)
    summ["ratio"] = (summ.pt / summ.sl).round(3)
    trades, score = {}, {}
    for r in summ.itertuples():
        t = pd.read_csv(art / f"v3_execution_trades_{r.label}_{r.run_stamp}.csv",
                        usecols=["status", "event_ts", "entry_ts", "exit_ts", "exec_r"])
        t = t[t.status == "resolved"].dropna(subset=["exec_r"])
        t["entry_ts"] = to_datetime_iso(t.entry_ts, utc=True)
        t["exit_ts"] = to_datetime_iso(t.exit_ts, utc=True)
        trades[r.label] = t.set_index("event_ts")
        score[r.label] = compound_velocity_score(t) if len(t) else float("nan")
    summ["cv_score"] = summ.label.map(score)
    out: dict = {"stamps": stamps, "cells": int(len(summ))}
    elig = summ[(summ.exec_mean_R > 0) & (summ.coverage_rate >= MIN_COVERAGE)]
    win = elig.loc[elig.cv_score.idxmax()] if len(elig) else None
    out["winner"] = None if win is None else {"label": win.label, "cv_score": float(win.cv_score),
                                              "exec_mean_R": float(win.exec_mean_R)}
    out["n_eligible"] = int(len(elig))
    for dim, col in DIMS.items():
        others = [c for c in DIMS.values() if c != col]
        levels = sorted(summ[col].unique())
        ts, ident, diffs, pair_of = [], [], [], []
        for _, g in summ.groupby(others):
            g = g.set_index(col)
            for a, b in zip(levels[:-1], levels[1:]):
                ta, tb = trades[g.loc[a, "label"]], trades[g.loc[b, "label"]]
                m = ta[["exec_r"]].join(tb[["exec_r"]], how="inner", lsuffix="_a", rsuffix="_b")
                if len(m) < 30:
                    continue
                d = (m.exec_r_a - m.exec_r_b).to_numpy()
                week = to_datetime_iso(m.index, utc=True).tz_convert(None).to_period("W").astype(str).to_numpy()
                ts.append(cluster_t(d, week))
                ident.append(float((np.abs(d) < 1e-9).mean()))
                diffs.append(float(d.mean()))
                pair_of.append(f"{float(a):g}-{float(b):g}")
        ts_a = np.abs(np.array(ts))
        rec = {"pairs": len(ts), "share_abs_t_gt_1_96": round(float((ts_a > 1.96).mean()), 3),
               "median_abs_t": round(float(np.median(ts_a)), 2), "median_identical_trade_share": round(float(np.median(ident)), 3),
               "median_abs_mean_diff_R": round(float(np.median(np.abs(diffs))), 4)}
        pf = pd.DataFrame({"pair": pair_of, "abs_t": ts_a, "ident": ident, "diff": diffs})
        rec["by_level_pair"] = {k: {"pairs": int(len(v)), "share_abs_t_gt_1_96": round(float((v.abs_t > 1.96).mean()), 3),
                                    "median_identical": round(float(v.ident.median()), 3),
                                    "mean_diff_R": round(float(v["diff"].mean()), 4)}
                                for k, v in pf.groupby("pair", sort=False)}
        if win is not None:
            fixed = {}
            for v in levels:
                e = elig[elig[col] == v]
                if len(e):
                    w = e.loc[e.cv_score.idxmax()]
                    fixed[str(float(v))] = {"label": w.label, "cv_score_ratio": round(float(w.cv_score / win.cv_score), 3),
                                            "same_other_dims": bool(all(w[o] == win[o] for o in others))}
            rec["winner_level"] = float(win[col])
            rec["winner_if_fixed"] = fixed
        out[dim] = rec
    return out


def main(out_path: Path) -> None:
    res = {}
    for e in EDGES:
        art = V2 / "assets" / e.split("/")[0] / "edges" / e.split("/")[1] / "artifacts" / "v3_horizon_lab"
        res[e] = [analyse_run(art, st) for st in full_runs(art)]
        for r in res[e]:
            print(e, r["stamps"][0][:8], "winner", r["winner"]["label"] if r["winner"] else None)
            for dim in DIMS:
                x = r[dim]
                print(f"   {dim}: pairs {x['pairs']}, |t|>1.96 {x['share_abs_t_gt_1_96']}, median|t| {x['median_abs_t']}, "
                      f"identical {x['median_identical_trade_share']}, |diff| {x['median_abs_mean_diff_R']}, "
                      f"fixed -> {({k: (v['cv_score_ratio'], v['same_other_dims']) for k, v in x.get('winner_if_fixed', {}).items()})}")
    out_path.write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
