# QUANTARA_V3_SEMANTIC_PHASE2_EMPIRICAL_EXECUTABLE_PAYOFF_V1_REV3
from __future__ import annotations

# QUANTARA_V3_SEMANTIC_PHASE3A_PROVENANCE_CONTEXT_V1

"""V3 allocator Step 04D: independent final representative executable-payoff/AFML audit."""
import argparse
import hashlib
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

from shared.calibration.sigmoid_afml import (
    AFML_BET_SIZE_COL,
    AFML_SIZING_METHOD,
    PAYOFF_PROFILE_VERSION,
    empirical_payoff_profile,
    payoff_profile_afml_components,
    validate_payoff_profile,
)
from shared.validation.v3_semantic_contract import (
    GROSS_LABEL_CONTRACT_VERSION,
    EXECUTED_TARGET_CONTRACT_VERSION,
    PAYOFF_CONTEXT_VERSION,
    build_payoff_context,
)

OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs"
REPRESENTATIVES = OUTPUT_DIR / "v2_representative_threshold_candidates_all.csv"
GRID_LEDGER = OUTPUT_DIR / "v2_threshold_grid_trade_ledger_core_plus_watchlist.csv"
REPORT = OUTPUT_DIR / "v3_final_representative_executable_economics.csv"
MANIFEST = OUTPUT_DIR / "v3_final_representative_executable_economics_manifest.json"
EXECUTION_CONTRACT_VERSION = "v3_oof_exact_bidask_economic_label_v2"
AFML_ATOL = 1e-12


class RepresentativeExecutableEconomicsError(RuntimeError):
    pass


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def max_drawdown(values: pd.Series) -> float:
    r = pd.to_numeric(values, errors="coerce").dropna()
    if r.empty:
        return 0.0
    equity = r.cumsum()
    return float((equity - equity.cummax()).min())


def profit_factor(values: pd.Series) -> float:
    r = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    if len(r) == 0:
        return float("nan")
    gp = float(r[r > 0].sum()) if np.any(r > 0) else 0.0
    gl = float(abs(r[r < 0].sum())) if np.any(r < 0) else 0.0
    if gl <= 0:
        return float("inf") if gp > 0 else float("nan")
    return gp / gl


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RepresentativeExecutableEconomicsError(message)


def _embedded_profile(selected: pd.DataFrame) -> dict[str, Any]:
    keys = [
        "payoff_profile_version", "payoff_profile_valid", "payoff_profile_invalid_reason",
        "payoff_trade_count", "payoff_win_count", "payoff_loss_count", "payoff_zero_count", "payoff_zero_rate",
        "min_wins_required", "min_losses_required", "empirical_avg_win_r", "empirical_avg_loss_r",
        "empirical_reward_risk_ratio", "empirical_break_even_probability",
    ]
    missing = [k for k in keys if k not in selected.columns]
    require(not missing, f"04B ledger missing payoff profile fields: {missing}")
    first = selected.iloc[0]
    profile = {k: first[k] for k in keys}
    profile["payoff_profile_valid"] = str(profile["payoff_profile_valid"]).strip().lower() in {"true", "1", "yes"}
    return validate_payoff_profile(profile)


