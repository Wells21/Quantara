"""AFML Ch.12 walkthrough (owner-approved 2026-10-26): CPCV backtest PATHS (AFML s.12.4.2 step 5, pp.165-166).

Step 03 runs CombinatorialPurgedCV(N = 5, k = 2) but averages each row's phi = 4 path forecasts into one probability.
This test keeps the paths separate. For each CPCV setting (N = 5 and N = 10, k = 2; rule of thumb k = 2, N = phi + 1,
p.165) every split is fitted exactly as Step 03 fits it (the edge's own Step 03 module: selected features, Step 02 best
params, purge on label_t1, embargo max(100, 1 %), sequential-bootstrap bagging, train weights), each test group's
forecasts are assigned to their path (skfolio get_path_ids), and each of the phi assembled paths is run through the live
decision chain:
  calibrated p = the edge's frozen sigmoid calibrator (fitted on the averaged OOF -- caveat, unchanged for this test);
  approve if p >= the live threshold chosen by the portfolio allocator (portfolio_live/live_manifest.json);
  size m = payoff-aware AFML s.10.3 size with the manifest's empirical break-even (D007); risk m / c_q (D017);
  P&L = risk x Gate C broker executed R (Moneta), booked on the exit day; daily Sharpe x sqrt(252).
Reference: the current single averaged path (the calibrated OOF file's live_calibrated_probability / afml_bet_size).
Research period only; the holdout is never read.

usage (from trading_models_v2, venv312):
  python -m shared.research.cpcv_path_backtest --edge XAUUSD/rangestructure_long --out <json>
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from skfolio.model_selection import CombinatorialPurgedCV

from shared.calibration.sigmoid_afml import payoff_aware_afml_components

V2 = Path(__file__).resolve().parents[2]
SETTINGS = ((5, 2), (10, 2))


def load_step03(asset: str, edge: str):
    path = V2 / "assets" / asset / "edges" / edge / "research" / "03_generate_oof_predictions.py"
    spec = importlib.util.spec_from_file_location(f"step03_{asset}_{edge}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def daily_sharpe(exit_time: pd.Series, pnl: np.ndarray) -> float:
    d = pd.Series(pnl, index=pd.to_datetime(exit_time, utc=True, format="ISO8601").dt.floor("D")).groupby(level=0).sum()
    if d.empty:
        return float("nan")
    d = d.reindex(pd.date_range(d.index.min(), d.index.max(), freq="D"), fill_value=0.0)
    sd = d.std(ddof=1)
    return float(d.mean() / sd * np.sqrt(252)) if sd > 0 else float("nan")


def economics(p_cal: np.ndarray, df: pd.DataFrame, thr: float, p_be: float, c_q: float, rr: np.ndarray) -> dict:
    ok = np.isfinite(p_cal)
    m = np.zeros(len(df))
    comp = payoff_aware_afml_components(np.clip(np.where(ok, p_cal, 0.5), 1e-6, 1 - 1e-6), rr, break_even_probability=p_be)
    m[ok] = np.asarray(comp["afml_bet_size"])[ok]
    appr = ok & (p_cal >= thr) & (m > 0)
    r = df["executed_r"].to_numpy(float)
    pnl = np.where(appr, m / c_q * r, 0.0)
    return {"n_approved": int(appr.sum()), "mean_r_approved": float(r[appr].mean()) if appr.any() else float("nan"),
            "net_risk_units": float(pnl.sum()), "daily_sharpe": daily_sharpe(df["exit_time"][appr], pnl[appr]) if appr.any() else float("nan")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    asset, edge = a.edge.split("/")
    t0 = time.time()
    mod = load_step03(asset, edge)
    feats = mod.load_selected_features()
    best = mod.load_json(mod.BEST_PARAMS_PATH)
    df = mod.load_dataset(feats)
    x, y = df[feats].copy(), df[mod.TARGET_COL].copy()
    t1 = pd.Series(mod._label_end_times(df), index=df.index)
    cwm = best.get("class_weight_mode", "balanced")
    manifest = json.loads((V2 / "portfolio_live" / "live_manifest.json").read_text(encoding="utf-8"))
    me = next(e for e in manifest["selected_edges"] if e["edge_id"] == a.edge)
    thr, c_q = float(me["final_live_threshold"]), float(me["risk_caps"]["afml_concurrency_divisor"])
    p_be = float(me["live_payoff_context"]["empirical_break_even_probability"])
    root = V2 / "assets" / asset / "edges" / edge / "artifacts" / "calibration"
    calj = json.loads(next(root.glob("probability_calibrator_sigmoid_side_*.json")).read_text(encoding="utf-8"))
    clip = float(calj.get("probability_clip", 1e-6))

    def calibrate(raw: np.ndarray) -> np.ndarray:
        q = np.clip(raw, clip, 1 - clip)
        return 1.0 / (1.0 + np.exp(-(calj["intercept"] + calj["coef"] * np.log(q / (1 - q)))))

    rr = (df["take_profit_distance_price"] / df["stop_distance_price"]).to_numpy(float) if "take_profit_distance_price" in df else np.full(len(df), 2.0)
    cal = pd.read_csv(next(root.glob("*_calibrated_oof_predictions.csv")), usecols=["live_calibrated_probability"])
    ref = economics(cal["live_calibrated_probability"].to_numpy(float)[: len(df)], df, thr, p_be, c_q, rr) if len(cal) == len(df) else None
    out = {"edge": a.edge, "rows": int(len(df)), "live_threshold_from_allocator": thr, "c_q": c_q, "p_be": p_be,
           "caveat": "calibrator and live threshold fitted on the averaged OOF path (unchanged for this test)",
           "reference_single_averaged_path": ref, "settings": {}}
    print(f"[{a.edge}] rows {len(df)} thr {thr} reference {ref}", flush=True)
    for n_folds, k in SETTINGS:
        cv = CombinatorialPurgedCV(n_folds=n_folds, n_test_folds=k)
        pid = cv.get_path_ids()
        paths = np.full((cv.n_test_paths, len(df)), np.nan)
        for s, (train_idx, test_parts) in enumerate(cv.split(x)):
            test_all = mod.flatten_cv_indices(test_parts)
            tr = mod.flatten_cv_indices(train_idx)
            tr = mod.purge_train_overlaps(tr, test_all, df.index, t1)
            tr = mod.apply_embargo(tr, test_all, len(df), mod.embargo_size_for(len(df)))
            if len(tr) == 0 or y.iloc[tr].nunique() < 2:
                continue
            w = mod.make_train_weights(df=df, y=y, train_idx=tr, class_weight_mode=cwm)
            model = mod.make_bagged_model(best, df.iloc[tr], x.iloc[tr], y.iloc[tr], w)
            for j, part in enumerate(test_parts):
                part = np.atleast_1d(part).astype(int)
                paths[pid[s, j], part] = mod.get_positive_class_proba(model, x.iloc[part])
            print(f"[{a.edge}] N={n_folds} k={k} split {s + 1}/{cv.n_splits} elapsed {time.time() - t0:.0f}s", flush=True)
        per = [economics(calibrate(paths[p]), df, thr, p_be, c_q, rr) for p in range(cv.n_test_paths)]
        sh = np.array([e["daily_sharpe"] for e in per])
        avg = economics(calibrate(np.nanmean(paths, axis=0)), df, thr, p_be, c_q, rr)
        out["settings"][f"N{n_folds}_k{k}"] = {
            "n_splits": int(cv.n_splits), "n_paths": int(cv.n_test_paths), "train_share": 1 - k / n_folds,
            "paths": per, "sharpe_mean": float(np.nanmean(sh)), "sharpe_std": float(np.nanstd(sh, ddof=1)),
            "sharpe_min": float(np.nanmin(sh)), "sharpe_max": float(np.nanmax(sh)),
            "share_paths_sharpe_le_0": float(np.mean(sh <= 0)), "averaged_paths_this_run": avg}
        print(f"[{a.edge}] N={n_folds}: Sharpe paths {np.round(sh, 3).tolist()} averaged {avg}", flush=True)
    out["elapsed_s"] = round(time.time() - t0, 1)
    Path(a.out).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
