"""AFML Ch.10 walkthrough, test step 2 (owner-approved 2026-10-26): go / no-go for s.10.6 updated forecasts.

AFML s.10.6 (p.145) sizes an open bet from forecasts of its outcome formed while it is open, E_{t_j,0}[p_{t_i,1}],
t_j,0 <= t_i,1. Quantara's meta-model only forecasts at the candidate-event bar. This test asks whether a forecast made
at a later bar close, while the trade is open, carries information about the trade's outcome beyond what price
location already implies. Research period only; the sealed holdout is NOT read.

Rows: every bar close t_j strictly inside an open trade (entry_time < t_j < label_t1), one row per (trade, bar).
Label: the trade's mid-path meta label, int(label_r > 0) (barriers fixed, so the remaining outcome is the trade's own).
Row span: [t_j, label_t1] (purging and uniqueness, AFML Ch.4/7).
Forecasts compared out of sample (purged + embargoed k-fold, AFML Snippets 7.1-7.3, contiguous folds):
  (a) random walk : s = (side (close_j - entry_mid) + SL) / (TP + SL), the driftless chance of TP before SL (no model);
  (b) state model : bagged trees on [s, remaining-time fraction, the trade's own OOF entry probability];
  (c) full model  : (b) + the edge's selected features at bar j (the information an updated forecast could add).
Model = AFML s.6.4 pp.98-99 option 3 (+ option 2 early stopping): BaggingClassifier(DecisionTree(entropy,
max_features='sqrt' (the book's 'auto'), class_weight='balanced', min_weight_fraction_leaf=0.05), n_estimators=1000,
max_samples=avgU); sample weight = average uniqueness of the row span (AFML Snippet 4.2 / 4.4).
Pre-registered GO rule (docs/mldp/AFML_MLAM_WALKTHROUGH.md s.4J.2, written before the run): an edge is GO when the
full model (c) beats the state model (b) on pooled OOS weighted log loss AND beats the random walk (a) on OOS
weighted AUC, both with the trade-clustered bootstrap 95% interval of the improvement above zero.

usage (from trading_models_v2, venv312):
  python -m shared.research.ch10_intrade_forecast_test --edge XAUUSD/rangestructure_long --out <json>
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import BaggingClassifier
from sklearn.metrics import roc_auc_score
from sklearn.tree import DecisionTreeClassifier

from shared.features.clustered_feature_importance import purged_kfold_splits
from shared.models.sequential_bootstrap import get_avg_uniqueness

V2 = Path(__file__).resolve().parents[2]
N_SPLITS = 10
N_ESTIMATORS = 1000
N_BOOT = 1000
SEED = 20261026
EPS = 1e-15
TREE = dict(criterion="entropy", max_features="sqrt", class_weight="balanced", min_weight_fraction_leaf=0.05)


def _ts(s: pd.Series) -> np.ndarray:
    return pd.to_datetime(s, utc=True, format="ISO8601").to_numpy(dtype="datetime64[ns]").astype(np.int64)


def _ll(y: np.ndarray, p: np.ndarray, w: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPS, 1 - EPS)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p)) * w


def event_mask(ev: pd.DataFrame, edge_id: str, approved_only: bool) -> pd.Series:
    """Resolved events with an OOF probability; approved_only keeps the live-traded ones (live calibrated p >= the live
    threshold and a positive AFML size) -- the population an in-trade model would see live (owner 2026-10-26)."""
    keep = ev.execution_status.eq("resolved") & ev.oof_prob_raw.notna()
    if approved_only:
        manifest = json.loads((V2 / "portfolio_live" / "live_manifest.json").read_text(encoding="utf-8"))
        thr = next(e["final_live_threshold"] for e in manifest["selected_edges"] if e["edge_id"] == edge_id)
        keep &= (ev.live_calibrated_probability >= thr) & (ev.afml_bet_size > 0)
    return keep


def build_rows(asset: str, edge: str, approved_only: bool = False) -> tuple[pd.DataFrame, list[str], dict]:
    root = V2 / "assets" / asset / "edges" / edge
    cal = next((root / "artifacts" / "calibration").glob("*_calibrated_oof_predictions.csv"))
    ev = pd.read_csv(cal, usecols=["entry_time", "vertical_deadline", "label_r", "label_t1", "side", "entry_bid", "entry_ask",
                                   "take_profit_distance_price", "stop_distance_price", "oof_prob_raw", "execution_status",
                                   "live_calibrated_probability", "afml_bet_size"])
    ev = ev[event_mask(ev, f"{asset}/{edge}", approved_only)].reset_index(drop=True)
    feats = json.loads((root / "artifacts" / "selected_features.json").read_text(encoding="utf-8"))["selected_features"]
    fm = pd.read_csv(next((root / "artifacts" / "research_dataset").glob("*_feature_matrix.csv")), usecols=["Datetime", "High", "Low", "Close"] + feats)
    fm = fm.assign(t_ns=_ts(fm.Datetime))
    fm = fm.sort_values("t_ns").reset_index(drop=True)
    bar_t = fm.t_ns.to_numpy()
    e_ns, t1_ns, v_ns = _ts(ev.entry_time), _ts(ev.label_t1), _ts(ev.vertical_deadline)
    lo = np.searchsorted(bar_t, e_ns, side="right")   # first bar close strictly after entry
    hi = np.searchsorted(bar_t, t1_ns, side="left")   # bar closes strictly before the exit
    parts = []
    for i in range(len(ev)):
        if hi[i] <= lo[i]:
            continue
        j = np.arange(lo[i], hi[i])
        parts.append(pd.DataFrame({"trade": i, "bar": j}))
    rows = pd.concat(parts, ignore_index=True)
    i, j = rows.trade.to_numpy(), rows.bar.to_numpy()
    entry_mid = ((ev.entry_bid + ev.entry_ask) / 2).to_numpy()[i]
    side, tp, sl = ev.side.to_numpy()[i], ev.take_profit_distance_price.to_numpy()[i], ev.stop_distance_price.to_numpy()[i]
    close = fm.Close.to_numpy()[j]
    rows["s_random_walk"] = np.clip((side * (close - entry_mid) + sl) / (tp + sl), 0.0, 1.0)
    rows["time_left_frac"] = (v_ns[i] - bar_t[j]) / (v_ns[i] - e_ns[i])
    rows["entry_oof_prob"] = ev.oof_prob_raw.to_numpy()[i]
    rows["y"] = (ev.label_r.to_numpy()[i] > 0).astype(int)
    rows["t0_ns"] = bar_t[j]
    rows["t1_ns"] = t1_ns[i]
    rows["t1_bar"] = np.minimum(np.searchsorted(bar_t, t1_ns[i], side="left"), len(bar_t) - 1)
    rows = pd.concat([rows, pd.DataFrame(fm[feats].to_numpy()[j], columns=feats)], axis=1)
    # drop rows at or after the bar in which a barrier was already touched (bar High/Low; label_t1 is on the bar clock)
    # the edge's own research bars (feature matrix OHLC; JPN research data predates the 2026-10-23 bar rebuild)
    hi, lo_px = fm.High.to_numpy(float), fm.Low.to_numpy(float)
    touched = np.zeros(len(rows), dtype=bool)
    order = np.argsort(i, kind="stable")
    for idx in np.split(order, np.flatnonzero(np.diff(i[order])) + 1):
        k, jj = i[idx[0]], j[idx]
        f0 = lo[k]
        fav = (hi if side[idx[0]] > 0 else lo_px)[f0: jj.max() + 1]
        adv = (lo_px if side[idx[0]] > 0 else hi)[f0: jj.max() + 1]
        hit = np.maximum.accumulate((side[idx[0]] * (fav - entry_mid[idx[0]]) >= tp[idx[0]])
                                    | (side[idx[0]] * (adv - entry_mid[idx[0]]) <= -sl[idx[0]]))
        touched[idx] = hit[jj - f0]
    n_touched = int(touched.sum())
    rows = rows[~touched].reset_index(drop=True)
    n_before = len(rows)
    rows = rows.dropna(subset=feats).sort_values(["t0_ns", "trade"]).reset_index(drop=True)
    info = {"n_trades": int(len(ev)), "n_trades_with_rows": int(rows.trade.nunique()), "n_rows": int(len(rows)),
            "rows_dropped_after_barrier_touch": n_touched,
            "rows_dropped_nan_features": int(n_before - len(rows)), "n_features": len(feats),
            "share_positive": float(rows.y.mean())}
    return rows, feats, info


def fit_predict(xtr, ytr, wtr, xte, avg_u: float, seed: int) -> np.ndarray:
    clf = BaggingClassifier(DecisionTreeClassifier(**TREE), n_estimators=N_ESTIMATORS, max_samples=avg_u,
                            random_state=seed, n_jobs=1)
    clf.fit(xtr, ytr, sample_weight=wtr)
    return clf.predict_proba(xte)[:, list(clf.classes_).index(1)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    asset, edge = args.edge.split("/")
    started = time.time()
    rows, feats, info = build_rows(asset, edge)
    n = len(rows)
    bar_of = rows.bar.to_numpy()
    n_bars = int(max(bar_of.max(), rows.t1_bar.max()) + 1)
    w = get_avg_uniqueness(bar_of, np.maximum(rows.t1_bar.to_numpy(), bar_of), n_bars, np.arange(n))
    avg_u = float(np.clip(w.mean(), 1e-4, 1.0))
    w = w / w.mean()
    embargo = max(100, int(0.01 * n))
    splits = purged_kfold_splits(rows.t0_ns.to_numpy(), rows.t1_ns.to_numpy(), N_SPLITS, embargo)
    y = rows.y.to_numpy()
    state_cols = ["s_random_walk", "time_left_frac", "entry_oof_prob"]
    xb, xc = rows[state_cols].to_numpy(), rows[state_cols + feats].to_numpy()
    pa = rows.s_random_walk.to_numpy()
    pb, pc = np.full(n, np.nan), np.full(n, np.nan)
    folds = []
    for k, (tr, te) in enumerate(splits):
        pb[te] = fit_predict(xb[tr], y[tr], w[tr], xb[te], avg_u, SEED + k)
        pc[te] = fit_predict(xc[tr], y[tr], w[tr], xc[te], avg_u, SEED + 100 + k)
        rec = {"fold": k, "n_train": int(len(tr)), "n_test": int(len(te))}
        if len(np.unique(y[te])) == 2:
            for name, p in (("a", pa), ("b", pb), ("c", pc)):
                rec[f"auc_{name}"] = float(roc_auc_score(y[te], p[te], sample_weight=w[te]))
            for name, p in (("b", pb), ("c", pc)):
                rec[f"nll_{name}"] = float(_ll(y[te], p[te], w[te]).sum() / w[te].sum())
        folds.append(rec)
        print(f"[{args.edge}] fold {k + 1}/{N_SPLITS} {rec} elapsed {time.time() - started:.0f}s", flush=True)

    # trade-clustered bootstrap of the pooled OOS improvements
    rng = np.random.default_rng(SEED)
    trades = rows.trade.to_numpy()
    order = np.argsort(trades, kind="stable")
    cuts = np.flatnonzero(np.diff(trades[order])) + 1
    groups = np.split(order, cuts)
    ll_b, ll_c = _ll(y, pb, w), _ll(y, pc, w)

    def stats(idx: np.ndarray) -> tuple[float, float, float]:
        d_ll = (ll_b[idx].sum() - ll_c[idx].sum()) / w[idx].sum()            # > 0: (c) better than (b)
        auc_c = roc_auc_score(y[idx], pc[idx], sample_weight=w[idx])
        d_auc_ca = auc_c - roc_auc_score(y[idx], pa[idx], sample_weight=w[idx])  # > 0: (c) better than (a)
        d_auc_cb = auc_c - roc_auc_score(y[idx], pb[idx], sample_weight=w[idx])
        return d_ll, d_auc_ca, d_auc_cb

    point = stats(np.arange(n))
    boot = []
    for _ in range(N_BOOT):
        pick = rng.integers(0, len(groups), len(groups))
        idx = np.concatenate([groups[g] for g in pick])
        if len(np.unique(y[idx])) == 2:
            boot.append(stats(idx))
    boot = np.array(boot)
    ci = {name: [float(np.percentile(boot[:, k], 2.5)), float(np.percentile(boot[:, k], 97.5))]
          for k, name in enumerate(["nll_improvement_c_over_b", "auc_gain_c_over_a", "auc_gain_c_over_b"])}
    pooled = {
        "auc_a_random_walk": float(roc_auc_score(y, pa, sample_weight=w)),
        "auc_b_state": float(roc_auc_score(y, pb, sample_weight=w)),
        "auc_c_full": float(roc_auc_score(y, pc, sample_weight=w)),
        "nll_b_state": float(ll_b.sum() / w.sum()),
        "nll_c_full": float(ll_c.sum() / w.sum()),
        "nll_improvement_c_over_b": point[0], "auc_gain_c_over_a": point[1], "auc_gain_c_over_b": point[2],
    }
    go = ci["nll_improvement_c_over_b"][0] > 0 and ci["auc_gain_c_over_a"][0] > 0
    report = {"edge": args.edge, "test": "AFML Ch.10 walkthrough step 2: in-trade updated-forecast information (research period only)",
              "rows": info, "avg_uniqueness": avg_u, "embargo_rows": embargo, "n_splits": N_SPLITS,
              "n_estimators": N_ESTIMATORS, "tree": TREE, "pooled_oos": pooled, "bootstrap_95ci": ci,
              "n_boot_valid": int(len(boot)), "verdict": "GO" if go else "NO-GO", "folds": folds,
              "elapsed_s": round(time.time() - started, 1)}
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("edge", "rows", "pooled_oos", "bootstrap_95ci", "verdict")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
