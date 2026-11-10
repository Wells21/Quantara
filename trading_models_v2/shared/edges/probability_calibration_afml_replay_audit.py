"""Generic SOTD Step 08 probability/calibration/AFML replay audit.

This shared module verifies that runtime.score() reproduces selected feature rows
for overlapping historical events and independently replays:

    model.predict_proba -> raw_probability
    saved sigmoid calibrator -> calibrated_probability
    payoff-aware AFML bet size -> afml_bet_size
    calibrated threshold -> approved_signal

Edge-local research/08_probability_calibration_afml_replay_audit.py files should
be thin wrappers around this module.
"""

from __future__ import annotations

# QUANTARA_V3_SEMANTIC_PHASE3A_PROVENANCE_CONTEXT_V1

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

import numpy as np
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from shared.calibration.sigmoid_afml import (  # noqa: E402
    AFML_BET_SIZE_COL,
    AFML_ECONOMIC_STRENGTH_COL,
    AFML_EFFECTIVE_PROBABILITY_COL,
    AFML_SIZING_METHOD,
    AFML_SIZING_METHOD_COL,
    APPROVED_SIGNAL_COL,
    BREAK_EVEN_PROBABILITY_COL,
    CALIBRATED_PROBABILITY_COL,
    RAW_PROBABILITY_COL,
    REWARD_RISK_RATIO_COL,
    payoff_profile_afml_components,
    payoff_profile_from_threshold_payload,
    apply_sigmoid_calibrator,
    validate_threshold_payload,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.edges.edge_layout import EdgeLayout, get_edge_layout_from_edge_root  # noqa: E402
from shared.edges.trade_geometry import load_trade_geometry  # noqa: E402
from shared.labels.leakage_columns import LEAKAGE_COLS, SCORE_OUTPUT_OVERLAP  # noqa: E402
from shared.validation.v3_semantic_contract import (
    validate_research_generation_chain,
    validate_payoff_context_mapping,
)

TIME_COL = "Datetime"
# Recursive/path-dependent window statistics (pandas rolling skew/kurt, STC-style
# smoothed indicators) are not bit-reproducible when the full feature history is
# recomputed independently, even from bit-identical input prices -- confirmed
# empirically 2026-09-15 on JPN/rangestructure_long (Close diff == 0.0 across all
# 4,390 overlap rows; Roll_Kurt_50 drifted up to 3.2e-4). 1e-3 stays 3+ orders of
# magnitude tighter than a real feature-logic bug would produce while tolerating
# this class of pandas-internal floating-point non-determinism.
FEATURE_ATOL = 1e-3
PROBABILITY_ATOL = 1e-12
AFML_ATOL = 1e-12
MIN_EXPECTED_OVERLAP_ROWS = 1
# Canonical leakage-column list (see shared/labels/leakage_columns.py); "label"
# and "target" are generic aliases kept in addition to the canonical set.
# SCORE_OUTPUT_OVERLAP is subtracted because a live score row legitimately
# carries some of those same column names as intended output, not leakage.
FORBIDDEN_SCORE_COLUMNS = (LEAKAGE_COLS | {"label", "target"}) - SCORE_OUTPUT_OVERLAP


class ReplayAuditError(RuntimeError):
    """Raised when Step 08 fails before report creation."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.resolve().as_posix()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return payload


def save_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def add_finding(findings: list[dict[str, Any]], severity: str, code: str, message: str, **extra: Any) -> None:
    row = {"severity": severity, "code": code, "message": message}
    row.update(extra)
    findings.append(row)


def import_runtime(layout: EdgeLayout) -> ModuleType:
    runtime_path = layout.runtime_py
    edge_root_str = str(layout.edge_root)
    if edge_root_str not in sys.path:
        sys.path.insert(0, edge_root_str)
    spec = importlib.util.spec_from_file_location(f"v2_runtime_{layout.edge_stem}_step08", runtime_path)
    if spec is None or spec.loader is None:
        raise ReplayAuditError(f"Could not import runtime.py: {runtime_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selected_features(layout: EdgeLayout) -> list[str]:
    payload = load_json(layout.selected_features_json)
    features = payload.get("selected_features")
    if not isinstance(features, list) or not features:
        raise ReplayAuditError("selected_features.json must contain a non-empty selected_features list.")
    return [str(feature) for feature in features]


def selected_threshold(layout: EdgeLayout) -> float:
    return validate_threshold_payload(load_json(layout.selected_threshold_json))


def positive_class_probability(model: Any, x_data: pd.DataFrame) -> np.ndarray:
    proba = model.predict_proba(x_data)
    if proba.ndim != 2:
        raise ReplayAuditError("Model predict_proba must return 2D array.")
    if proba.shape[1] == 1:
        cls = int(model.classes_[0])
        return np.ones(len(x_data), dtype="float64") if cls == 1 else np.zeros(len(x_data), dtype="float64")
    class_to_col = {int(cls): i for i, cls in enumerate(model.classes_)}
    if 1 not in class_to_col:
        raise ReplayAuditError("Model classes_ does not contain positive class 1.")
    return proba[:, class_to_col[1]].astype("float64")


def load_research_model_rows(layout: EdgeLayout, features: list[str]) -> pd.DataFrame:
    path = layout.research_dataset_dir / f"{layout.edge_stem}_model_rows.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path)
    if TIME_COL not in df.columns:
        raise ReplayAuditError("Research model rows missing Datetime column.")
    missing = [feature for feature in features if feature not in df.columns]
    if missing:
        raise ReplayAuditError(f"Research model rows missing selected features: {missing}")
    df[TIME_COL] = to_datetime_iso(df[TIME_COL], utc=True, errors="coerce")
    df = df.dropna(subset=[TIME_COL]).sort_values(TIME_COL).drop_duplicates(TIME_COL)
    for feature in features:
        df[feature] = pd.to_numeric(df[feature], errors="coerce")
    return df[[TIME_COL, *features]].copy()


def max_selected_feature_abs_diff(merged: pd.DataFrame, features: list[str]) -> tuple[float, str | None]:
    max_diff = 0.0
    max_feature = None
    for feature in features:
        diff = (merged[f"{feature}_runtime"] - merged[f"{feature}_research"]).abs().max()
        diff = float(diff) if pd.notna(diff) else 0.0
        if diff > max_diff:
            max_diff = diff
            max_feature = feature
    return max_diff, max_feature


def run_replay_audit(edge_root: str | Path, *, write_report: bool = True) -> dict[str, Any]:
    layout = get_edge_layout_from_edge_root(edge_root)
    runtime = import_runtime(layout)
    findings: list[dict[str, Any]] = []

    try:
        semantic_chain = validate_research_generation_chain(layout)
        features = selected_features(layout)
        threshold = selected_threshold(layout)
        threshold_artifact = load_json(layout.selected_threshold_json)
        payoff_profile = payoff_profile_from_threshold_payload(threshold_artifact)
        payoff_context = validate_payoff_context_mapping(
            threshold_artifact.get("payoff_context") or {},
            expected_edge_id=layout.edge_id,
            expected_threshold=threshold,
            expected_geometry_hash=semantic_chain["geometry_hash"],
        )
        calibrator = load_json(layout.calibrator_json)
        geometry = load_trade_geometry(
            layout.edge_root / "edge.yaml",
            expected_edge_id=layout.edge_id,
            expected_side_name=layout.side_name,
        )
        scored, runtime_manifest = runtime.score(latest_only=False)
        scored[TIME_COL] = to_datetime_iso(scored[TIME_COL], utc=True, errors="coerce")
        scored = scored.dropna(subset=[TIME_COL]).sort_values(TIME_COL).drop_duplicates(TIME_COL).reset_index(drop=True)
    except Exception as exc:
        report = {
            "created_at": now_utc(),
            "edge_id": layout.edge_id,
            "script": "shared/edges/probability_calibration_afml_replay_audit.py",
            "replay_audit_status": "FAIL",
            "deployable": False,
            "summary": {"pass_count": 0, "warn_count": 0, "fail_count": 1},
            "metrics": {},
            "runtime_manifest": None,
            "findings": [{"severity": "FAIL", "code": "RUNTIME_SCORE_OR_ARTIFACT_LOAD_FAILED", "message": str(exc)}],
        }
        if write_report:
            save_json(layout.replay_audit_report_json, report)
        return report

    if scored.empty:
        add_finding(findings, "FAIL", "NO_RUNTIME_SCORED_ROWS", "runtime.score returned zero rows.")
        return finalize(layout, findings, {}, runtime_manifest, write_report=write_report)

    forbidden_present = sorted(FORBIDDEN_SCORE_COLUMNS.intersection(scored.columns))
    if forbidden_present:
        add_finding(findings, "FAIL", "FORBIDDEN_LABEL_COLUMNS_PRESENT", "Runtime score output contains research label columns.", columns=forbidden_present)
    else:
        add_finding(findings, "PASS", "NO_FORBIDDEN_LABEL_COLUMNS", "Runtime score output contains no research label columns.")

    required_probability_cols = [
        RAW_PROBABILITY_COL,
        CALIBRATED_PROBABILITY_COL,
        REWARD_RISK_RATIO_COL,
        BREAK_EVEN_PROBABILITY_COL,
        AFML_ECONOMIC_STRENGTH_COL,
        AFML_EFFECTIVE_PROBABILITY_COL,
        AFML_BET_SIZE_COL,
        AFML_SIZING_METHOD_COL,
        APPROVED_SIGNAL_COL,
        "payoff_context_version",
        "payoff_context_id",
        "gross_label_contract_version",
        "execution_contract_version",
        "executed_target_contract_version",
    ]
    missing_probability_cols = [col for col in required_probability_cols if col not in scored.columns]
    if missing_probability_cols:
        add_finding(findings, "FAIL", "SCORE_PROBABILITY_COLUMNS_MISSING", "Runtime score output missing probability/risk columns.", columns=missing_probability_cols)
        return finalize(layout, findings, {}, runtime_manifest, write_report=write_report)
    add_finding(
        findings,
        "PASS",
        "SCORE_PROBABILITY_COLUMNS_PRESENT",
        "Runtime score output has raw, calibrated, payoff-aware AFML, sizing-contract, and approved-signal columns.",
    )

    try:
        research = load_research_model_rows(layout, features)
    except Exception as exc:
        add_finding(findings, "FAIL", "RESEARCH_MODEL_ROWS_LOAD_FAILED", str(exc))
        return finalize(layout, findings, {}, runtime_manifest, write_report=write_report)

    runtime_feature_frame = scored[[TIME_COL, *features]].copy()
    merged = runtime_feature_frame.merge(
        research,
        on=TIME_COL,
        how="inner",
        suffixes=("_runtime", "_research"),
        validate="one_to_one",
    )
    overlap_rows = int(len(merged))
    extra_runtime_rows = int(len(scored) - overlap_rows)
    missing_runtime_research_rows = int(len(research) - overlap_rows)
    if overlap_rows >= MIN_EXPECTED_OVERLAP_ROWS:
        add_finding(findings, "PASS", "RUNTIME_RESEARCH_OVERLAP_PRESENT", f"Runtime/research overlap rows: {overlap_rows:,}")
    else:
        add_finding(findings, "FAIL", "RUNTIME_RESEARCH_OVERLAP_TOO_SMALL", f"Runtime/research overlap rows too small: {overlap_rows:,}")

    if extra_runtime_rows > 0:
        add_finding(findings, "WARN", "EXTRA_RUNTIME_ROWS_AFTER_RESEARCH_LABEL_HORIZON", "Runtime has extra scoreable rows that research labels may have dropped due to vertical-horizon availability.", extra_runtime_rows=extra_runtime_rows)
    if missing_runtime_research_rows > 0:
        add_finding(findings, "WARN", "RESEARCH_ROWS_NOT_IN_RUNTIME_SCORE", "Some research rows did not appear in runtime score output.", missing_rows=missing_runtime_research_rows)

    if overlap_rows == 0:
        return finalize(layout, findings, {}, runtime_manifest, write_report=write_report)

    feature_max_diff, feature_max_name = max_selected_feature_abs_diff(merged, features)
    if feature_max_diff <= FEATURE_ATOL:
        add_finding(findings, "PASS", "SELECTED_FEATURE_REPLAY_EXACT", f"Selected features replay within tolerance. max_abs_diff={feature_max_diff:.3e}")
    else:
        add_finding(findings, "FAIL", "SELECTED_FEATURE_REPLAY_MISMATCH", f"Selected features differ. max_abs_diff={feature_max_diff:.3e}", feature=feature_max_name)

    scored_overlap = scored.merge(merged[[TIME_COL]], on=TIME_COL, how="inner", validate="one_to_one").sort_values(TIME_COL).reset_index(drop=True)
    x = scored_overlap[features].astype("float64")
    model = runtime.load_model()
    raw_replay = positive_class_probability(model, x)
    calibrated_replay = apply_sigmoid_calibrator(raw_replay, calibrator)
    afml_components = payoff_profile_afml_components(
        calibrated_replay,
        payoff_profile,
    )
    afml_replay = afml_components[AFML_BET_SIZE_COL]
    approved_replay = calibrated_replay >= threshold

    payoff_context_mismatches = int(
        np.sum(scored_overlap["payoff_context_id"].astype(str).to_numpy() != payoff_context["payoff_context_id"])
    )
    add_finding(
        findings,
        "PASS" if payoff_context_mismatches == 0 else "FAIL",
        "PAYOFF_CONTEXT_ID_REPLAY",
        f"payoff_context_id mismatches={payoff_context_mismatches}",
    )

    raw_diff = np.abs(scored_overlap[RAW_PROBABILITY_COL].to_numpy(dtype="float64") - raw_replay)
    cal_diff = np.abs(scored_overlap[CALIBRATED_PROBABILITY_COL].to_numpy(dtype="float64") - calibrated_replay)
    reward_risk_diff = np.abs(
        scored_overlap[REWARD_RISK_RATIO_COL].to_numpy(dtype="float64")
        - afml_components[REWARD_RISK_RATIO_COL]
    )
    break_even_diff = np.abs(
        scored_overlap[BREAK_EVEN_PROBABILITY_COL].to_numpy(dtype="float64")
        - afml_components[BREAK_EVEN_PROBABILITY_COL]
    )
    economic_strength_diff = np.abs(
        scored_overlap[AFML_ECONOMIC_STRENGTH_COL].to_numpy(dtype="float64")
        - afml_components[AFML_ECONOMIC_STRENGTH_COL]
    )
    effective_probability_diff = np.abs(
        scored_overlap[AFML_EFFECTIVE_PROBABILITY_COL].to_numpy(dtype="float64")
        - afml_components[AFML_EFFECTIVE_PROBABILITY_COL]
    )
    afml_diff = np.abs(scored_overlap[AFML_BET_SIZE_COL].to_numpy(dtype="float64") - afml_replay)
    sizing_method_mismatches = int(
        np.sum(
            scored_overlap[AFML_SIZING_METHOD_COL].astype(str).to_numpy()
            != AFML_SIZING_METHOD
        )
    )
    threshold_mismatches = int(np.sum(scored_overlap[APPROVED_SIGNAL_COL].astype(bool).to_numpy() != approved_replay.astype(bool)))

    raw_max = float(raw_diff.max()) if len(raw_diff) else np.nan
    cal_max = float(cal_diff.max()) if len(cal_diff) else np.nan
    reward_risk_max = float(reward_risk_diff.max()) if len(reward_risk_diff) else np.nan
    break_even_max = float(break_even_diff.max()) if len(break_even_diff) else np.nan
    economic_strength_max = float(economic_strength_diff.max()) if len(economic_strength_diff) else np.nan
    effective_probability_max = float(effective_probability_diff.max()) if len(effective_probability_diff) else np.nan
    afml_max = float(afml_diff.max()) if len(afml_diff) else np.nan

    add_finding(findings, "PASS" if raw_max <= PROBABILITY_ATOL else "FAIL", "RAW_PROBABILITY_REPLAY", f"Raw replay max diff: {raw_max:.3e}")
    add_finding(findings, "PASS" if cal_max <= PROBABILITY_ATOL else "FAIL", "CALIBRATED_PROBABILITY_REPLAY", f"Calibrated replay max diff: {cal_max:.3e}")
    sizing_contract_ok = (
        reward_risk_max <= AFML_ATOL
        and break_even_max <= AFML_ATOL
        and sizing_method_mismatches == 0
    )
    add_finding(
        findings,
        "PASS" if sizing_contract_ok else "FAIL",
        "AFML_PAYOFF_CONTRACT_REPLAY",
        (
            f"reward_risk_max_diff={reward_risk_max:.3e}; "
            f"break_even_max_diff={break_even_max:.3e}; "
            f"sizing_method_mismatches={sizing_method_mismatches}"
        ),
    )
    add_finding(
        findings,
        "PASS"
        if economic_strength_max <= AFML_ATOL and effective_probability_max <= AFML_ATOL
        else "FAIL",
        "AFML_PAYOFF_COMPONENTS_REPLAY",
        (
            f"economic_strength_max_diff={economic_strength_max:.3e}; "
            f"effective_probability_max_diff={effective_probability_max:.3e}"
        ),
    )
    add_finding(findings, "PASS" if afml_max <= AFML_ATOL else "FAIL", "AFML_BET_SIZE_REPLAY", f"AFML replay max diff: {afml_max:.3e}")
    add_finding(findings, "PASS" if threshold_mismatches == 0 else "FAIL", "THRESHOLD_DECISION_REPLAY", f"Threshold mismatches: {threshold_mismatches}")

    sample = scored_overlap[
        [
            TIME_COL,
            "event_id",
            RAW_PROBABILITY_COL,
            CALIBRATED_PROBABILITY_COL,
            REWARD_RISK_RATIO_COL,
            BREAK_EVEN_PROBABILITY_COL,
            AFML_ECONOMIC_STRENGTH_COL,
            AFML_EFFECTIVE_PROBABILITY_COL,
            AFML_BET_SIZE_COL,
            AFML_SIZING_METHOD_COL,
            APPROVED_SIGNAL_COL,
        ]
    ].copy()
    sample["raw_probability_replay"] = raw_replay
    sample["calibrated_probability_replay"] = calibrated_replay
    sample["reward_risk_ratio_replay"] = afml_components[REWARD_RISK_RATIO_COL]
    sample["break_even_probability_replay"] = afml_components[BREAK_EVEN_PROBABILITY_COL]
    sample["afml_economic_strength_replay"] = afml_components[AFML_ECONOMIC_STRENGTH_COL]
    sample["afml_effective_probability_replay"] = afml_components[AFML_EFFECTIVE_PROBABILITY_COL]
    sample["afml_bet_size_replay"] = afml_replay
    sample["afml_sizing_method_replay"] = AFML_SIZING_METHOD
    sample["approved_signal_replay"] = approved_replay
    sample.head(200).to_csv(layout.reports_dir / "probability_calibration_afml_replay_scored_sample.csv", index=False)

    metrics = {
        "runtime_rows_scored": int(len(scored)),
        "research_model_rows": int(len(research)),
        "research_overlap_rows": overlap_rows,
        "extra_runtime_rows": extra_runtime_rows,
        "missing_runtime_research_rows": missing_runtime_research_rows,
        "approved_signals": int(scored[APPROVED_SIGNAL_COL].sum()),
        "approved_rate": float(scored[APPROVED_SIGNAL_COL].mean()),
        "selected_feature_max_abs_diff": feature_max_diff,
        "selected_feature_max_diff_feature": feature_max_name,
        "raw_replay_max_diff": raw_max,
        "calibrated_replay_max_diff": cal_max,
        "reward_risk_ratio": float(afml_components[REWARD_RISK_RATIO_COL][0]),
        "reward_risk_replay_max_diff": reward_risk_max,
        "break_even_probability": float(afml_components[BREAK_EVEN_PROBABILITY_COL][0]),
        "break_even_replay_max_diff": break_even_max,
        "afml_sizing_method": AFML_SIZING_METHOD,
        "afml_sizing_method_mismatches": sizing_method_mismatches,
        "afml_economic_strength_replay_max_diff": economic_strength_max,
        "afml_effective_probability_replay_max_diff": effective_probability_max,
        "afml_replay_max_diff": afml_max,
        "threshold_mismatches": threshold_mismatches,
        "selected_threshold": float(threshold),
    }
    return finalize(layout, findings, metrics, runtime_manifest, write_report=write_report)


def finalize(layout: EdgeLayout, findings: list[dict[str, Any]], metrics: Mapping[str, Any], runtime_manifest: Any, *, write_report: bool) -> dict[str, Any]:
    fail_count = sum(1 for row in findings if row["severity"] == "FAIL")
    warn_count = sum(1 for row in findings if row["severity"] == "WARN")
    pass_count = sum(1 for row in findings if row["severity"] == "PASS")
    report = {
        "created_at": now_utc(),
        "edge_id": layout.edge_id,
        "script": "shared/edges/probability_calibration_afml_replay_audit.py",
        "replay_audit_status": "FAIL" if fail_count else "PASS",
        "deployable": False,
        "summary": {"pass_count": pass_count, "warn_count": warn_count, "fail_count": fail_count},
        "metrics": dict(metrics),
        "runtime_manifest": runtime_manifest,
        "findings": findings,
    }
    if write_report:
        save_json(layout.replay_audit_report_json, report)
    return report


def print_summary(layout: EdgeLayout, report: Mapping[str, Any]) -> None:
    summary = report.get("summary", {})
    metrics = report.get("metrics", {}) if isinstance(report.get("metrics"), Mapping) else {}
    print("=" * 80, flush=True)
    print(f"V2 STEP 08: PROBABILITY/CALIBRATION/AFML REPLAY AUDIT — {layout.edge_id}", flush=True)
    print("=" * 80, flush=True)
    print(f"Status: {report.get('replay_audit_status')}", flush=True)
    print(f"Pass: {summary.get('pass_count', 0)} | Warn: {summary.get('warn_count', 0)} | Fail: {summary.get('fail_count', 0)}", flush=True)
    if metrics:
        print(f"Runtime rows scored: {metrics.get('runtime_rows_scored'):,}", flush=True)
        print(f"Research overlap rows: {metrics.get('research_overlap_rows'):,}", flush=True)
        print(f"Approved signals: {metrics.get('approved_signals'):,} ({float(metrics.get('approved_rate', 0.0)):.2%})", flush=True)
        print(f"Raw replay max diff: {float(metrics.get('raw_replay_max_diff', float('nan'))):.3e}", flush=True)
        print(f"Calibrated replay max diff: {float(metrics.get('calibrated_replay_max_diff', float('nan'))):.3e}", flush=True)
        print(f"AFML replay max diff: {float(metrics.get('afml_replay_max_diff', float('nan'))):.3e}", flush=True)
        print(f"Threshold mismatches: {metrics.get('threshold_mismatches')}", flush=True)
    print(f"Report: {layout.replay_audit_report_json}", flush=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generic SOTD Step 08 probability/calibration/AFML replay audit.")
    parser.add_argument("--edge-root", required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    layout = get_edge_layout_from_edge_root(args.edge_root)
    report = run_replay_audit(layout.edge_root, write_report=True)
    print_summary(layout, report)
    if report.get("replay_audit_status") != "PASS":
        failures = [row for row in report.get("findings", []) if row.get("severity") == "FAIL"]
        if failures:
            print("\nFailed findings:", flush=True)
            for row in failures:
                print(f"  - {row.get('code')}: {row.get('message')}", flush=True)
        raise SystemExit(1)
    print("✅ Replay audit passed. Edge is ready for edge_manifest export, but remains non-deployable until manifest export and allocator gates.", flush=True)


if __name__ == "__main__":
    main()
