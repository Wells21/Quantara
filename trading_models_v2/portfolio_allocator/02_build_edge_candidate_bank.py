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

from shared.edges.trade_geometry import GEOMETRY_COLUMNS, geometry_from_mapping  # noqa: E402

SCHEMA_VERSION = "v2.2"
PROBABILITY_COLUMN = "calibrated_probability"
AFML_COLUMN = "afml_bet_size"
LOCAL_WINDOW = 5
ALLOW_EMPTY_SWEEPS = True

EXPECTED_SIDE_GATES = {
    "min_trade_abs": 100,
    "min_trade_frac_of_side_max": 0.08,
    "min_expectancy_r": 0.0,
    "min_profit_factor": 1.02,
    "min_cumulative_r": 0.0,
    "min_calmar_r": 0.50,
    "min_local_positive_ratio": 0.60,
}

OPPOSITE_SIDE_GATES = {
    "min_trade_abs": 100,
    "min_trade_frac_of_side_max": 0.08,
    "min_expectancy_r": 0.0,
    "min_profit_factor": 1.05,
    "min_cumulative_r": 0.0,
    "min_calmar_r": 1.00,
    "min_local_positive_ratio": 0.70,
}


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


def clean_threshold_for_id(threshold: float) -> str:
    return f"{threshold:.2f}".replace(".", "p")


def empty_sweep_template() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "threshold", "calibration_method", "probability_column", "take_rate", "precision",
        "avg_pred_prob", "avg_afml_bet_size", "trade_count", "win_rate", "expectancy_r",
        "profit_factor", "gross_profit_r", "gross_loss_r", "cumulative_r",
        "max_drawdown_r", "calmar_r",
    ])


