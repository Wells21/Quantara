"""
EXPERIMENTAL, NON-GATING: decile-level precision-trend test on raw OOF predictions.

This is a standalone side-by-side comparison against the authoritative Gate D
(shared/edges/gate_d_calibration_monotonicity.py, AUC-ROC/Mann-Whitney on raw
OOF predictions) -- it does NOT modify, replace, or gate on top of that script.
Its verdicts have no effect on any edge's deployability status.

Motivation: AFML Ch.3 frames meta-labeling's purpose as trading recall for
precision -- as the classifier's decision threshold rises, the positives it
still calls should become a purer (higher-precision) set. AUC-ROC/Mann-Whitney
tests a global, full-sample average version of "does the score rank positives
above negatives"; this script tests the more literal, local version of the
same idea: bucket raw OOF predictions into deciles by raw_probability, compute
realized precision (= quote_executable_meta_label positive rate) per decile,
and run a Cochran-Armitage trend test for a significant *increasing* trend in
precision as the decile (and therefore the implied threshold) rises.

Cochran-Armitage trend test (manual implementation, no extra dependency):
  Given ordered groups i=1..k with counts n_i and positives x_i (so binomial
  proportion p_i = x_i/n_i per group), and linear group scores t_i = 1..k:
    N = sum(n_i), R = sum(x_i), p_bar = R/N
    Z = [sum(x_i*t_i) - p_bar*sum(n_i*t_i)]
        / sqrt( p_bar*(1-p_bar) * [sum(n_i*t_i^2) - (sum(n_i*t_i))^2/N] )
  One-sided p-value (testing H1: increasing trend) = 1 - Phi(Z).

Reads the exact same artifacts/oof/<asset>_<edge>_oof_predictions.csv used by
the authoritative Gate D, so both scripts are always looking at the identical
underlying data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

MIN_CLASS_SIZE = 10
DEFAULT_N_BINS = 10

RAW_PROBABILITY_COL = "raw_probability"
META_LABEL_COL = "quote_executable_meta_label"


class PrecisionTrendError(RuntimeError):
    """Raised when the experiment cannot be evaluated (bad input, not a verdict)."""


def cochran_armitage_trend_test(n_i: np.ndarray, x_i: np.ndarray) -> tuple[float, float]:
    """One-sided (increasing-trend) Cochran-Armitage test. Returns (Z, p_value)."""
    k = len(n_i)
    t_i = np.arange(1, k + 1, dtype="float64")
    N = float(n_i.sum())
    R = float(x_i.sum())
    p_bar = R / N
    s_nt = float((n_i * t_i).sum())
    s_nt2 = float((n_i * t_i**2).sum())
    numerator = float((x_i * t_i).sum()) - p_bar * s_nt
    variance = p_bar * (1.0 - p_bar) * (s_nt2 - (s_nt**2) / N)
    if variance <= 0:
        return 0.0, 1.0
    z = numerator / np.sqrt(variance)
    p_value = float(1.0 - stats.norm.cdf(z))
    return float(z), p_value


def evaluate_precision_trend(
    oof_predictions_csv: Path, *, edge_id: str, n_bins: int = DEFAULT_N_BINS
) -> dict[str, Any]:
    if not oof_predictions_csv.exists():
        raise PrecisionTrendError(f"{edge_id}: OOF predictions CSV not found: {oof_predictions_csv}")

    df = pd.read_csv(oof_predictions_csv)
    required = {RAW_PROBABILITY_COL, META_LABEL_COL}
    missing = required - set(df.columns)
    if missing:
        raise PrecisionTrendError(f"{edge_id}: OOF predictions CSV missing required columns: {sorted(missing)}")

    df = df.dropna(subset=[RAW_PROBABILITY_COL, META_LABEL_COL]).copy()
    n_rows = int(len(df))
    if n_rows == 0:
        raise PrecisionTrendError(f"{edge_id}: OOF predictions CSV has zero usable rows after dropping NaNs.")

    y = df[META_LABEL_COL].astype(int).to_numpy()
    p = df[RAW_PROBABILITY_COL].to_numpy(dtype="float64")
    n_pos, n_neg = int(y.sum()), int(n_rows - y.sum())

    result: dict[str, Any] = {
        "edge_id": edge_id,
        "oof_predictions_csv": str(oof_predictions_csv),
        "n_rows": n_rows,
        "n_positive": n_pos,
        "n_negative": n_neg,
        "positive_rate": float(n_pos / n_rows) if n_rows else None,
        "n_bins_requested": n_bins,
        "experimental_non_gating": True,
    }

    if n_pos < MIN_CLASS_SIZE or n_neg < MIN_CLASS_SIZE:
        result.update({
            "verdict": "INCONCLUSIVE",
            "reason": f"INSUFFICIENT_CLASS_SIZE: n_positive={n_pos}, n_negative={n_neg} (need >= {MIN_CLASS_SIZE}).",
        })
        return result

    # Rank-based qcut for robustness against tied raw_probability values.
    rank = pd.Series(p).rank(method="first")
    try:
        bin_id = pd.qcut(rank, q=n_bins, labels=False, duplicates="drop")
    except ValueError as exc:
        raise PrecisionTrendError(f"{edge_id}: could not form {n_bins} bins: {exc}") from exc

    actual_bins = int(bin_id.nunique())
    order = np.argsort(pd.Series(p).groupby(bin_id).mean().index.to_numpy())
    # bin_id from qcut on rank is already ordered low->high score by construction.

    table = pd.DataFrame({"bin": bin_id.to_numpy(), "y": y})
    grp = table.groupby("bin")["y"].agg(n="count", x="sum").sort_index()
    n_i = grp["n"].to_numpy(dtype="float64")
    x_i = grp["x"].to_numpy(dtype="float64")
    precision_i = (x_i / n_i).tolist()

    z_stat, p_value = cochran_armitage_trend_test(n_i, x_i)
    verdict = "PASS" if p_value < 0.05 and z_stat > 0 else "FAIL"

    result.update({
        "n_bins_actual": actual_bins,
        "decile_table": [
            {"decile": int(i + 1), "n": int(n_i[i]), "positives": int(x_i[i]), "precision": float(precision_i[i])}
            for i in range(actual_bins)
        ],
        "base_rate_precision": float(n_pos / n_rows),
        "cochran_armitage_z": z_stat,
        "cochran_armitage_p_value_one_sided_increasing": p_value,
        "verdict": verdict,
        "reason": (
            f"Cochran-Armitage Z={z_stat:.4f}, one-sided p={p_value:.4g} for an INCREASING precision trend "
            f"across {actual_bins} raw_probability-ranked bins "
            f"({'PASS: significant increasing precision trend' if verdict == 'PASS' else 'FAIL: no significant increasing precision trend'})."
        ),
    })
    return result


def _default_oof_path(edge_root: Path) -> Path:
    parts = edge_root.resolve().parts
    try:
        asset = parts[parts.index("assets") + 1]
    except (ValueError, IndexError) as exc:
        raise PrecisionTrendError(f"Could not infer asset from edge-root path: {edge_root}") from exc
    edge_name = edge_root.resolve().name
    return edge_root / "artifacts" / "oof" / f"{asset.lower()}_{edge_name}_oof_predictions.csv"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="EXPERIMENTAL, non-gating: Cochran-Armitage decile precision-trend test on raw OOF predictions."
    )
    parser.add_argument("--edge-root", required=True, help="Path to assets/<ASSET>/edges/<family>_<side>.")
    parser.add_argument("--oof-csv", default=None, help="Override path to the OOF predictions CSV.")
    parser.add_argument("--n-bins", type=int, default=DEFAULT_N_BINS, help="Number of raw_probability-ranked bins (default 10).")
    parser.add_argument("--write-report", action="store_true", help="Write JSON report under <edge-root>/reports/.")
    args = parser.parse_args()

    edge_root = Path(args.edge_root).resolve()
    edge_id = f"{edge_root.parent.parent.name}/{edge_root.name}"
    oof_csv = Path(args.oof_csv).resolve() if args.oof_csv else _default_oof_path(edge_root)

    try:
        result = evaluate_precision_trend(oof_csv, edge_id=edge_id, n_bins=args.n_bins)
    except PrecisionTrendError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print("=" * 80)
    print(f"GATE D EXPERIMENT (non-gating): Cochran-Armitage precision trend -- {edge_id}")
    print("=" * 80)
    for key, value in result.items():
        if key == "decile_table":
            print("decile_table:")
            for row in value:
                print(f"  decile={row['decile']:>2} n={row['n']:>5} positives={row['positives']:>5} precision={row['precision']:.4f}")
        else:
            print(f"{key}: {value}")

    if args.write_report:
        report_dir = edge_root / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "gate_d_precision_trend_experiment_report.json"
        report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nReport: {report_path}")

    raise SystemExit(0)


if __name__ == "__main__":
    main()
