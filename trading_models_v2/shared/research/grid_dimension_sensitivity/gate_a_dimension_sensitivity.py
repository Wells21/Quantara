"""Does each geometry-grid dimension change the Gate A outcome? (owner question 2026-10-31: is EWM span redundant?)

Input: collect_gate_a_grids.py output (one row per cell per grid run). Per grid run and per dimension P (event-vol
lookback L, EWM span E, vertical V, pt:sl ratio R), using only runs in which P has >= 2 levels and is fully crossed with
the other dimensions:
  share_main   between-level sum of squares of P / total sum of squares of the cell mean R (main effect, balanced design)
  med_range    median over the other-dimension combinations of (max - min) mean R across P's levels
  corr_min     smallest Pearson correlation between two P-levels' cell vectors (aligned on the other dimensions)
  win_loss     best cell mean R minus the best cell with P fixed at its middle level (what fixing P would have cost)
  win_same     the best cell with P fixed has the same other dimensions as the overall winner
Metrics are on gross_mean_R (mid, cost-free) and net_mean_R (after the grid's cost).

usage (from trading_models_v2): python shared/research/grid_dimension_sensitivity/gate_a_dimension_sensitivity.py IN.csv OUT.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DIMS = {"L": "event_vol_lookback", "E": "event_vol_ewm_span", "V": "vertical_horizon", "R": "ratio"}


def run_metrics(g: pd.DataFrame, dim: str, y: str) -> dict | None:
    col = DIMS[dim]
    others = [DIMS[d] for d in DIMS if d != dim]
    levels = sorted(g[col].unique())
    if len(levels) < 2:
        return None
    wide = g.pivot_table(index=others, columns=col, values=y, aggfunc="first")
    if wide.isna().any().any() or len(wide) * len(levels) != len(g):
        return None  # not fully crossed
    vals = wide.to_numpy(dtype=float)
    grand = vals.mean()
    ss_tot = ((vals - grand) ** 2).sum()
    ss_main = len(wide) * ((vals.mean(axis=0) - grand) ** 2).sum()
    rng = vals.max(axis=1) - vals.min(axis=1)
    cm = np.corrcoef(vals.T) if len(wide) > 2 else np.full((len(levels), len(levels)), np.nan)
    mid = levels[(len(levels) - 1) // 2] if len(levels) % 2 else levels[len(levels) // 2 - 1]
    best = g.loc[g[y].idxmax()]
    gm = g[g[col] == mid]
    best_mid = gm.loc[gm[y].idxmax()]
    return {
        "levels": [float(v) for v in levels],
        "share_main": float(ss_main / ss_tot) if ss_tot > 0 else 0.0,
        "med_range": float(np.median(rng)),
        "corr_min": float(np.nanmin(cm[np.triu_indices(len(levels), 1)])),
        "win_level": float(best[col]),
        "mid_level": float(mid),
        "win_loss": float(best[y] - best_mid[y]),
        "win_same": bool(all(best[o] == best_mid[o] for o in others)),
        "best": float(best[y]),
        "sign_flip": bool((best[y] > 0) != (best_mid[y] > 0)),
    }


def main(inp: Path, out: Path) -> None:
    df = pd.read_csv(inp)
    df["ratio"] = (df.pt / df.sl).round(3)
    recs = []
    for (edge, stamp), g in df.groupby(["edge_id", "stamp"]):
        for dim in DIMS:
            for y in ("gross_mean_R", "net_mean_R"):
                m = run_metrics(g, dim, y)
                if m:
                    recs.append({"edge_id": edge, "stamp": stamp, "runbook_grid": bool(g.runbook_grid.iloc[0]),
                                 "cells": len(g), "dim": dim, "y": y, **m})
    r = pd.DataFrame(recs)
    summary = {}
    for scope, sub in (("runbook_405", r[r.runbook_grid]), ("all_grids", r)):
        for y in ("gross_mean_R", "net_mean_R"):
            s = sub[sub.y == y]
            for dim in DIMS:
                d = s[s.dim == dim]
                if d.empty:
                    continue
                summary[f"{scope}|{y}|{dim}"] = {
                    "runs": int(len(d)), "sides": int(d.edge_id.nunique()),
                    "share_main_median": round(float(d.share_main.median()), 4),
                    "share_main_p90": round(float(d.share_main.quantile(0.9)), 4),
                    "med_range_median": round(float(d.med_range.median()), 4),
                    "corr_min_median": round(float(d.corr_min.median()), 4),
                    "corr_min_p10": round(float(d.corr_min.quantile(0.1)), 4),
                    "win_loss_median": round(float(d.win_loss.median()), 4),
                    "win_loss_p90": round(float(d.win_loss.quantile(0.9)), 4),
                    "win_same_share": round(float(d.win_same.mean()), 3),
                    "sign_flip_runs": int(d.sign_flip.sum()),
                    "win_level_counts": {str(k): int(v) for k, v in d.win_level.value_counts().sort_index().items()},
                }
    out.write_text(json.dumps({"per_run": recs, "summary": summary}, indent=1), encoding="utf-8")
    for k, v in summary.items():
        print(k, {kk: vv for kk, vv in v.items() if kk != "win_level_counts"}, v["win_level_counts"])


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