def load_sweep(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Calibrated threshold sweep file does not exist: {path}")
    df = pd.read_csv(path)
    if df.empty:
        return empty_sweep_template() if ALLOW_EMPTY_SWEEPS else df
    required = ["threshold", "trade_count", "expectancy_r", "profit_factor", "cumulative_r", "max_drawdown_r"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Sweep missing required columns {missing}: {path}")
    for col in ["win_rate", "take_rate", "precision", "avg_pred_prob", "avg_afml_bet_size", "calmar_r"]:
        if col not in df.columns:
            df[col] = np.nan
    numeric_cols = [
        "threshold", "trade_count", "win_rate", "expectancy_r", "profit_factor", "gross_profit_r",
        "gross_loss_r", "cumulative_r", "max_drawdown_r", "calmar_r", "take_rate",
        "precision", "avg_pred_prob", "avg_afml_bet_size",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    dd_abs = df["max_drawdown_r"].apply(lambda x: abs(min(safe_float(x, 0.0), 0.0)))
    computed_calmar = df["cumulative_r"] / dd_abs.replace(0.0, np.nan)
    df["calmar_r"] = df["calmar_r"].fillna(computed_calmar)
    for col in ["profit_factor", "calmar_r"]:
        finite = df[col].replace([np.inf, -np.inf], np.nan)
        max_finite = finite.max(skipna=True)
        min_finite = finite.min(skipna=True)
        if pd.notna(max_finite):
            df[col] = df[col].replace(np.inf, max_finite * 1.25)
        if pd.notna(min_finite):
            df[col] = df[col].replace(-np.inf, min_finite * 1.25)
    df = df.dropna(subset=["threshold", "trade_count", "expectancy_r", "profit_factor", "cumulative_r"])
    return df.sort_values("threshold").reset_index(drop=True)


def add_local_stability(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    positive = (out["expectancy_r"] > 0) & (out["profit_factor"] > 1.0) & (out["cumulative_r"] > 0)
    out["is_positive_core"] = positive.astype(int)
    out["local_positive_ratio"] = out["is_positive_core"].rolling(LOCAL_WINDOW, center=True, min_periods=1).mean()
    out["local_expectancy_mean"] = out["expectancy_r"].rolling(LOCAL_WINDOW, center=True, min_periods=1).mean()
    out["local_pf_mean"] = out["profit_factor"].rolling(LOCAL_WINDOW, center=True, min_periods=1).mean()
    return out


def apply_gates(df: pd.DataFrame, gates: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    if df.empty:
        return df.copy(), {
            "max_trade_count_in_side_sweep": 0,
            "min_trade_count_required": int(gates["min_trade_abs"]),
            "rows_in_sweep": 0,
            "eligible_rows": 0,
            "best_expectancy_r": np.nan,
            "best_profit_factor": np.nan,
            "best_cumulative_r": np.nan,
            "best_calmar_r": np.nan,
            "best_local_positive_ratio": np.nan,
            "empty_sweep": True,
        }
    out = add_local_stability(df)
    max_trades = int(out["trade_count"].max())
    min_trade_count = max(int(gates["min_trade_abs"]), int(np.floor(max_trades * gates["min_trade_frac_of_side_max"])))
    eligible = out[
        (out["trade_count"] >= min_trade_count)
        & (out["expectancy_r"] > gates["min_expectancy_r"])
        & (out["profit_factor"] >= gates["min_profit_factor"])
        & (out["cumulative_r"] > gates["min_cumulative_r"])
        & (out["calmar_r"] >= gates["min_calmar_r"])
        & (out["local_positive_ratio"] >= gates["min_local_positive_ratio"])
    ].copy()
    diagnostics = {
        "max_trade_count_in_side_sweep": max_trades,
        "min_trade_count_required": min_trade_count,
        "rows_in_sweep": int(len(out)),
        "eligible_rows": int(len(eligible)),
        "best_expectancy_r": float(out["expectancy_r"].max()),
        "best_profit_factor": float(out["profit_factor"].max()),
        "best_cumulative_r": float(out["cumulative_r"].max()),
        "best_calmar_r": float(out["calmar_r"].max()),
        "best_local_positive_ratio": float(out["local_positive_ratio"].max()),
        "empty_sweep": False,
    }
    return eligible, diagnostics


def percentile_rank(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    if s.nunique(dropna=True) <= 1:
        return pd.Series(0.5, index=s.index)
    if higher_is_better:
        return s.rank(pct=True, ascending=True)
    return 1.0 - s.rank(pct=True, ascending=True) + (1.0 / s.notna().sum())


def add_candidate_scores(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["score_trade_count"] = percentile_rank(out["trade_count"], True)
    out["score_expectancy"] = percentile_rank(out["expectancy_r"], True)
    out["score_profit_factor"] = percentile_rank(out["profit_factor"], True)
    out["score_cumulative"] = percentile_rank(out["cumulative_r"], True)
    out["score_calmar"] = percentile_rank(out["calmar_r"], True)
    out["score_local_stability"] = percentile_rank(out["local_positive_ratio"], True)
    out["score_afml_size"] = percentile_rank(out.get("avg_afml_bet_size", pd.Series(0.5, index=out.index)), True)
    out["score_calibrated_prob"] = percentile_rank(out.get("avg_pred_prob", pd.Series(0.5, index=out.index)), True)
    out["balanced_score"] = 0.18*out["score_trade_count"] + 0.20*out["score_expectancy"] + 0.15*out["score_profit_factor"] + 0.20*out["score_cumulative"] + 0.20*out["score_calmar"] + 0.04*out["score_local_stability"] + 0.03*out["score_afml_size"]
    out["recall_score"] = 0.60*out["score_trade_count"] + 0.18*out["score_cumulative"] + 0.10*out["score_expectancy"] + 0.07*out["score_local_stability"] + 0.05*out["score_afml_size"]
    out["conservative_score"] = 0.43*out["score_calmar"] + 0.20*out["score_profit_factor"] + 0.15*out["score_expectancy"] + 0.10*out["score_local_stability"] + 0.07*out["score_trade_count"] + 0.05*out["score_afml_size"]
    return out


def pick_candidate_rows(eligible: pd.DataFrame) -> dict[str, pd.Series]:
    if eligible.empty:
        return {}
    scored = add_candidate_scores(eligible)
    return {
        "high_recall": scored.sort_values(["recall_score", "trade_count", "cumulative_r"], ascending=[False, False, False]).iloc[0],
        "max_total_r": scored.sort_values(["cumulative_r", "trade_count", "expectancy_r"], ascending=[False, False, False]).iloc[0],
        "balanced": scored.sort_values(["balanced_score", "cumulative_r", "calmar_r"], ascending=[False, False, False]).iloc[0],
        "conservative": scored.sort_values(["conservative_score", "calmar_r", "profit_factor"], ascending=[False, False, False]).iloc[0],
        "max_afml_size": scored.sort_values(["score_afml_size", "calmar_r", "profit_factor", "trade_count"], ascending=[False, False, False, False]).iloc[0],
    }


def candidate_id(edge_id: str, kind: str, threshold: float) -> str:
    return f"{edge_id.replace('/', '_')}_{kind}_thr{clean_threshold_for_id(threshold)}"


def edge_geometry(edge: dict[str, Any]) -> dict[str, Any]:
    geometry = geometry_from_mapping(edge, expected_side_name=str(edge.get("side_name", "")))
    nested = edge.get("trade_geometry")
    if isinstance(nested, dict):
        nested_geometry = geometry_from_mapping(
            {**nested, "edge_id": edge.get("edge_id"), "side_name": edge.get("side_name")},
            expected_side_name=str(edge.get("side_name", "")),
        )
        if nested_geometry["geometry_hash"] != geometry["geometry_hash"]:
            raise ValueError(f"{edge.get('edge_id')}: flattened/nested geometry hash mismatch")
    return geometry


def row_to_candidate(edge: dict[str, Any], kind: str, pick: pd.Series, gate_profile: str, sweep_path: Path) -> dict[str, Any]:
    threshold = float(pick["threshold"])
    geometry = edge_geometry(edge)
    return {
        "candidate_id": candidate_id(edge["edge_id"], kind, threshold),
        "edge_id": edge["edge_id"],
        "asset": edge["asset"],
        "family": edge["family"],
        "side_name": edge["side_name"],
        "side_value": int(edge["side_value"]),
        "candidate_type": kind,
        "gate_profile": gate_profile,
        **geometry,
        "edge_yaml_path": edge.get("edge_yaml_path", ""),
        "edge_yaml_hash": edge.get("edge_yaml_hash", ""),
        "threshold": threshold,
        "threshold_probability_column": PROBABILITY_COLUMN,
        "calibration_method": "sigmoid",
        "afml_bet_size_column": AFML_COLUMN,
        "trade_count": int(pick["trade_count"]),
        "take_rate": safe_float(pick.get("take_rate")),
        "win_rate": safe_float(pick.get("win_rate")),
        "expectancy_r": safe_float(pick.get("expectancy_r")),
        "profit_factor": safe_float(pick.get("profit_factor")),
        "gross_profit_r": safe_float(pick.get("gross_profit_r")),
        "gross_loss_r": safe_float(pick.get("gross_loss_r")),
        "cumulative_r": safe_float(pick.get("cumulative_r")),
        "max_drawdown_r": safe_float(pick.get("max_drawdown_r")),
        "calmar_r": safe_float(pick.get("calmar_r")),
        "precision": safe_float(pick.get("precision")),
        "local_positive_ratio": safe_float(pick.get("local_positive_ratio")),
        "local_expectancy_mean": safe_float(pick.get("local_expectancy_mean")),
        "local_pf_mean": safe_float(pick.get("local_pf_mean")),
        "balanced_score": safe_float(pick.get("balanced_score")),
        "recall_score": safe_float(pick.get("recall_score")),
        "conservative_score": safe_float(pick.get("conservative_score")),
        "score_afml_size": safe_float(pick.get("score_afml_size")),
        "score_calibrated_prob": safe_float(pick.get("score_calibrated_prob")),
        "avg_calibrated_probability": safe_float(pick.get("avg_pred_prob")),
        "avg_afml_bet_size": safe_float(pick.get("avg_afml_bet_size")),
        "calibrated_oof_path": edge["artifacts"].get("calibrated_oof_predictions"),
        "threshold_sweep_path": str(sweep_path.relative_to(V2_ROOT)),
        "edge_manifest_path": edge["edge_manifest_path"],
        "edge_manifest_hash": edge.get("edge_manifest_hash", ""),
        "candidate_status": "candidate",
        "notes": "V2 manifest-driven calibrated candidate. Final threshold still belongs to portfolio simulation/HRP/stress pipeline.",
    }


def evaluate_edge(edge: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sweep_rel = edge["artifacts"].get("threshold_sweep")
    if not sweep_rel:
        raise ValueError(f"{edge['edge_id']}: edge manifest missing threshold_sweep artifact")
    sweep_path = V2_ROOT / sweep_rel
    sweep = load_sweep(sweep_path)
    gates = EXPECTED_SIDE_GATES
    gate_profile = "expected_side"
    eligible, diagnostics = apply_gates(sweep, gates)
    geometry = edge_geometry(edge)
    viability = {
        "edge_id": edge["edge_id"],
        "asset": edge["asset"],
        "family": edge["family"],
        "side_name": edge["side_name"],
        "side_value": int(edge["side_value"]),
        **geometry,
        "edge_yaml_path": edge.get("edge_yaml_path", ""),
        "edge_yaml_hash": edge.get("edge_yaml_hash", ""),
        "gate_profile": gate_profile,
        "passed_gates": bool(len(eligible) > 0),
        "threshold_sweep_path": str(sweep_path.relative_to(V2_ROOT)),
        "edge_manifest_hash": edge.get("edge_manifest_hash", ""),
        **diagnostics,
    }
    candidates = [row_to_candidate(edge, kind, pick, gate_profile, sweep_path) for kind, pick in pick_candidate_rows(eligible).items()]
    return candidates, viability


def validate_geometry_consistency(candidate_df: pd.DataFrame, viability_df: pd.DataFrame) -> None:
    for name, df in (("candidate bank", candidate_df), ("viability report", viability_df)):
        missing = [c for c in GEOMETRY_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"{name} missing finalized geometry columns: {missing}")
        conflicts = df.groupby("edge_id")["geometry_hash"].nunique(dropna=False)
        bad = conflicts[conflicts != 1]
        if not bad.empty:
            raise ValueError(f"{name} has multiple geometry hashes for edge(s): {bad.to_dict()}")


def build(write: bool = True) -> dict[str, Any]:
    active_payload = load_json(V2_ROOT / "portfolio_allocator" / "outputs" / "active_edge_universe.json")
    if active_payload.get("ingestion_status") != "PASS":
        raise ValueError("active_edge_universe ingestion_status must be PASS")
    candidates: list[dict[str, Any]] = []
    viability: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for edge in active_payload.get("active_edges", []):
        try:
            edge_candidates, edge_viability = evaluate_edge(edge)
            candidates.extend(edge_candidates)
            viability.append(edge_viability)
        except Exception as exc:
            failures.append({"edge_id": str(edge.get("edge_id")), "error": str(exc)})
    candidate_df = pd.DataFrame(candidates)
    viability_df = pd.DataFrame(viability)
    if not candidate_df.empty and not viability_df.empty:
        validate_geometry_consistency(candidate_df, viability_df)
    status = "FAIL" if failures or candidate_df.empty else "PASS"
    out = V2_ROOT / "portfolio_allocator" / "outputs"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "candidate_bank_status": status,
        "input_active_edge_universe": "portfolio_allocator/outputs/active_edge_universe.json",
        "candidate_rows": int(len(candidate_df)),
        "unique_edges": int(candidate_df["edge_id"].nunique()) if not candidate_df.empty else 0,
        "geometry_contract_version": "trade_geometry_v1",
        "geometry_columns": list(GEOMETRY_COLUMNS),
        "failures": failures,
        "expected_side_gates": EXPECTED_SIDE_GATES,
        "local_window": LOCAL_WINDOW,
        "outputs": {
            "candidate_bank_csv": "portfolio_allocator/outputs/v2_edge_candidate_bank.csv",
            "candidate_bank_json": "portfolio_allocator/outputs/v2_edge_candidate_bank.json",
            "viability_report_csv": "portfolio_allocator/outputs/v2_edge_viability_report.csv",
            "manifest_json": "portfolio_allocator/outputs/v2_edge_candidate_bank_manifest.json",
        },
        "notes": [
            "Port of V1 Step 03 core logic using V2 deployability-PASS edge manifests as inputs.",
            "Candidate thresholds are selected in calibrated_probability space only.",
            "Finalized trade geometry is metadata-only here and does not alter candidate ranking or gates.",
            "This step does not select final live thresholds or weights.",
            "File-content hashes are optional compatibility metadata and are not validated or used for candidate selection.",
            "geometry_hash remains a semantic trade-geometry fingerprint, not an artifact-file invalidation hash.",
        ],
    }
    if write:
        out.mkdir(parents=True, exist_ok=True)
        candidate_df.to_csv(out / "v2_edge_candidate_bank.csv", index=False)
        candidate_df.to_json(out / "v2_edge_candidate_bank.json", orient="records", indent=2)
        viability_df.to_csv(out / "v2_edge_viability_report.csv", index=False)
        write_json(out / "v2_edge_candidate_bank_manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    manifest = build(write=not args.dry_run)
    print("V2 Phase B2.02 manifest-driven edge candidate bank")
    print(f"Status: {manifest['candidate_bank_status']}")
    print(f"Candidate rows: {manifest['candidate_rows']}")
    print(f"Unique edges: {manifest['unique_edges']}")
    for row in manifest["failures"]:
        print(f"FAIL {row['edge_id']}: {row['error']}")
    if manifest["candidate_bank_status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
