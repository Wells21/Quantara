"""
V2 Phase B2.08B -- Probability of Backtest Overfitting (AFML Ch.11) applied
to Step 08's rolling-start scenario search.

Step 08 (08_rolling_start_prop_challenge_optimizer.py) tests a grid of
scenario configurations (risk-sizing modes, budgets, overlap rules, etc.)
across many rolling-start windows and picks a "robust" and a "fast_safe"
winner by historical pass-rate/speed. That selection-among-many-candidates
process is structurally the same multiple-comparison overfitting exposure
that AFML Ch.11's CSCV/PBO framework exists to measure -- Step 08 itself
does not run any such check, and this script closes that gap as a
diagnostic (not a gate, consistent with 07b/07c/asset_independence_lab.py):
it does not change Step 08's own selection, it only reports how much of
Step 08's apparent "robust winner" ranking a resampled train/test split
would actually agree with.

Method (shared/backtest_statistics/afml_ch11_pbo_reference.py, faithful to
AFML SS11.6-11.7, p.157-167):
  For every group of scenarios that were evaluated on an IDENTICAL set of
  rolling-start windows (same return_mode/universe/profile_role/
  linkage_method -- CSCV requires all candidates aligned on the same
  observations), build a (rolling-start x scenario) performance matrix
  from Step 08's own stage2_run_results.csv at cost_stress_r=0.0, using
  net_profit as the per-observation performance measure. Run CSCV with
  S=16 blocks and report PBO plus the performance-degradation slope.

Requires portfolio_allocator/outputs/prop_challenge_rolling_optimizer_outputs/
stage2_run_results.csv to already exist (produced by running Step 08).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for p in (V2_ROOT, REPO_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from shared.backtest_statistics.afml_ch11_pbo_reference import (  # noqa: E402
    AFMLPBOError,
    cscv_pbo,
)

SCHEMA_VERSION = "v1.0"
OPTIMIZER_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "prop_challenge_rolling_optimizer_outputs"
INPUT_STAGE2_RUNS = OPTIMIZER_DIR / "stage2_run_results.csv"
OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "pbo_cscv_validation_outputs"

GROUP_COLS = ["return_mode", "universe", "profile_role", "linkage_method"]
PERFORMANCE_COLUMN = "net_profit"
CSCV_S_BLOCKS = 16
MIN_SCENARIOS_PER_GROUP = 2
MIN_STARTS_PER_SCENARIO = CSCV_S_BLOCKS


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def build_group_performance_matrix(group_df: pd.DataFrame) -> pd.DataFrame | None:
    """Pivot (scenario_id x start_ns) -> net_profit. Only scenarios present
    across the FULL common set of start_ns for this group are kept, so
    every column is evaluated on identical rows (a CSCV requirement)."""
    pivot = group_df.pivot_table(
        index="start_ns", columns="scenario_id", values=PERFORMANCE_COLUMN, aggfunc="mean"
    )
    complete_cols = [c for c in pivot.columns if pivot[c].notna().all()]
    pivot = pivot[complete_cols].sort_index()
    if pivot.shape[1] < MIN_SCENARIOS_PER_GROUP or pivot.shape[0] < MIN_STARTS_PER_SCENARIO:
        return None
    return pivot


def build(*, write: bool = True, stage2_runs_path: Path = INPUT_STAGE2_RUNS) -> dict[str, Any]:
    if not stage2_runs_path.exists():
        raise FileNotFoundError(
            f"Missing {stage2_runs_path}. Run 08_rolling_start_prop_challenge_optimizer.py first."
        )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    runs = pd.read_csv(stage2_runs_path, float_precision="round_trip")
    base = runs[runs["cost_stress_r"] == 0.0].copy()
    if base.empty:
        raise AFMLPBOError("stage2_run_results.csv has no cost_stress_r=0.0 rows.")

    results = []
    skipped = []
    for group_key, group_df in base.groupby(GROUP_COLS):
        matrix = build_group_performance_matrix(group_df)
        group_label = "/".join(str(g) for g in group_key)
        if matrix is None:
            skipped.append({
                "group": group_label,
                "n_scenarios_raw": int(group_df["scenario_id"].nunique()),
                "n_starts_raw": int(group_df["start_ns"].nunique()),
                "reason": "insufficient aligned scenarios or starts for CSCV",
            })
            continue
        pbo_result = cscv_pbo(matrix, s=CSCV_S_BLOCKS, higher_is_better=True)
        pbo_result["group"] = group_label
        pbo_result["n_scenarios_evaluated"] = matrix.shape[1]
        pbo_result["n_starts_evaluated"] = matrix.shape[0]
        results.append(pbo_result)

    report_rows = [
        {
            "group": r["group"],
            "n_scenarios_evaluated": r["n_scenarios_evaluated"],
            "n_starts_evaluated": r["n_starts_evaluated"],
            "n_combinations": r["n_combinations"],
            "pbo": r["pbo"],
            "mean_logit": r["mean_logit"],
            "performance_degradation_slope": r["performance_degradation_slope"],
            "prob_oos_winner_nonpositive": r["prob_oos_winner_nonpositive"],
        }
        for r in results
    ]
    report_df = pd.DataFrame(report_rows).sort_values("pbo", ascending=False) if report_rows else pd.DataFrame(
        columns=["group", "n_scenarios_evaluated", "n_starts_evaluated", "n_combinations", "pbo", "mean_logit", "performance_degradation_slope", "prob_oos_winner_nonpositive"]
    )

    n_groups_evaluated = len(results)
    n_groups_high_pbo = int((report_df["pbo"] > 0.5).sum()) if not report_df.empty else 0
    worst_pbo = float(report_df["pbo"].max()) if not report_df.empty else float("nan")

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "status": "PASS",
        "input_stage2_runs": str(stage2_runs_path.relative_to(V2_ROOT)),
        "methodology": {
            "cscv_pbo": "AFML SS11.6-11.7 (p.157-167): Combinatorially Symmetric Cross-Validation. Rolling-start windows are split into S=16 contiguous blocks; every C(16,8) train/test split picks the in-sample best-net-profit scenario and checks its out-of-sample relative rank. PBO = fraction of splits where the IS winner underperformed the OOS median.",
            "not_a_gate": "This lab is diagnostic only. It does not change Step 08's own scenario selection (robust_rank / fast_safe_rank). It reports whether that selection process is exposed to overfitting risk.",
            "grouping": "CSCV requires every candidate compared on identical observations, so PBO is computed separately within each (return_mode, universe, profile_role, linkage_method) group -- scenarios sharing the same underlying rolling-start windows.",
        },
        "n_groups_evaluated": n_groups_evaluated,
        "n_groups_skipped": len(skipped),
        "n_groups_pbo_above_50pct": n_groups_high_pbo,
        "worst_group_pbo": worst_pbo,
        "skipped_groups": skipped,
        "outputs": {
            "report": str(OUTPUT_DIR / "pbo_cscv_report.csv"),
            "manifest": str(OUTPUT_DIR / "pbo_cscv_manifest.json"),
        },
    }

    if write:
        report_df.to_csv(OUTPUT_DIR / "pbo_cscv_report.csv", index=False)
        write_json(OUTPUT_DIR / "pbo_cscv_manifest.json", manifest)

    return manifest


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V2 Phase B2.08B AFML Ch.11 PBO/CSCV validation of Step 08's scenario search.")
    parser.add_argument("--stage2-runs", default=str(INPUT_STAGE2_RUNS))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    manifest = build(write=not args.dry_run, stage2_runs_path=Path(args.stage2_runs))
    print("V2 Phase B2.08B AFML Ch.11 PBO/CSCV validation")
    print(f"Status: {manifest['status']}")
    print(f"Groups evaluated: {manifest['n_groups_evaluated']} (skipped: {manifest['n_groups_skipped']})")
    print(f"Groups with PBO > 50%: {manifest['n_groups_pbo_above_50pct']}")
    print(f"Worst group PBO: {manifest['worst_group_pbo']}")
    for name, path in manifest["outputs"].items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