def build(*, write: bool = True) -> dict[str, Any]:
    require(REPRESENTATIVES.exists(), f"Run 04C first. Missing: {REPRESENTATIVES}")
    require(GRID_LEDGER.exists(), f"Run 04B/04BB first. Missing: {GRID_LEDGER}")
    # float_precision="round_trip": see config/mldp_deviation_register.json --
    # pandas' default C float parser is not always bit-exact, which breaks the
    # tight atol=1e-12 payoff-profile identity checks below.
    reps = pd.read_csv(REPRESENTATIVES, float_precision="round_trip")
    ledger = pd.read_csv(GRID_LEDGER, float_precision="round_trip")
    require(not reps.empty, "04C representative file is empty.")
    require(not ledger.empty, "04B threshold-grid ledger is empty.")
    rep_required = ["representative_id", "source_grid_candidate_id", "edge_id", "asset", "threshold", "trade_count"]
    missing = [c for c in rep_required if c not in reps.columns]
    require(not missing, f"04C representatives missing columns: {missing}")
    ledger_required = [
        "grid_candidate_id", "edge_id", "executed_r", "executed_meta_label", "calibrated_probability",
        "afml_bet_size", "afml_sizing_method", "execution_status", "execution_contract_version",
        "execution_geometry_hash", "geometry_hash",
        "payoff_context_version", "payoff_context_id", "payoff_context_edge_id",
        "gross_label_contract_version", "executed_target_contract_version",
        "threshold_probability_column",
    ]
    missing = [c for c in ledger_required if c not in ledger.columns]
    require(not missing, f"04B ledger missing Phase-2 columns: {missing}")
    require(ledger["execution_status"].astype(str).eq("resolved").all(), "04D refuses non-resolved exact-execution rows.")
    require(ledger["execution_contract_version"].astype(str).eq(EXECUTION_CONTRACT_VERSION).all(), "04D execution contract version mismatch.")
    require(ledger["execution_geometry_hash"].astype(str).eq(ledger["geometry_hash"].astype(str)).all(), "04D execution geometry hash mismatch.")
    require(ledger["afml_sizing_method"].astype(str).eq(AFML_SIZING_METHOD).all(), "04D AFML sizing method mismatch.")

    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    available_ids = set(ledger["grid_candidate_id"].astype(str))
    for rep in reps.itertuples(index=False):
        grid_id = str(rep.source_grid_candidate_id)
        rep_id = str(rep.representative_id)
        edge_id = str(rep.edge_id)
        if grid_id not in available_ids:
            failures.append(f"{rep_id}: source grid candidate missing")
            continue
        selected = ledger[ledger["grid_candidate_id"].astype(str).eq(grid_id)].copy()
        try:
            require(not selected.empty, f"{rep_id}: selected ledger subset empty")
            require(selected["edge_id"].astype(str).nunique() == 1 and str(selected["edge_id"].iloc[0]) == edge_id, f"{rep_id}: edge mismatch")
            expected_count = int(float(rep.trade_count))
            actual_count = int(len(selected))
            ledger_thresholds = pd.to_numeric(selected["threshold"], errors="coerce")
            require(
                ledger_thresholds.notna().all()
                and np.allclose(ledger_thresholds.to_numpy(dtype=float), float(rep.threshold), atol=1e-12, rtol=0.0),
                f"{rep_id}: representative threshold does not match 04B ledger threshold",
            )
            r = pd.to_numeric(selected["executed_r"], errors="coerce").to_numpy(dtype=float)
            require(np.isfinite(r).all(), f"{rep_id}: non-finite executed_r")
            labels = pd.to_numeric(selected["executed_meta_label"], errors="coerce").to_numpy(dtype=float)
            require(np.isfinite(labels).all() and np.array_equal(labels.astype(int), (r > 0.0).astype(int)), f"{rep_id}: executed label identity mismatch")
            embedded = _embedded_profile(selected)
            recomputed = empirical_payoff_profile(r, min_wins=int(embedded["min_wins_required"]), min_losses=int(embedded["min_losses_required"]))
            recomputed = validate_payoff_profile(recomputed)
            for key in ("payoff_zero_rate", "empirical_avg_win_r", "empirical_avg_loss_r", "empirical_reward_risk_ratio", "empirical_break_even_probability"):
                require(np.isclose(float(embedded[key]), float(recomputed[key]), atol=1e-12, rtol=0.0), f"{rep_id}: payoff profile mismatch {key}")
            stored_context_ids = selected["payoff_context_id"].astype(str).unique().tolist()
            require(len(stored_context_ids) == 1, f"{rep_id}: multiple payoff_context_id values in 04B ledger")
            reconstructed_context = build_payoff_context(
                edge_id=edge_id,
                threshold=float(rep.threshold),
                geometry_hash=str(selected["geometry_hash"].iloc[0]),
                payoff_profile=recomputed,
                threshold_probability_column=str(selected["threshold_probability_column"].iloc[0]),
            )
            require(
                stored_context_ids[0] == reconstructed_context["payoff_context_id"],
                f"{rep_id}: independently reconstructed threshold/payoff context does not match 04B",
            )
            probs = pd.to_numeric(selected["calibrated_probability"], errors="coerce").to_numpy(dtype=float)
            replay_afml = payoff_profile_afml_components(probs, recomputed)[AFML_BET_SIZE_COL]
            stored_afml = pd.to_numeric(selected["afml_bet_size"], errors="coerce").to_numpy(dtype=float)
            require(np.isfinite(stored_afml).all(), f"{rep_id}: non-finite stored AFML")
            afml_max_diff = float(np.max(np.abs(stored_afml - replay_afml))) if len(stored_afml) else 0.0
            require(afml_max_diff <= AFML_ATOL, f"{rep_id}: AFML replay mismatch max={afml_max_diff:.3e}")
            mean_r = float(np.mean(r)); total_r = float(np.sum(r)); pf = profit_factor(pd.Series(r)); dd = max_drawdown(pd.Series(r))
            passed = actual_count == expected_count and actual_count > 0 and mean_r > 0.0 and total_r > 0.0 and (np.isinf(pf) or pf > 1.0)
            require(passed, f"{rep_id}: executable economics failed")
            rows.append({
                "representative_id": rep_id, "source_grid_candidate_id": grid_id, "edge_id": edge_id,
                "asset": str(rep.asset), "threshold": float(rep.threshold), "expected_trade_count": expected_count,
                "actual_trade_count": actual_count, "trade_count_diff": actual_count - expected_count,
                "exec_mean_r": mean_r, "exec_total_r": total_r, "exec_profit_factor": pf,
                "exec_max_drawdown_r": dd, "execution_contract_version": EXECUTION_CONTRACT_VERSION,
                "geometry_hash": str(selected["geometry_hash"].iloc[0]), "payoff_profile_version": PAYOFF_PROFILE_VERSION,
                "payoff_zero_rate": recomputed["payoff_zero_rate"],
                "empirical_avg_win_r": recomputed["empirical_avg_win_r"], "empirical_avg_loss_r": recomputed["empirical_avg_loss_r"],
                "empirical_reward_risk_ratio": recomputed["empirical_reward_risk_ratio"],
                "empirical_break_even_probability": recomputed["empirical_break_even_probability"],
                "afml_sizing_method": AFML_SIZING_METHOD, "afml_replay_max_abs_diff": afml_max_diff,
                "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
                "executed_target_contract_version": EXECUTED_TARGET_CONTRACT_VERSION,
                "payoff_context_version": PAYOFF_CONTEXT_VERSION,
                "payoff_context_id": reconstructed_context["payoff_context_id"], "status": "PASS",
            })
        except Exception as exc:
            failures.append(f"{rep_id}: {exc}")

    report = pd.DataFrame(rows)
    status = "PASS" if not failures and len(report) == len(reps) else "FAIL"
    manifest = {
        "schema_version": "v3.2", "created_at": now_utc(),
        "final_representative_executable_economics_status": status,
        "summary": {"representatives_expected": int(len(reps)), "representatives_audited": int(len(report)),
                    "pass_count": int((report["status"] == "PASS").sum()) if not report.empty else 0,
                    "fail_count": len(failures)},
        "threshold_ownership": "04C owns representative threshold; 04D independently verifies its exact payoff profile and AFML without re-optimizing.",
        "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "executed_target_contract_version": EXECUTED_TARGET_CONTRACT_VERSION,
        "payoff_profile_version": PAYOFF_PROFILE_VERSION,
        "payoff_context_version": PAYOFF_CONTEXT_VERSION,
        "afml_sizing_method": AFML_SIZING_METHOD,
        "source_sha256": {REPRESENTATIVES.name: sha256_file(REPRESENTATIVES), GRID_LEDGER.name: sha256_file(GRID_LEDGER)},
        "failures": failures, "outputs": {"report": REPORT.name, "manifest": MANIFEST.name},
        "next_ordered_step": "05_build_representative_trade_ledger.py",
    }
    if write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        report.to_csv(REPORT, index=False)
        MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print("V3 Phase 2.04D final representative executable payoff/AFML audit")
    print(f"Status: {status} | Representatives: {len(report)}/{len(reps)} | failures={len(failures)}")
    if failures:
        for item in failures[:30]: print(" -", item)
    if status != "PASS":
        raise RepresentativeExecutableEconomicsError("04D failed. Do not continue to Step 05.")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--dry-run", action="store_true"); args = parser.parse_args()
    build(write=not args.dry_run)


if __name__ == "__main__":
    try: main()
    except RepresentativeExecutableEconomicsError as exc: raise SystemExit(f"ERROR: {exc}") from exc
