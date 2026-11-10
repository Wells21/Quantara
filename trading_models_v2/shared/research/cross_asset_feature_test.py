"""Cross-asset feature experiment (owner-approved 2026-10-26; pre-registered in docs/mldp/AFML_MLAM_WALKTHROUGH.md
"Cross-asset feature experiment"). Research period only; the sealed holdout is never read. Common window: rows at or
before the earliest research end of the 10 assets (XAUUSD, 2026-04-07), so every driver has data.

--mode entry  : the entry meta-label on every resolved research event of the edge (the population the entry meta-model
                is trained on). Baseline = the edge's selected features; candidate = baseline + cross-asset features
                (shared/research/cross_asset_features.py); diagnostic = cross-asset features alone. Label = int(label_r > 0)
                (mid-path label); span [event bar, label_t1]; weights = average uniqueness (Snippet 4.4).
--mode intrade: the Ch.10 plan-step-A target (remaining mid R) on live-approved trades only; baseline = trade state
                (STATE_COLS); candidate = state + cross-asset features at each in-trade bar close.
Model: AFML s.6.4 pp.98-99 option 3 + option 2 (bagged entropy / squared-error trees, max_features sqrt, class_weight
balanced for classification, min_weight_fraction_leaf 0.05, 1000 trees, max_samples = avgU); purged + embargoed 10-fold.
GO (pre-registered): candidate beats baseline on pooled OOS weighted log loss (entry) / MSE (intrade), with the
95 % bootstrap CI of the gain above zero (entry: calendar-month blocks; intrade: trades as clusters).

usage (from trading_models_v2, venv312):
  python -m shared.research.cross_asset_feature_test --edge HK/drawdownhazardcontinuation_short --mode entry --out <json>
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import BaggingClassifier, BaggingRegressor
from sklearn.metrics import roc_auc_score
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor

from shared.features.clustered_feature_importance import purged_kfold_splits
from shared.models.sequential_bootstrap import get_avg_uniqueness
from shared.research import cross_asset_features as xa
from shared.research.ch10_intrade_forecast_test import _ts, build_rows
from shared.research.ch10_remaining_return_test import STATE_COLS, add_trade_state

V2 = Path(__file__).resolve().parents[2]
N_SPLITS, N_EST, N_BOOT, SEED, EPS = 10, 1000, 1000, 20261026, 1e-15
CLF = dict(criterion="entropy", max_features="sqrt", class_weight="balanced", min_weight_fraction_leaf=0.05)
REG = dict(max_features="sqrt", min_weight_fraction_leaf=0.05)


def common_end() -> int:
    return min(xa.research_end(a) for a in xa.ASSETS)


def entry_rows(asset: str, edge: str):
    root = V2 / "assets" / asset / "edges" / edge
    feats = json.loads((root / "artifacts" / "selected_features.json").read_text(encoding="utf-8"))["selected_features"]
    res = next((root / "artifacts" / "research_dataset").glob("*_mldp_execution_resolution.csv"))
    d = pd.read_csv(res, usecols=["Datetime", "label_r", "label_t1", "execution_status"] + feats)
    d = d[d.execution_status.eq("resolved") & d.label_r.notna()].copy()
    d["t0_ns"], d["t1_ns"] = _ts(d.Datetime), _ts(d.label_t1)
    d = d.sort_values("t0_ns").reset_index(drop=True)
    fm = pd.read_csv(next((root / "artifacts" / "research_dataset").glob("*_feature_matrix.csv")), usecols=["Datetime"])
    bar_t = np.sort(_ts(fm.Datetime))
    d["bar"] = np.searchsorted(bar_t, d.t0_ns.to_numpy(), side="left")
    d["t1_bar"] = np.minimum(np.searchsorted(bar_t, d.t1_ns.to_numpy(), side="left"), len(bar_t) - 1)
    d["y"] = (d.label_r > 0).astype(int)
    d["trade"] = np.arange(len(d))
    return d, feats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", required=True)
    ap.add_argument("--mode", choices=["entry", "intrade"], required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    asset, edge = args.edge.split("/")
    started = time.time()
    end = common_end()
    if args.mode == "entry":
        rows, base = entry_rows(asset, edge)
    else:
        rows, _, _ = build_rows(asset, edge, approved_only=True)
        rows = add_trade_state(rows, asset, edge, approved_only=True)
        rows["y"] = rows.r_rem
        base = list(STATE_COLS)
    rows = rows[rows.t0_ns <= end].sort_values(["t0_ns", "trade"]).reset_index(drop=True)
    X = xa.build(asset, rows.t0_ns.to_numpy())
    xcols = list(X.columns)
    rows = pd.concat([rows, X], axis=1)
    n0 = len(rows)
    rows = rows.dropna(subset=base + xcols).reset_index(drop=True)
    n = len(rows)
    print(f"[{args.edge} {args.mode}] rows {n} (dropped {n0 - n} warm-up/NaN), {len(xcols)} cross-asset features, "
          f"elapsed {time.time() - started:.0f}s", flush=True)
    bar_of = rows.bar.to_numpy()
    t1b = np.maximum(rows.t1_bar.to_numpy(), bar_of)
    w = get_avg_uniqueness(bar_of, t1b, int(t1b.max() + 1), np.arange(n))
    avg_u = float(np.clip(w.mean(), 1e-4, 1.0))
    w = w / w.mean()
    splits = purged_kfold_splits(rows.t0_ns.to_numpy(), rows.t1_ns.to_numpy(), N_SPLITS, max(100, int(0.01 * n)))
    y = rows.y.to_numpy()
    sets = {"baseline": base, "candidate": base + xcols, "cross_only": xcols}
    pred = {k: np.full(n, np.nan) for k in sets}
    for k, (tr, te) in enumerate(splits):
        for name, cols in sets.items():
            if args.mode == "entry":
                m = BaggingClassifier(DecisionTreeClassifier(**CLF), n_estimators=N_EST, max_samples=avg_u, random_state=SEED + k, n_jobs=1)
                m.fit(rows[cols].to_numpy()[tr], y[tr], sample_weight=w[tr])
                pred[name][te] = m.predict_proba(rows[cols].to_numpy()[te])[:, list(m.classes_).index(1)]
            else:
                m = BaggingRegressor(DecisionTreeRegressor(**REG), n_estimators=N_EST, max_samples=avg_u, random_state=SEED + k, n_jobs=1)
                m.fit(rows[cols].to_numpy()[tr], y[tr], sample_weight=w[tr])
                pred[name][te] = m.predict(rows[cols].to_numpy()[te])
        print(f"[{args.edge} {args.mode}] fold {k + 1}/{N_SPLITS} elapsed {time.time() - started:.0f}s", flush=True)

    if args.mode == "entry":
        def loss(p):
            p = np.clip(p, EPS, 1 - EPS)
            return -(y * np.log(p) + (1 - y) * np.log(1 - p)) * w
        mon = pd.to_datetime(rows.t0_ns, unit="ns", utc=True).dt.strftime("%Y-%m").to_numpy()
        _, gid = np.unique(mon, return_inverse=True)
    else:
        def loss(p):
            return (y - p) ** 2 * w
        _, gid = np.unique(rows.trade.to_numpy(), return_inverse=True)
    L = {k: loss(v) for k, v in pred.items()}
    G = gid.max() + 1
    gL = {k: np.bincount(gid, weights=v, minlength=G) for k, v in L.items()}
    gW = np.bincount(gid, weights=w, minlength=G)
    point = float((gL["baseline"].sum() - gL["candidate"].sum()) / gW.sum())
    rng = np.random.default_rng(SEED)
    boot = []
    for _ in range(N_BOOT):
        pick = rng.integers(0, G, G)
        boot.append((gL["baseline"][pick].sum() - gL["candidate"][pick].sum()) / gW[pick].sum())
    ci = [float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))]
    pooled = {k: float(v.sum() / w.sum()) for k, v in L.items()}
    extra = {}
    if args.mode == "entry":
        extra["auc"] = {k: float(roc_auc_score(y, v, sample_weight=w)) for k, v in pred.items()}
    else:
        extra["r2_vs_zero"] = {k: float(1 - pooled[k] / float(np.sum(y ** 2 * w) / w.sum())) for k in pred}
    report = {"edge": args.edge, "mode": args.mode, "rows": n, "common_end": str(pd.Timestamp(end, tz="UTC")),
              "n_baseline_features": len(base), "n_cross_asset_features": len(xcols), "avg_uniqueness": avg_u,
              "pooled_oos_loss": pooled, **extra, "gain_candidate_over_baseline": point, "gain_95ci": ci,
              "GO": bool(ci[0] > 0), "elapsed_s": round(time.time() - started, 1)}
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
