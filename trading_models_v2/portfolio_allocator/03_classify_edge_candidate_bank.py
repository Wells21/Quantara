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

SCHEMA_VERSION = "v2.2"
ACTIVE_PIPELINE_MODE = "v2_manifest_driven_calibrated_afml"
PROBABILITY_COLUMN = "calibrated_probability"
AFML_COLUMN = "afml_bet_size"

CORE_RULES = {
    "min_eligible_rows": 3,
    "min_best_cumulative_r": 75.0,
    "min_best_profit_factor": 1.08,
    "min_best_calmar_r": 1.00,
    "min_best_expectancy_r": 0.0,
    "require_passed_gates": True,
}

WATCHLIST_RULES = {
    "min_eligible_rows": 1,
    "min_best_cumulative_r": 0.0,
    "min_best_profit_factor": 1.00,
    "min_best_calmar_r": 0.0,
    "min_best_expectancy_r": 0.0,
    "require_passed_gates": True,
}

STRONG_WATCHLIST_RULES = {
    "min_best_cumulative_r": 35.0,
    "min_best_profit_factor": 1.03,
    "min_best_calmar_r": 0.50,
}

CLASS_ORDER = {"core": 0, "watchlist": 1, "fragile_reject": 2}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def safe_float(x: Any, default: float = np.nan) -> float:
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def safe_int(x: Any, default: int = 0) -> int:
    try:
        if pd.isna(x):
            return default
        return int(float(x))
    except Exception:
        return default


def normalize_bool(x: Any) -> bool:
    return str(x).strip().lower() in {"true", "1", "yes", "y"}


def class_sort(series: pd.Series) -> pd.Series:
    return series.map(CLASS_ORDER).fillna(99).astype(int)


