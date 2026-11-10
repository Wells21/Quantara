"""
Export reproducible research datasets for XAUUSD/roundlevelcascade_long.

The edge runtime is the source of truth. This exporter persists what
runtime.py builds so feature surgery/model training can happen without
re-engineering features outside the edge runtime.
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
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

import runtime  # noqa: E402
from shared.io.artifact_hash import hash_artifacts  # noqa: E402
from shared.labels.economic_meta_label import enforce_gross_economic_model_row_contract  # noqa: E402

EDGE_ID = "XAUUSD/roundlevelcascade_long"
SIDE_VALUE = 1
EXPORT_NAME = "research_dataset_v1_roundlevelcascade_long"
PRIMARY_SIDE_RULE = "Round_Break_Type == +1 (first close above an untouched round level) -> side=+1 long"
ROW_BUILDER = "roundlevelcascade_long_v1_1r_triple_barrier_model_rows"


class ResearchDatasetExportError(RuntimeError):
    """Raised when exporting research artifacts must fail closed."""


def _relative_to_v2(path: Path) -> str:
    return path.resolve().relative_to(_V2_ROOT.resolve()).as_posix()


def _output_paths() -> dict[str, Path]:
    root = _EDGE_ROOT / "artifacts" / "research_dataset"
    return {
        "root": root,
        "feature_matrix": root / "xauusd_roundlevelcascade_long_feature_matrix.csv",
        "candidate_events": root / "xauusd_roundlevelcascade_long_candidate_events.csv",
        "model_rows": root / "xauusd_roundlevelcascade_long_model_rows.csv",
        "model_row_manifest": root / "model_row_manifest.json",
        "export_manifest": root / "research_dataset_manifest.json",
    }


def _check_safe_to_write(paths: list[Path], overwrite: bool) -> None:
    existing = [path for path in paths if path.exists()]
    if existing and not overwrite:
        formatted = "\n".join(f"- {path}" for path in existing)
        raise ResearchDatasetExportError(
            "Research dataset output(s) already exist. Use --overwrite to replace intentionally:\n"
            f"{formatted}"
        )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _feature_names() -> list[str]:
    return runtime._schema_feature_names()  # noqa: SLF001


def _feature_export_frame(features: pd.DataFrame, feature_names: list[str]) -> pd.DataFrame:
    base_cols = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close", "edge_id", "side"]
    cols = [col for col in base_cols if col in features.columns] + feature_names
    missing = [col for col in feature_names if col not in features.columns]
    if missing:
        raise ResearchDatasetExportError(f"Runtime feature matrix missing schema features: {missing}")
    return features[cols].copy()


def _validate_round_break_fields(df: pd.DataFrame, *, name: str) -> None:
    for col in ("Round_Break_Type", "Round_Level"):
        if col not in df.columns:
            raise ResearchDatasetExportError(f"{name} must contain {col}.")
    brk = pd.to_numeric(df["Round_Break_Type"], errors="coerce")
    if not (brk == 1).all():
        raise ResearchDatasetExportError(f"{name} contain non-up-break Round_Break_Type values.")


def _validate_export_contract(
    *,
    feature_names: list[str],
    feature_export: pd.DataFrame,
    events: pd.DataFrame,
    model_rows: pd.DataFrame,
    model_row_manifest: dict[str, Any],
) -> None:
    if not feature_names:
        raise ResearchDatasetExportError("Feature schema produced zero feature names.")
    if feature_export.empty:
        raise ResearchDatasetExportError("Feature export is empty.")
    if events.empty:
        raise ResearchDatasetExportError("Candidate event export is empty.")
    if model_rows.empty:
        raise ResearchDatasetExportError("Model-row export is empty.")

    if "edge_id" in events.columns and not (events["edge_id"] == EDGE_ID).all():
        bad = events.loc[events["edge_id"] != EDGE_ID, "edge_id"].unique().tolist()
        raise ResearchDatasetExportError(f"Candidate events contain wrong edge_id values: {bad}")
    if "side" in events.columns and not (pd.to_numeric(events["side"], errors="coerce") == SIDE_VALUE).all():
        bad = events.loc[pd.to_numeric(events["side"], errors="coerce") != SIDE_VALUE, "side"].unique().tolist()
        raise ResearchDatasetExportError(f"Candidate events contain non-long side values: {bad}")
    _validate_round_break_fields(events, name="Candidate events")

    if not (pd.to_numeric(model_rows["side"], errors="coerce") == SIDE_VALUE).all():
        bad = model_rows.loc[pd.to_numeric(model_rows["side"], errors="coerce") != SIDE_VALUE, "side"].unique().tolist()
        raise ResearchDatasetExportError(f"Model rows contain non-long side values: {bad}")
    _validate_round_break_fields(model_rows, name="Model rows")

    if not (model_rows["pt_price"] > model_rows["entry_price"]).all():
        raise ResearchDatasetExportError("Long model rows must have pt_price above entry_price.")
    if not (model_rows["sl_price"] < model_rows["entry_price"]).all():
        raise ResearchDatasetExportError("Long model rows must have sl_price below entry_price.")

    if model_row_manifest.get("edge_id") != EDGE_ID:
        raise ResearchDatasetExportError(f"Model row manifest edge_id mismatch: {model_row_manifest.get('edge_id')!r}")
    if model_row_manifest.get("side_value") != SIDE_VALUE:
        raise ResearchDatasetExportError("Model row manifest side_value must be +1.")
    if model_row_manifest.get("primary_side_rule") != PRIMARY_SIDE_RULE:
        raise ResearchDatasetExportError("Model row manifest primary_side_rule mismatch.")
    if model_row_manifest.get("row_builder") != ROW_BUILDER:
        raise ResearchDatasetExportError("Model row manifest row_builder is not the roundlevelcascade_long builder.")
    if model_row_manifest.get("long_barrier_contract") != "pt_price_above_entry_sl_price_below_entry":
        raise ResearchDatasetExportError("Model row manifest long barrier contract mismatch.")

    forbidden_event_cols = {
        "meta_label", "t1", "barrier_hit", "resolution_source", "entry_price",
        "pt_price", "sl_price", "realized_r", "raw_probability",
        "calibrated_probability", "afml_bet_size", "approved_signal",
    }
    forbidden_present = sorted(forbidden_event_cols.intersection(events.columns))
    if forbidden_present:
        raise ResearchDatasetExportError(f"Candidate events contain forbidden research/live-decision columns: {forbidden_present}")


def export_research_dataset(*, overwrite: bool = False, dry_run: bool = False) -> dict[str, Any]:
    started = time.time()
    paths = _output_paths()
    output_files = [
        paths["feature_matrix"], paths["candidate_events"], paths["model_rows"],
        paths["model_row_manifest"], paths["export_manifest"],
    ]
    if dry_run:
        return {
            "status": "DRY_RUN_PASS",
            "edge_id": EDGE_ID,
            "planned_outputs": {key: _relative_to_v2(value) for key, value in paths.items() if key != "root"},
        }

    _check_safe_to_write(output_files, overwrite=overwrite)
    paths["root"].mkdir(parents=True, exist_ok=True)

    feature_names = _feature_names()
    features = runtime.build_features(drop_warmup_rows=False)
    events = runtime.generate_candidate_events(features)
    model_rows, model_row_manifest = runtime.build_model_rows(features=features, events=events)
    model_rows, model_row_manifest = enforce_gross_economic_model_row_contract(model_rows, model_row_manifest, edge_id=EDGE_ID)
    feature_export = _feature_export_frame(features, feature_names)

    _validate_export_contract(
        feature_names=feature_names,
        feature_export=feature_export,
        events=events,
        model_rows=model_rows,
        model_row_manifest=model_row_manifest,
    )

    feature_export.to_csv(paths["feature_matrix"], index=False)
    events.to_csv(paths["candidate_events"], index=False)
    model_rows.to_csv(paths["model_rows"], index=False)
    _write_json(paths["model_row_manifest"], model_row_manifest)

    artifact_hashes = hash_artifacts(output_files[:-1], root=_V2_ROOT)
    export_manifest = {
        "edge_id": EDGE_ID,
        "export_name": EXPORT_NAME,
        "status": "research_dataset_exported_not_deployable",
        "runtime_contract_status": runtime.load_contract()["runtime_status"],
        "feature_count": int(len(feature_names)),
        "feature_names": feature_names,
        "rows_feature_matrix": int(len(feature_export)),
        "rows_candidate_events": int(len(events)),
        "rows_model_rows": int(len(model_rows)),
        "long_contract_checks": {
            "candidate_events_side": SIDE_VALUE,
            "candidate_events_round_break_type_up_only": True,
            "model_rows_side": SIDE_VALUE,
            "model_rows_round_break_type_up_only": True,
            "pt_price_above_entry_price": True,
            "sl_price_below_entry_price": True,
        },
        "outputs": {
            "feature_matrix": _relative_to_v2(paths["feature_matrix"]),
            "candidate_events": _relative_to_v2(paths["candidate_events"]),
            "model_rows": _relative_to_v2(paths["model_rows"]),
            "model_row_manifest": _relative_to_v2(paths["model_row_manifest"]),
            "export_manifest": _relative_to_v2(paths["export_manifest"]),
        },
        "artifact_hashes": artifact_hashes,
        "blueprint_boundaries": [
            "Features were generated by roundlevelcascade_long/runtime.py::build_features.",
            "Candidate events were generated by roundlevelcascade_long/runtime.py::generate_candidate_events.",
            "Labels/model rows were generated by roundlevelcascade_long/runtime.py::build_model_rows and are research-only.",
            "Live score() remains fail-closed until model/calibrator/threshold/deployability audit pass.",
            "Candidate events are long-only round-level up-breaks (Round_Break_Type == +1) and contain no labels/probabilities/approved signals.",
        ],
        "elapsed_seconds": round(time.time() - started, 2),
    }
    _write_json(paths["export_manifest"], export_manifest)

    return {
        "status": "PASS",
        "edge_id": EDGE_ID,
        "feature_count": len(feature_names),
        "rows_feature_matrix": len(feature_export),
        "rows_candidate_events": len(events),
        "rows_model_rows": len(model_rows),
        "feature_matrix": _relative_to_v2(paths["feature_matrix"]),
        "candidate_events": _relative_to_v2(paths["candidate_events"]),
        "model_rows": _relative_to_v2(paths["model_rows"]),
        "export_manifest": _relative_to_v2(paths["export_manifest"]),
        "elapsed_seconds": round(time.time() - started, 2),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export V2 research dataset for XAUUSD/roundlevelcascade_long.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _run(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    result = export_research_dataset(overwrite=args.overwrite, dry_run=args.dry_run)
    if result["status"] == "DRY_RUN_PASS":
        print("✅ XAUUSD/roundlevelcascade_long research dataset export dry-run passed.")
        print(f"Edge: {result['edge_id']}")
        for name, path in result["planned_outputs"].items():
            print(f"{name}: {path}")
        return

    print("✅ XAUUSD/roundlevelcascade_long research dataset exported.")
    print(f"Edge: {result['edge_id']}")
    print(f"Feature count: {result['feature_count']}")
    print(f"Feature matrix rows: {result['rows_feature_matrix']}")
    print(f"Candidate event rows: {result['rows_candidate_events']}")
    print(f"Model rows: {result['rows_model_rows']}")
    print(f"Feature matrix: {result['feature_matrix']}")
    print(f"Candidate events: {result['candidate_events']}")
    print(f"Model rows: {result['model_rows']}")
    print(f"Manifest: {result['export_manifest']}")
    print(f"Elapsed seconds: {result['elapsed_seconds']}")


if __name__ == "__main__":
    try:
        _run()
    except ResearchDatasetExportError as exc:
        raise SystemExit(f"❌ {exc}") from exc
