"""Export runtime-built research data for NASDAQ/downsidevolatilitycascade_short.

The edge runtime remains the source of truth for features, candidate events and
model rows. This script validates and persists the selected 1000-bar, 1.5R/1R
label geometry and its reproducible outputs.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_EDGE_ROOT = _THIS_FILE.parent
_V2_ROOT = _THIS_FILE.parents[4]
_REPO_ROOT = _THIS_FILE.parents[5]
for _path in (_EDGE_ROOT, _V2_ROOT, _REPO_ROOT):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

import runtime  # noqa: E402
from candidate_events import get_last_state_machine_diagnostics  # noqa: E402
from shared.io.artifact_hash import hash_artifacts  # noqa: E402

EDGE_ID = "NASDAQ/downsidevolatilitycascade_short"
SIDE_VALUE = -1
EXPORT_NAME = "research_dataset_v3_downsidevolatilitycascade_short_selected_1p5r_1000"
ROW_BUILDER = "downsidevolatilitycascade_short_v3_1p5r_1000_triple_barrier_model_rows"
PRIMARY_SIDE_RULE = (
    "negative completed-bar impulses inside downside variance share >=0.60 and "
    "non-overlapping volatility acceleration >=1.10 map to side=-1 short; "
    "both initial state entries and spaced renewed impulses are valid"
)
EXPECTED_VERTICAL_HORIZON = 1000
EXPECTED_PT_MULTIPLIER = 1.5
EXPECTED_SL_MULTIPLIER = 1.0
GEOMETRY_STATUS = "selected_from_grid_scan"
GRID_SCAN_SELECTED_RANK = 2
GRID_SCAN_SELECTED_ROWS = 2953
GRID_SCAN_SELECTED_NET_MEAN_R = 0.03285
GRID_SCAN_SELECTED_GROSS_MEAN_R = 0.03285
GRID_SCAN_SELECTED_VERTICAL_RATE = 0.0
GRID_SCAN_SELECTED_MEDIAN_HOLD_BARS = 27.0


class ResearchDatasetExportError(RuntimeError):
    pass


def _relative(path: Path) -> str:
    return path.resolve().relative_to(_V2_ROOT.resolve()).as_posix()


def _paths() -> dict[str, Path]:
    root = _EDGE_ROOT / "artifacts" / "research_dataset"
    return {
        "root": root,
        "feature_matrix": root / "nasdaq_downsidevolatilitycascade_short_feature_matrix.csv",
        "candidate_events": root / "nasdaq_downsidevolatilitycascade_short_candidate_events.csv",
        "model_rows": root / "nasdaq_downsidevolatilitycascade_short_model_rows.csv",
        "model_row_manifest": root / "model_row_manifest.json",
        "event_diagnostics": root / "event_diagnostics.json",
        "export_manifest": root / "research_dataset_manifest.json",
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _feature_names() -> list[str]:
    return runtime._schema_feature_names()  # noqa: SLF001


def _feature_export(features: pd.DataFrame, feature_names: list[str]) -> pd.DataFrame:
    missing = [name for name in feature_names if name not in features.columns]
    if missing:
        raise ResearchDatasetExportError(f"Runtime features missing schema fields: {missing}")
    base = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close", "edge_id", "side"]
    columns = [column for column in base if column in features.columns] + feature_names
    return features[columns].copy()


def _validate_cascade_rows(frame: pd.DataFrame, name: str) -> None:
    required = [
        "Downside_Volatility_Cascade_Event", "Cascade_State_Entry",
        "Cascade_Renewed_Impulse", "Cascade_Active_State", "Log_Return_1",
        "Negative_Event_Bar", "Downside_Variance_Share_20",
        "Vol_Acceleration_20_100", "Event_Vol",
    ]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ResearchDatasetExportError(f"{name} missing cascade fields: {missing}")
    for column in [
        "Downside_Volatility_Cascade_Event", "Cascade_Active_State",
        "Negative_Event_Bar",
    ]:
        if not pd.to_numeric(frame[column], errors="coerce").eq(1).all():
            raise ResearchDatasetExportError(f"{name} violate {column}=1")
    state_entry = pd.to_numeric(frame["Cascade_State_Entry"], errors="coerce")
    renewed = pd.to_numeric(frame["Cascade_Renewed_Impulse"], errors="coerce")
    if not state_entry.isin([0, 1]).all() or not renewed.isin([0, 1]).all():
        raise ResearchDatasetExportError(f"{name} contain non-binary cascade type flags")
    if not (state_entry + renewed).eq(1).all():
        raise ResearchDatasetExportError(
            f"{name} must be exactly one of state entry or renewed impulse"
        )
    if not pd.to_numeric(frame["Log_Return_1"], errors="coerce").lt(0).all():
        raise ResearchDatasetExportError(f"{name} contain non-negative event returns")
    if not pd.to_numeric(frame["Downside_Variance_Share_20"], errors="coerce").ge(0.60).all():
        raise ResearchDatasetExportError(f"{name} violate downside share threshold")
    if not pd.to_numeric(frame["Vol_Acceleration_20_100"], errors="coerce").ge(1.10).all():
        raise ResearchDatasetExportError(f"{name} violate acceleration threshold")
    if not pd.to_numeric(frame["Event_Vol"], errors="coerce").gt(0).all():
        raise ResearchDatasetExportError(f"{name} contain invalid Event_Vol")


def _validate(
    features: pd.DataFrame,
    events: pd.DataFrame,
    rows: pd.DataFrame,
    manifest: dict[str, Any],
    feature_names: list[str],
) -> None:
    if not feature_names or features.empty or events.empty or rows.empty:
        raise ResearchDatasetExportError("Research export contains an empty required table")
    if not events["edge_id"].eq(EDGE_ID).all():
        raise ResearchDatasetExportError("Candidate events contain wrong edge_id")
    if not pd.to_numeric(events["side"], errors="coerce").eq(SIDE_VALUE).all():
        raise ResearchDatasetExportError("Runtime candidate events must be side=-1 after mapping")
    if not pd.to_numeric(rows["side"], errors="coerce").eq(SIDE_VALUE).all():
        raise ResearchDatasetExportError("Model rows must be side=-1")
    _validate_cascade_rows(events, "Candidate events")
    _validate_cascade_rows(rows, "Model rows")
    if not rows["pt_price"].lt(rows["entry_price"]).all():
        raise ResearchDatasetExportError("Short PT must be below entry")
    if not rows["sl_price"].gt(rows["entry_price"]).all():
        raise ResearchDatasetExportError("Short SL must be above entry")
    expected = {
        "edge_id": EDGE_ID,
        "side_value": SIDE_VALUE,
        "primary_side_rule": PRIMARY_SIDE_RULE,
        "row_builder": ROW_BUILDER,
        "geometry_status": GEOMETRY_STATUS,
        "geometry_selection_source": "label_geometry_grid_scan",
        "grid_scan_selected_rank": GRID_SCAN_SELECTED_RANK,
        "grid_scan_selected_rows": GRID_SCAN_SELECTED_ROWS,
        "grid_scan_selected_net_mean_R": GRID_SCAN_SELECTED_NET_MEAN_R,
        "grid_scan_selected_gross_mean_R": GRID_SCAN_SELECTED_GROSS_MEAN_R,
        "grid_scan_selected_vertical_rate": GRID_SCAN_SELECTED_VERTICAL_RATE,
        "grid_scan_selected_median_hold_bars": GRID_SCAN_SELECTED_MEDIAN_HOLD_BARS,
        "vertical_horizon": EXPECTED_VERTICAL_HORIZON,
        "pt_multiplier": EXPECTED_PT_MULTIPLIER,
        "sl_multiplier": EXPECTED_SL_MULTIPLIER,
        "short_barrier_contract": "pt_price_below_entry_sl_price_above_entry",
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ResearchDatasetExportError(
                f"Model-row manifest {key} mismatch: {manifest.get(key)!r} != {value!r}"
            )
    forbidden = {
        "meta_label", "t1", "barrier_hit", "resolution_source", "entry_price",
        "pt_price", "sl_price", "realized_r", "raw_probability",
        "calibrated_probability", "afml_bet_size", "approved_signal",
        "Ticks", "Proxy_Value", "Tick_Activity_Ratio", "Proxy_Value_Ratio",
        "Activity_Impulse",
    }
    present = sorted(forbidden.intersection(events.columns))
    if present:
        raise ResearchDatasetExportError(f"Candidate events contain forbidden fields: {present}")


def export_research_dataset(*, overwrite: bool = False, dry_run: bool = False) -> dict[str, Any]:
    started = time.time()
    paths = _paths()
    outputs = [value for key, value in paths.items() if key != "root"]
    if dry_run:
        return {
            "status": "DRY_RUN_PASS",
            "edge_id": EDGE_ID,
            "selected_geometry": {
                "vertical_horizon": EXPECTED_VERTICAL_HORIZON,
                "pt_multiplier": EXPECTED_PT_MULTIPLIER,
                "sl_multiplier": EXPECTED_SL_MULTIPLIER,
            },
            "planned_outputs": {
                key: _relative(value) for key, value in paths.items() if key != "root"
            },
        }
    existing = [path for path in outputs if path.exists()]
    if existing and not overwrite:
        raise ResearchDatasetExportError(
            "Research outputs already exist; use --overwrite intentionally:\n"
            + "\n".join(f"- {path}" for path in existing)
        )
    paths["root"].mkdir(parents=True, exist_ok=True)
    feature_names = _feature_names()
    features = runtime.build_features(drop_warmup_rows=False)
    events = runtime.generate_candidate_events(features)
    rows, row_manifest = runtime.build_model_rows(features=features, events=events)
    feature_frame = _feature_export(features, feature_names)
    diagnostics = get_last_state_machine_diagnostics()
    _validate(feature_frame, events, rows, row_manifest, feature_names)
    feature_frame.to_csv(paths["feature_matrix"], index=False)
    events.to_csv(paths["candidate_events"], index=False)
    rows.to_csv(paths["model_rows"], index=False)
    _write_json(paths["model_row_manifest"], row_manifest)
    _write_json(paths["event_diagnostics"], diagnostics)
    hashes = hash_artifacts(outputs[:-1], root=_V2_ROOT)
    export_manifest = {
        "edge_id": EDGE_ID,
        "export_name": EXPORT_NAME,
        "status": "research_dataset_exported_not_deployable",
        "geometry_status": GEOMETRY_STATUS,
        "geometry_selection_source": "label_geometry_grid_scan",
        "row_builder": ROW_BUILDER,
        "vertical_horizon": EXPECTED_VERTICAL_HORIZON,
        "pt_multiplier": EXPECTED_PT_MULTIPLIER,
        "sl_multiplier": EXPECTED_SL_MULTIPLIER,
        "reward_risk_ratio": EXPECTED_PT_MULTIPLIER / EXPECTED_SL_MULTIPLIER,
        "grid_scan_selected_rank": GRID_SCAN_SELECTED_RANK,
        "grid_scan_selected_rows": GRID_SCAN_SELECTED_ROWS,
        "grid_scan_selected_net_mean_R": GRID_SCAN_SELECTED_NET_MEAN_R,
        "grid_scan_selected_gross_mean_R": GRID_SCAN_SELECTED_GROSS_MEAN_R,
        "grid_scan_selected_vertical_rate": GRID_SCAN_SELECTED_VERTICAL_RATE,
        "grid_scan_selected_median_hold_bars": GRID_SCAN_SELECTED_MEDIAN_HOLD_BARS,
        "runtime_contract_status": runtime.load_contract()["runtime_status"],
        "feature_count": len(feature_names),
        "feature_names": feature_names,
        "rows_feature_matrix": len(feature_frame),
        "rows_candidate_events": len(events),
        "rows_model_rows": len(rows),
        "event_diagnostics": diagnostics,
        "cascade_contract_checks": {
            "side": SIDE_VALUE,
            "downside_variance_share_minimum": 0.60,
            "vol_acceleration_minimum": 1.10,
            "negative_completed_event_bar": True,
            "initial_state_entries_allowed": True,
            "spaced_renewed_impulses_allowed": True,
            "minimum_event_gap_bars": 3,
            "pt_below_entry": True,
            "sl_above_entry": True,
        },
        "outputs": {
            key: _relative(value) for key, value in paths.items() if key != "root"
        },
        "artifact_hashes": hashes,
        "elapsed_seconds": round(time.time() - started, 3),
    }
    _write_json(paths["export_manifest"], export_manifest)
    return export_manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            export_research_dataset(overwrite=args.overwrite, dry_run=args.dry_run),
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