def load_candidate_bank(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Run 02_build_edge_candidate_bank.py first. Missing: {path}")
    df = pd.read_csv(path)
    required = [
        "candidate_id", "edge_id", "asset", "family", "side_name", "side_value",
        "candidate_type", "threshold", "threshold_probability_column", "calibration_method",
        "afml_bet_size_column", "trade_count", "expectancy_r", "profit_factor",
        "cumulative_r", "max_drawdown_r", "calmar_r", "local_positive_ratio",
        "avg_calibrated_probability", "avg_afml_bet_size", "calibrated_oof_path",
        "threshold_sweep_path", "edge_manifest_path",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Candidate bank missing required columns: {missing}")
    if df.empty:
        raise ValueError("Candidate bank is empty.")
    if "edge_manifest_hash" not in df.columns:
        df["edge_manifest_hash"] = ""
    if df["candidate_id"].duplicated().any():
        raise ValueError("Duplicate candidate_id values found in candidate bank.")
    numeric = [
        "side_value", "threshold", "trade_count", "take_rate", "win_rate", "expectancy_r",
        "profit_factor", "gross_profit_r", "gross_loss_r", "cumulative_r", "max_drawdown_r",
        "calmar_r", "precision", "local_positive_ratio", "local_expectancy_mean", "local_pf_mean",
        "balanced_score", "recall_score", "conservative_score", "score_afml_size",
        "score_calibrated_prob", "avg_calibrated_probability", "avg_afml_bet_size",
    ]
    for col in numeric:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if set(df["threshold_probability_column"].dropna().unique()) != {PROBABILITY_COLUMN}:
        raise ValueError("All candidates must use calibrated_probability threshold column.")
    if set(df["afml_bet_size_column"].dropna().unique()) != {AFML_COLUMN}:
        raise ValueError("All candidates must carry afml_bet_size metadata.")
    return df


def load_viability(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Run 02_build_edge_candidate_bank.py first. Missing: {path}")
    df = pd.read_csv(path)
    required = [
        "edge_id", "asset", "family", "side_name", "side_value", "passed_gates",
        "eligible_rows", "rows_in_sweep", "best_cumulative_r", "best_expectancy_r",
        "best_profit_factor", "best_calmar_r", "empty_sweep", "threshold_sweep_path",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Viability report missing required columns: {missing}")
    numeric = [
        "side_value", "eligible_rows", "rows_in_sweep", "best_cumulative_r",
        "best_expectancy_r", "best_profit_factor", "best_calmar_r", "best_local_positive_ratio",
    ]
    for col in numeric:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "edge_manifest_hash" not in df.columns:
        df["edge_manifest_hash"] = ""
    for col in ["passed_gates", "empty_sweep"]:
        if col in df.columns:
            df[col] = df[col].apply(normalize_bool)
    return df


def candidate_summary_by_edge(candidates: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for edge_id, g in candidates.groupby("edge_id", sort=True):
        first = g.iloc[0]
        rows.append({
            "edge_id": edge_id,
            "candidate_rows": int(len(g)),
            "candidate_types": "|".join(sorted(g["candidate_type"].astype(str).unique())),
            "best_candidate_id_by_cumulative_r": g.sort_values(["cumulative_r", "trade_count", "calmar_r"], ascending=[False, False, False]).iloc[0]["candidate_id"],
            "best_candidate_id_by_calmar": g.sort_values(["calmar_r", "profit_factor", "cumulative_r"], ascending=[False, False, False]).iloc[0]["candidate_id"],
            "best_candidate_id_by_afml_size": g.sort_values(["avg_afml_bet_size", "avg_calibrated_probability", "calmar_r"], ascending=[False, False, False]).iloc[0]["candidate_id"],
            "candidate_threshold_min": float(g["threshold"].min()),
            "candidate_threshold_max": float(g["threshold"].max()),
            "candidate_trade_count_max": int(g["trade_count"].max()),
            "candidate_trade_count_min": int(g["trade_count"].min()),
            "candidate_best_cumulative_r": float(g["cumulative_r"].max()),
            "candidate_best_expectancy_r": float(g["expectancy_r"].max()),
            "candidate_best_profit_factor": float(g["profit_factor"].max()),
            "candidate_best_calmar_r": float(g["calmar_r"].max()),
            "candidate_max_avg_calibrated_probability": float(g["avg_calibrated_probability"].max()),
            "candidate_max_avg_afml_bet_size": float(g["avg_afml_bet_size"].max()),
            "candidate_mean_avg_afml_bet_size": float(g["avg_afml_bet_size"].mean()),
            "candidate_mean_avg_calibrated_probability": float(g["avg_calibrated_probability"].mean()),
            "asset": first["asset"],
            "family": first["family"],
            "side_name": first["side_name"],
            "side_value": int(first["side_value"]),
            "calibration_method": first["calibration_method"],
            "threshold_probability_column": first["threshold_probability_column"],
            "afml_bet_size_column": first["afml_bet_size_column"],
            "calibrated_oof_path": first["calibrated_oof_path"],
            "threshold_sweep_path": first["threshold_sweep_path"],
            "edge_manifest_path": first["edge_manifest_path"],
        })
    return pd.DataFrame(rows)


def passes_core(row: pd.Series) -> bool:
    if CORE_RULES["require_passed_gates"] and not bool(row.get("passed_gates", False)):
        return False
    return (
        safe_int(row.get("eligible_rows")) >= CORE_RULES["min_eligible_rows"]
        and safe_float(row.get("best_cumulative_r")) >= CORE_RULES["min_best_cumulative_r"]
        and safe_float(row.get("best_profit_factor")) >= CORE_RULES["min_best_profit_factor"]
        and safe_float(row.get("best_calmar_r")) >= CORE_RULES["min_best_calmar_r"]
        and safe_float(row.get("best_expectancy_r")) > CORE_RULES["min_best_expectancy_r"]
    )


def passes_watchlist(row: pd.Series) -> bool:
    if WATCHLIST_RULES["require_passed_gates"] and not bool(row.get("passed_gates", False)):
        return False
    return (
        safe_int(row.get("eligible_rows")) >= WATCHLIST_RULES["min_eligible_rows"]
        and safe_float(row.get("best_cumulative_r")) > WATCHLIST_RULES["min_best_cumulative_r"]
        and safe_float(row.get("best_profit_factor")) > WATCHLIST_RULES["min_best_profit_factor"]
        and safe_float(row.get("best_calmar_r")) > WATCHLIST_RULES["min_best_calmar_r"]
        and safe_float(row.get("best_expectancy_r")) > WATCHLIST_RULES["min_best_expectancy_r"]
    )


def is_strong_watchlist(row: pd.Series) -> bool:
    return (
        safe_float(row.get("best_cumulative_r")) >= STRONG_WATCHLIST_RULES["min_best_cumulative_r"]
        and safe_float(row.get("best_profit_factor")) >= STRONG_WATCHLIST_RULES["min_best_profit_factor"]
        and safe_float(row.get("best_calmar_r")) >= STRONG_WATCHLIST_RULES["min_best_calmar_r"]
    )


def classify_edge(row: pd.Series) -> tuple[str, str, str]:
    if bool(row.get("empty_sweep", False)):
        return "fragile_reject", "empty_calibrated_threshold_sweep", ""
    if not bool(row.get("passed_gates", False)):
        if safe_int(row.get("eligible_rows", 0)) <= 0:
            return "fragile_reject", "did_not_pass_step02_viability_gates", ""
        return "fragile_reject", "failed_core_watchlist_rules_after_candidate_bank", ""
    if passes_core(row):
        return "core", "passed_core_rules", ""
    if passes_watchlist(row):
        return "watchlist", "passed_watchlist_rules", "strong_watchlist" if is_strong_watchlist(row) else "thin_watchlist"
    return "fragile_reject", "passed_candidate_bank_but_failed_watchlist_safety_floor", ""


def build_classification_report(candidates: pd.DataFrame, viability: pd.DataFrame) -> pd.DataFrame:
    summary = candidate_summary_by_edge(candidates)
    report = viability.merge(
        summary,
        on=["edge_id", "asset", "family", "side_name", "side_value", "threshold_sweep_path"],
        how="left",
        suffixes=("", "_candidate"),
    )
    for col in ["candidate_rows", "candidate_trade_count_max", "candidate_trade_count_min"]:
        if col in report.columns:
            report[col] = report[col].fillna(0).astype(int)
    for col in ["candidate_types", "best_candidate_id_by_cumulative_r", "best_candidate_id_by_calmar", "best_candidate_id_by_afml_size"]:
        if col in report.columns:
            report[col] = report[col].fillna("")
    cls = report.apply(classify_edge, axis=1, result_type="expand")
    cls.columns = ["edge_classification", "classification_reason", "watchlist_quality_tag"]
    report = pd.concat([report, cls], axis=1)
    report["classification_rank_score"] = (
        0.30 * report["best_cumulative_r"].fillna(0).rank(pct=True)
        + 0.25 * report["best_calmar_r"].fillna(-999).rank(pct=True)
        + 0.20 * report["best_profit_factor"].fillna(0).rank(pct=True)
        + 0.15 * report["eligible_rows"].fillna(0).rank(pct=True)
        + 0.10 * report["candidate_max_avg_afml_bet_size"].fillna(0).rank(pct=True)
    )
    report["class_sort"] = class_sort(report["edge_classification"])
    return report.sort_values(["class_sort", "asset", "family", "edge_id"]).drop(columns=["class_sort"]).reset_index(drop=True)


def attach_classification(candidates: pd.DataFrame, report: pd.DataFrame) -> pd.DataFrame:
    class_cols = [
        "edge_id", "edge_classification", "classification_reason", "watchlist_quality_tag",
        "classification_rank_score", "passed_gates", "eligible_rows", "rows_in_sweep",
        "best_cumulative_r", "best_expectancy_r", "best_profit_factor", "best_calmar_r",
        "best_local_positive_ratio", "empty_sweep",
    ]
    out = candidates.merge(report[[c for c in class_cols if c in report.columns]], on="edge_id", how="left")
    out["edge_classification"] = out["edge_classification"].fillna("fragile_reject")
    out["classification_reason"] = out["classification_reason"].fillna("missing_classification_report_row")
    out["class_sort"] = class_sort(out["edge_classification"])
    return out.sort_values(["class_sort", "asset", "family", "edge_id", "candidate_type", "threshold"]).drop(columns=["class_sort"]).reset_index(drop=True)


def validate_outputs(filtered: pd.DataFrame, report: pd.DataFrame) -> None:
    problems = []
    if filtered["candidate_id"].duplicated().any():
        problems.append("Duplicate candidate_id values after classification merge.")
    if filtered["edge_classification"].isna().any():
        problems.append("Candidate rows missing edge_classification.")
    allowed = {"core", "watchlist", "fragile_reject"}
    bad = set(filtered["edge_classification"].dropna().unique()) - allowed
    if bad:
        problems.append(f"Unexpected edge_classification values: {sorted(bad)}")
    if report["edge_id"].duplicated().any():
        problems.append("Duplicate edge_id rows in classification report.")
    required = [
        "candidate_id", "edge_id", "asset", "family", "side_name", "candidate_type",
        "threshold", "edge_classification", "calibrated_oof_path",
        "threshold_probability_column", "afml_bet_size_column",
    ]
    missing = [c for c in required if c not in filtered.columns]
    if missing:
        problems.append(f"Filtered candidate bank missing downstream columns: {missing}")
    if problems:
        raise RuntimeError("\n".join(problems))


def build(write: bool = True) -> dict[str, Any]:
    out_dir = V2_ROOT / "portfolio_allocator" / "outputs"
    candidates = load_candidate_bank(out_dir / "v2_edge_candidate_bank.csv")
    viability = load_viability(out_dir / "v2_edge_viability_report.csv")
    report = build_classification_report(candidates, viability)
    filtered = attach_classification(candidates, report)
    validate_outputs(filtered, report)

    core = filtered[filtered["edge_classification"] == "core"].copy()
    watchlist = filtered[filtered["edge_classification"] == "watchlist"].copy()
    core_plus_watchlist = filtered[filtered["edge_classification"].isin(["core", "watchlist"])].copy()
    rejected = filtered[filtered["edge_classification"] == "fragile_reject"].copy()

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "pipeline_mode": ACTIVE_PIPELINE_MODE,
        "classification_status": "PASS",
        "input_candidate_bank_csv": "portfolio_allocator/outputs/v2_edge_candidate_bank.csv",
        "input_viability_report_csv": "portfolio_allocator/outputs/v2_edge_viability_report.csv",
        "counts": {
            "input_candidate_rows": int(len(candidates)),
            "input_unique_edges": int(candidates["edge_id"].nunique()),
            "classification_edge_counts": report["edge_classification"].value_counts().sort_index().to_dict(),
            "candidate_row_counts": filtered["edge_classification"].value_counts().sort_index().to_dict(),
            "core_candidate_rows": int(len(core)),
            "watchlist_candidate_rows": int(len(watchlist)),
            "core_plus_watchlist_candidate_rows": int(len(core_plus_watchlist)),
            "rejected_candidate_rows": int(len(rejected)),
            "core_edges": int(core["edge_id"].nunique()) if not core.empty else 0,
            "watchlist_edges": int(watchlist["edge_id"].nunique()) if not watchlist.empty else 0,
            "core_plus_watchlist_edges": int(core_plus_watchlist["edge_id"].nunique()) if not core_plus_watchlist.empty else 0,
        },
        "classification_rules": {
            "core_rules": CORE_RULES,
            "watchlist_rules": WATCHLIST_RULES,
            "strong_watchlist_rules": STRONG_WATCHLIST_RULES,
        },
        "outputs": {
            "filtered_csv": "portfolio_allocator/outputs/v2_edge_candidate_bank_filtered.csv",
            "filtered_json": "portfolio_allocator/outputs/v2_edge_candidate_bank_filtered.json",
            "classification_report_csv": "portfolio_allocator/outputs/v2_edge_classification_report.csv",
            "core_csv": "portfolio_allocator/outputs/v2_edge_candidate_bank_core.csv",
            "watchlist_csv": "portfolio_allocator/outputs/v2_edge_candidate_bank_watchlist.csv",
            "core_plus_watchlist_csv": "portfolio_allocator/outputs/v2_edge_candidate_bank_core_plus_watchlist.csv",
            "rejected_csv": "portfolio_allocator/outputs/v2_edge_candidate_bank_rejected.csv",
            "manifest_json": "portfolio_allocator/outputs/v2_edge_candidate_bank_filter_manifest.json",
        },
        "notes": [
            "Port of V1 Step 03B core/watchlist/fragile logic using V2 edge_id instead of V1 leg_id.",
            "This step classifies candidates only; final portfolio threshold/weight selection remains downstream.",
            "Watchlist remains intentionally inclusive so HRP and rolling simulation can decide portfolio usefulness.",
            "File-content hashes are optional compatibility metadata and are not merge keys or classification gates.",
        ],
    }
    if write:
        filtered.to_csv(out_dir / "v2_edge_candidate_bank_filtered.csv", index=False)
        filtered.to_json(out_dir / "v2_edge_candidate_bank_filtered.json", orient="records", indent=2)
        report.to_csv(out_dir / "v2_edge_classification_report.csv", index=False)
        core.to_csv(out_dir / "v2_edge_candidate_bank_core.csv", index=False)
        watchlist.to_csv(out_dir / "v2_edge_candidate_bank_watchlist.csv", index=False)
        core_plus_watchlist.to_csv(out_dir / "v2_edge_candidate_bank_core_plus_watchlist.csv", index=False)
        rejected.to_csv(out_dir / "v2_edge_candidate_bank_rejected.csv", index=False)
        write_json(out_dir / "v2_edge_candidate_bank_filter_manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    manifest = build(write=not args.dry_run)
    print("V2 Phase B2.03 classify edge candidate bank")
    print(f"Status: {manifest['classification_status']}")
    counts = manifest["counts"]
    print(f"Input candidate rows: {counts['input_candidate_rows']}")
    print(f"Core edges: {counts['core_edges']}")
    print(f"Watchlist edges: {counts['watchlist_edges']}")
    print(f"Core+watchlist candidate rows: {counts['core_plus_watchlist_candidate_rows']}")
    print(f"Rejected candidate rows: {counts['rejected_candidate_rows']}")


if __name__ == "__main__":
    main()
