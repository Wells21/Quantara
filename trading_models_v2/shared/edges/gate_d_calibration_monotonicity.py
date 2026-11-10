"""
Gate D v4.1 (D078, owner approved 2026-11-04): does the meta-model do what meta-labelling is for?

Canonical rule: docs/mldp/rules/GATE_D_V4_D078.md. AFML names two jobs for the secondary (meta) model:
  - act or pass: "Meta-labeling will increase your F1-score by filtering out the false positives ... Its purpose is to
    determine whether we should act or pass on the opportunity" (s.3.7, pp.52-53); judged by accuracy / precision /
    recall / F1 (s.14.8, pp.206-207);
  - size the bet from its probability (p.53; Ch.10), where probability quality is judged by log loss, because
    "investment strategies profit from predicting the right label with high confidence" (s.9.4, p.134).
v3 (AUC > 0.5, one-sided Mann-Whitney) tested ranking only -- necessary, but neither job. It stays as a diagnostic.

Both tests run on the Step 03 out-of-fold predictions (artifacts/oof/<asset>_<edge>_oof_predictions.csv), at the edge
level, with no Step 04 threshold and no allocator threshold. One-sided 95 % (the project standard, as D075).

Cross-fitted calibration (shared by D1 and D2). The events sorted by time are split into 10 contiguous blocks. For
  each block the live calibrator's method -- Platt sigmoid on logit(raw), fit_sigmoid_params on all CPCV paths stacked,
  WITH the AFML return-attribution weights, exactly as Step 04's fit_final_live_sigmoid (D082) -- is fitted on the other
  blocks only and applied to the held-out block's path forecasts.
  - Purged: training events whose [t0, label t1] overlaps the block's span are dropped.
  - Embargoed: max(100, ceil(1 % of rows)) events after the block are dropped.
  - b = the AFML-weighted positive rate of the same training rows.
  - P(win) of an event = the mean of its calibrated path forecasts.

D1 act or pass (s.3.7, s.14.8). Primary model = take every signal: precision = base rate, recall = 1,
  F1_primary = 2b / (1 + b). Meta acts when the cross-fitted calibrated P(win) > b (owner 2026-11-04; replaces raw p > 0.5,
  which acted on 74-100 % of events because the return weights correlate with the label). F1_meta = 2 TP / (2 TP + FP +
  FN), unweighted counts (s.14.8). Delta = F1_meta - F1_primary on the same events. Week-block bootstrap: ISO weeks
  resampled with replacement, 10,000 draws, seed 20261104, both F1 recomputed on each draw. PASS if the 5th percentile of
  Delta > 0.

D2 sizing (s.9.4, Ch.10). Weighted throughout (owner 2026-11-04; AFML s.4.6 pp.68-69, Snippet 7.4, s.9.4 pp.134-135):
  d_i = log loss(b) - mean over the event's CPCV paths of log loss(calibrated). The weighted calibrator and base rate
  are as above; the loss is weighted by AFML Ch.4 return attribution (Snippet 4.10, class balance excluded). PASS if
  the weighted mean of d_i - 1.645 x week-clustered SE > 0.

Gate D PASS = D1 PASS and D2 PASS. Reported, not gating: AUC + Mann-Whitney p (v3), log loss with the in-sample live
calibrator and with raw probabilities, Kendall tau(raw_probability, quote_executable_r), F1 / precision / recall.

This module is diagnostics for the edge research lifecycle only. It does not select, endorse, or write any live
trading threshold.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso
from scipy import stats

V2_ROOT = Path(__file__).resolve().parents[2]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

GATE_D_VERSION = "v4.1_D078_f1_logloss_weighted"
BASE_ALPHA = 0.05
Z_ONE_SIDED = 1.645
MIN_CLASS_SIZE = 10
BOOTSTRAP_DRAWS = 10_000
SEED = 20261104
N_BLOCKS = 10
EMBARGO_MIN, EMBARGO_PCT = 100, 0.01
EPS = 1e-6

TIME_COL = "Datetime"
RAW_PROBABILITY_COL = "raw_probability"
META_LABEL_COL = "quote_executable_meta_label"
REALIZED_R_COL = "quote_executable_r"
LABEL_T1_COL, GROSS_T1_COL = "label_t1", "gross_t1"
ELIGIBLE_COL = "mldp_target_eligible"


class GateDError(RuntimeError):
    """Raised when Gate D cannot be evaluated (bad input, not a verdict)."""


def _f1(tp: np.ndarray, fp: np.ndarray, fn: np.ndarray) -> np.ndarray:
    den = 2 * tp + fp + fn
    return np.where(den > 0, 2 * tp / np.where(den > 0, den, 1), 0.0)


def _weeks(ts: pd.Series) -> np.ndarray:
    return to_datetime_iso(ts, utc=True).dt.tz_convert(None).dt.to_period("W").astype(str).to_numpy()


def cross_fit_calibration(frame: pd.DataFrame, path_cols: list[str], weights: np.ndarray, *,
                          n_blocks: int = N_BLOCKS) -> dict[str, Any]:
    """Cross-fitted calibrated probabilities (D078 v4.1): per contiguous time block, the live calibrator's method -- Platt
    sigmoid on logit(raw), fitted on all CPCV paths stacked WITH the AFML return-attribution weights (D082) -- fitted on
    the other blocks only (purged + embargoed) and applied to the block's path forecasts. b = the weighted positive rate
    of the same training rows."""
    from shared.calibration.fit_sigmoid_calibrator_and_thresholds import apply_sigmoid_params, fit_sigmoid_params

    y = frame[META_LABEL_COL].astype(int).to_numpy()
    t0 = to_datetime_iso(frame[TIME_COL], utc=True).to_numpy(dtype="datetime64[ns]").astype("int64")
    t1s = frame[LABEL_T1_COL] if LABEL_T1_COL in frame else frame[GROSS_T1_COL]
    if GROSS_T1_COL in frame:
        t1s = t1s.fillna(frame[GROSS_T1_COL])
    t1 = to_datetime_iso(t1s, utc=True).to_numpy(dtype="datetime64[ns]").astype("int64")
    paths = frame[path_cols].to_numpy(dtype="float64")
    w = weights / weights.mean()
    cal = np.full(paths.shape, np.nan)
    b_vec = np.full(len(y), np.nan)
    blocks = []
    for train, test in _cross_fit_blocks(t0, t1, n_blocks):
        tp = paths[train]
        ok = np.isfinite(tp)
        params = fit_sigmoid_params(np.clip(tp[ok], EPS, 1 - EPS), np.repeat(y[train][:, None], tp.shape[1], 1)[ok],
                                    sample_weight=np.repeat(w[train][:, None], tp.shape[1], 1)[ok])
        b = float(np.sum(w[train] * y[train]) / np.sum(w[train]))
        hp = paths[test]
        cal[test] = np.where(np.isfinite(hp), apply_sigmoid_params(np.clip(np.nan_to_num(hp, nan=0.5), EPS, 1 - EPS), params), np.nan)
        b_vec[test] = b
        blocks.append({"rows_test": int(len(test)), "rows_train": int(len(train)), "base_rate_train_weighted": b,
                       "coef": params.get("coef"), "intercept": params.get("intercept"), "method": params.get("method")})
    return {"cal_paths": cal, "p_event": np.nanmean(cal, axis=1), "b": b_vec, "w": w, "y": y, "blocks": blocks}


def d1_act_or_pass(y: np.ndarray, act: np.ndarray, weeks: np.ndarray, *, draws: int = BOOTSTRAP_DRAWS,
                   seed: int = SEED) -> dict[str, Any]:
    """F1 (unweighted counts, AFML s.14.8) of acting on `act` vs the primary model (act on everything)."""
    act = np.asarray(act, dtype=bool)
    codes, wk = np.unique(weeks, return_inverse=True)
    nw = len(codes)
    cnt = np.bincount(wk, minlength=nw).astype(float)
    pos = np.bincount(wk, weights=y, minlength=nw)
    tp_w = np.bincount(wk, weights=(act & (y == 1)).astype(float), minlength=nw)
    fp_w = np.bincount(wk, weights=(act & (y == 0)).astype(float), minlength=nw)

    def stats_for(c):          # c: (k, nw) week multiplicities
        n, P, TP, FP = c @ cnt, c @ pos, c @ tp_w, c @ fp_w
        f1m = _f1(TP, FP, P - TP)
        b = P / n
        f1p = 2 * b / (1 + b)
        return f1m, f1p, TP / np.maximum(TP + FP, 1e-12), TP / np.maximum(P, 1e-12)

    f1m, f1p, prec, rec = (float(v[0]) for v in stats_for(np.ones((1, nw))))
    rng = np.random.default_rng(seed)
    deltas = np.empty(draws)
    for s in range(0, draws, 1000):
        k = min(1000, draws - s)
        c = rng.multinomial(nw, np.full(nw, 1.0 / nw), size=k).astype(float)
        a, b_, _, _ = stats_for(c)
        deltas[s:s + k] = a - b_
    lo = float(np.quantile(deltas, 0.05))
    return {"decision": "act when the cross-fitted calibrated P(win) > the weighted base rate b (approved 2026-11-04)",
            "n_events": int(len(y)), "n_weeks": int(nw),
            "base_rate": float(y.mean()), "acted_share": float(act.mean()),
            "f1_primary": f1p, "f1_meta": f1m, "precision_meta": prec, "recall_meta": rec,
            "precision_primary": float(y.mean()), "delta_f1": f1m - f1p,
            "bootstrap": {"draws": draws, "seed": seed, "unit": "ISO week", "delta_p05": lo,
                          "delta_p50": float(np.median(deltas)), "delta_p95": float(np.quantile(deltas, 0.95))},
            "PASS": bool(lo > 0)}


def _logloss(y: np.ndarray, q: np.ndarray) -> np.ndarray:
    q = np.clip(q, EPS, 1 - EPS)
    return -(y * np.log(q) + (1 - y) * np.log(1 - q))


def _cross_fit_blocks(t0: np.ndarray, t1: np.ndarray, n_blocks: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """(train_idx, test_idx) per contiguous time block, purged on [t0, t1] and embargoed (rows sorted by t0)."""
    n = len(t0)
    emb = max(EMBARGO_MIN, int(np.ceil(EMBARGO_PCT * n)))
    out = []
    for test in np.array_split(np.arange(n), n_blocks):
        lo, hi = t0[test].min(), t1[test].max()
        keep = ~((t0 <= hi) & (t1 >= lo))                       # purge: overlap with the block's span
        keep[test.max() + 1: test.max() + 1 + emb] = False      # embargo after the block
        keep[test] = False
        out.append((np.flatnonzero(keep), test))
    return out


def d2_sizing(frame: pd.DataFrame, cf: dict[str, Any], *, live_calibrator: dict | None = None,
              path_cols: list[str] | None = None) -> dict[str, Any]:
    """Weighted throughout (D078 v4.1, owner 2026-11-04; AFML s.4.6, Snippet 7.4, s.9.4): weighted calibrator (cf),
    weighted base rate, return-attribution-weighted log loss."""
    y, w, cal, b = cf["y"], cf["w"], cf["cal_paths"], cf["b"]
    loss_model = np.nanmean(_logloss(y[:, None], cal), axis=1)
    d = _logloss(y, b) - loss_model
    ok = np.isfinite(d)
    m = float(np.sum(w[ok] * d[ok]) / np.sum(w[ok]))
    wk = _weeks(frame[TIME_COL])[ok]
    g = pd.Series(w[ok] * (d[ok] - m) / np.sum(w[ok])).groupby(wk).sum()
    se = float(np.sqrt((g ** 2).sum()))
    out = {"n_blocks": len(cf["blocks"]), "blocks": cf["blocks"],
           "weights": "AFML Ch.4 return attribution (Snippet 4.10), mean 1; calibrator, base rate and loss all weighted",
           "weighted_logloss_model_crossfit": float(np.sum(w[ok] * loss_model[ok]) / np.sum(w[ok])),
           "weighted_logloss_base_rate": float(np.sum(w[ok] * (loss_model[ok] + d[ok])) / np.sum(w[ok])),
           "mean_d": m, "se_week_clustered": se, "lower_bound_1p645": m - Z_ONE_SIDED * se,
           "PASS": bool(m - Z_ONE_SIDED * se > 0)}
    if path_cols is not None:     # diagnostics (not gating)
        paths = frame[path_cols].to_numpy(dtype="float64")
        raw = np.nanmean(_logloss(y[:, None], np.clip(paths, EPS, 1 - EPS)), axis=1)
        out["diagnostic_weighted_logloss_raw_paths"] = float(np.nansum(w * raw) / np.sum(w[np.isfinite(raw)]))
        if live_calibrator is not None:
            from shared.calibration.sigmoid_afml import apply_sigmoid_calibrator
            lc_p = np.where(np.isfinite(paths), apply_sigmoid_calibrator(np.nan_to_num(paths, nan=0.5), live_calibrator), np.nan)
            lc = np.nanmean(_logloss(y[:, None], lc_p), axis=1)
            out["diagnostic_weighted_logloss_live_calibrator_in_sample"] = float(np.nansum(w * lc) / np.sum(w[np.isfinite(lc)]))
    return out


def _edge_paths(edge_root: Path) -> dict[str, Path]:
    parts = edge_root.resolve().parts
    try:
        asset = parts[parts.index("assets") + 1]
    except (ValueError, IndexError) as exc:
        raise GateDError(f"Could not infer asset from edge-root path: {edge_root}") from exc
    stem = f"{asset.lower()}_{edge_root.resolve().name}"
    bars = V2_ROOT / "assets" / asset / "bars" / "standard_quote_tick" / f"{asset.lower()}_standard_quote_tick_bars.csv"
    cal = sorted((edge_root / "artifacts").glob("**/probability_calibrator_sigmoid_side_*.json"))
    return {"oof": edge_root / "artifacts" / "oof" / f"{stem}_oof_predictions.csv",
            "resolution": edge_root / "artifacts" / "research_dataset" / f"{stem}_mldp_execution_resolution.csv",
            "bars": bars, "live_calibrator": cal[0] if cal else None}


def evaluate_gate_d(oof_predictions_csv: Path, *, edge_id: str, edge_root: Path | None = None) -> dict[str, Any]:
    if not oof_predictions_csv.exists():
        raise GateDError(f"{edge_id}: OOF predictions CSV not found: {oof_predictions_csv}")
    df = pd.read_csv(oof_predictions_csv)
    required = {RAW_PROBABILITY_COL, META_LABEL_COL, TIME_COL}
    missing = required - set(df.columns)
    if missing:
        raise GateDError(f"{edge_id}: OOF predictions CSV missing required columns: {sorted(missing)}")
    if ELIGIBLE_COL in df.columns:
        df = df[df[ELIGIBLE_COL].astype(str).str.lower().isin(["true", "1"])]
    df = df.dropna(subset=[RAW_PROBABILITY_COL, META_LABEL_COL]).sort_values(TIME_COL).reset_index(drop=True)
    n_rows = int(len(df))
    if n_rows == 0:
        raise GateDError(f"{edge_id}: OOF predictions CSV has zero usable rows after dropping NaNs.")
    y = df[META_LABEL_COL].astype(int).to_numpy()
    p = df[RAW_PROBABILITY_COL].to_numpy(dtype="float64")
    n_pos, n_neg = int(y.sum()), int(n_rows - y.sum())
    result: dict[str, Any] = {"gate_d_version": GATE_D_VERSION, "rule": "docs/mldp/rules/GATE_D_V4_D078.md",
                              "edge_id": edge_id, "oof_predictions_csv": str(oof_predictions_csv), "n_rows": n_rows,
                              "n_positive": n_pos, "n_negative": n_neg, "positive_rate": float(n_pos / n_rows)}
    if n_pos < MIN_CLASS_SIZE or n_neg < MIN_CLASS_SIZE:
        result.update(verdict="FAIL", reason=f"INSUFFICIENT_CLASS_SIZE: n_positive={n_pos}, n_negative={n_neg}")
        return result

    path_cols = sorted(c for c in df.columns if c.startswith("cpcv_path_") and c.endswith("_raw_probability"))
    if not path_cols:
        raise GateDError(f"{edge_id}: no cpcv_path_XX_raw_probability columns (D052 Step 03 required)")
    if edge_root is None:
        raise GateDError(f"{edge_id}: edge_root is required for the D2 AFML weights")
    fp = _edge_paths(edge_root)
    from shared.models.afml_sample_weights import build_afml_event_weight_table
    table = build_afml_event_weight_table(fp["resolution"], fp["bars"], time_col=TIME_COL, t1_col=GROSS_T1_COL,
                                          label_t1_col=LABEL_T1_COL, close_col="Close")
    idx = pd.DatetimeIndex(to_datetime_iso(df[TIME_COL], utc=True))
    wcol = "return_attribution_weight"
    w = table[wcol].reindex(idx).to_numpy(dtype="float64")
    if not np.isfinite(w).all():
        raise GateDError(f"{edge_id}: {int((~np.isfinite(w)).sum())} OOF rows have no AFML weight")
    live = json.loads(fp["live_calibrator"].read_text(encoding="utf-8")) if fp["live_calibrator"] else None
    if live is not None and "method" not in live:
        live = None
    cf = cross_fit_calibration(df, path_cols, w)
    act = cf["p_event"] > cf["b"]
    result["D1_act_or_pass"] = d1_act_or_pass(y, act, _weeks(df[TIME_COL]))
    result["D2_sizing"] = d2_sizing(df, cf, live_calibrator=live, path_cols=path_cols)

    passed = bool(result["D1_act_or_pass"]["PASS"] and result["D2_sizing"]["PASS"])
    result["verdict"] = "PASS" if passed else "FAIL"
    d1, d2 = result["D1_act_or_pass"], result["D2_sizing"]
    result["reason"] = (f"D1 {'PASS' if d1['PASS'] else 'FAIL'}: F1 meta {d1['f1_meta']:.4f} vs primary {d1['f1_primary']:.4f}, "
                        f"bootstrap 5th pct delta {d1['bootstrap']['delta_p05']:+.4f}; "
                        f"D2 {'PASS' if d2['PASS'] else 'FAIL'}: log-loss gain {d2['mean_d']:+.5f}, lower bound "
                        f"{d2['lower_bound_1p645']:+.5f}")

    # v3 diagnostics (not gating)
    u_stat, p_value = stats.mannwhitneyu(p[y == 1], p[y == 0], alternative="greater")
    result["diagnostic_v3_auc"] = float(u_stat / (n_pos * n_neg))
    result["diagnostic_v3_mannwhitney_p"] = float(p_value)
    if REALIZED_R_COL in df.columns and df[REALIZED_R_COL].notna().all():
        tau, p_tau = stats.kendalltau(p, df[REALIZED_R_COL].to_numpy())
        result["diagnostic_kendall_tau_raw_probability_vs_realized_r"] = None if np.isnan(tau) else float(tau)
        result["diagnostic_kendall_tau_p_value"] = None if np.isnan(p_tau) else float(p_tau)
    return result


def _default_oof_path(edge_root: Path) -> Path:
    return _edge_paths(edge_root)["oof"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Gate D v4 (D078): meta-model act/pass (F1) and sizing (log loss) tests.")
    parser.add_argument("--edge-root", required=True, help="Path to assets/<ASSET>/edges/<family>_<side>.")
    parser.add_argument("--oof-csv", default=None, help="Override path to the OOF predictions CSV.")
    parser.add_argument("--write-report", action="store_true", help="Write JSON report under <edge-root>/reports/.")
    args = parser.parse_args()

    edge_root = Path(args.edge_root).resolve()
    edge_id = f"{edge_root.parent.parent.name}/{edge_root.name}"
    oof_csv = Path(args.oof_csv).resolve() if args.oof_csv else _default_oof_path(edge_root)
    try:
        result = evaluate_gate_d(oof_csv, edge_id=edge_id, edge_root=edge_root)
    except GateDError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print("=" * 80)
    print(f"GATE D {GATE_D_VERSION} (F1 act/pass + calibrated log loss): {edge_id}")
    print("=" * 80)
    print(f"verdict: {result['verdict']}")
    print(f"reason: {result.get('reason')}")
    for key in ("diagnostic_v3_auc", "diagnostic_v3_mannwhitney_p"):
        if key in result:
            print(f"{key}: {result[key]}")
    if args.write_report:
        report_dir = edge_root / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "gate_d_calibration_monotonicity_report.json"
        report_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"\nReport: {report_path}")
    raise SystemExit(0 if result["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
