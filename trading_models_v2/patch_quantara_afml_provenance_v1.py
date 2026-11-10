#!/usr/bin/env python3
r"""
Quantara V3 — AFML provenance + CPCV F1 baseline patcher

Run from the trading_models_v2 root:

    python .\patch_quantara_afml_provenance_v1.py --dry-run
    python .\patch_quantara_afml_provenance_v1.py

Optional guarded manifest backfill for the BTC AFML-era runs already completed:

    python .\patch_quantara_afml_provenance_v1.py --dry-run --backfill-known-afml-runs
    python .\patch_quantara_afml_provenance_v1.py --backfill-known-afml-runs

Scope
-----
- Does NOT change AFML weighting math.
- Does NOT change Step 02 F1 model-selection objective.
- Does NOT rename/move any existing research output artifact.
- Fixes stale Step 01 MDA provenance wording.
- Binds Step 01/02/03 manifests to the exact standard_quote_tick CSV SHA-256
  and deterministic AFML event-weight-table SHA-256.
- Makes Step 02 validate Step 01 AFML/bar provenance before Optuna starts.
- Makes Step 03 validate Step 02 AFML/bar provenance before OOF generation.
- Adds always-positive CPCV F1 as a diagnostic only (never an Optuna objective).

Designed against Quantara v3 source after commit:
    b88e0f5afd9b233395b66e3cfbb063523c42edd3
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

PATCH_MARKER = "# QUANTARA_AFML_PROVENANCE_V1"
EXPECTED_AFML_MARKER = "# QUANTARA_AFML_CH4_SAMPLE_WEIGHT_V1"
AFML_CONTRACT = "quantara_afml_ch4_bar_clock_return_attribution_v1"
BACKFILL_CUTOFF = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
KNOWN_AFML_EDGES = (
    "BTCUSD/absorption_long",
    "BTCUSD/absorption_short",
    "BTCUSD/breakdownretestfailure_short",
    "BTCUSD/downsidejumpcontinuation_short",
)

STEP01_NAME = "01_feature_surgery_clustered_mda.py"
STEP02_NAME = "02_train_meta_model_optuna.py"
STEP03_NAME = "03_generate_oof_predictions.py"


class PatchError(RuntimeError):
    pass


def now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def save_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    n = text.count(old)
    if n != 1:
        raise PatchError(f"{label}: expected exactly one anchor, found {n}")
    return text.replace(old, new, 1)


def insert_after_once(text: str, anchor: str, insertion: str, *, label: str) -> str:
    n = text.count(anchor)
    if n != 1:
        raise PatchError(f"{label}: expected exactly one anchor, found {n}")
    return text.replace(anchor, anchor + insertion, 1)


def compile_text(text: str, path: Path) -> None:
    try:
        ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        raise PatchError(f"Patched source would not parse: {path}: {exc}") from exc


def output_contract_fingerprint(text: str) -> tuple[str, ...]:
    rows = []
    for raw in text.splitlines():
        s = raw.strip()
        if (
            ("OUTPUT" in s or "MANIFEST_OUTPUT" in s)
            and "=" in s
            and ("OUTPUT_DIR" in s or " / " in s)
            and not s.startswith("#")
        ):
            rows.append(s)
    return tuple(rows)


COMMON_HELPERS_FEATURE = r'''

def _afml_weight_table_sha256(table: pd.DataFrame) -> str:
    """Stable digest of event timestamps + canonical AFML financial fields.

    Numerical values are rounded only for the provenance digest so harmless
    last-bit platform differences do not break lineage. Training values are
    never rounded or altered.
    """
    required = [AFML_AVERAGE_UNIQUENESS_COL, AFML_RETURN_ATTRIBUTION_WEIGHT_COL]
    missing = [c for c in required if c not in table.columns]
    if missing:
        raise FeatureSurgeryError(f"Cannot hash AFML table; missing columns: {missing}")
    work = table.sort_index()
    h = hashlib.sha256()
    for ts, u, w in zip(
        pd.DatetimeIndex(work.index),
        work[AFML_AVERAGE_UNIQUENESS_COL].to_numpy(dtype="float64"),
        work[AFML_RETURN_ATTRIBUTION_WEIGHT_COL].to_numpy(dtype="float64"),
    ):
        h.update(f"{int(pd.Timestamp(ts).value)}|{float(u):.12f}|{float(w):.12f}\n".encode("utf-8"))
    return h.hexdigest()


def _afml_provenance_snapshot() -> dict[str, Any]:
    bars_path = _resolve_standard_quote_tick_bars_path()
    table = _get_afml_weight_table()
    return {
        "contract_version": AFML_SAMPLE_WEIGHT_CONTRACT_VERSION,
        "standard_quote_tick_bars": display_path(bars_path),
        "standard_quote_tick_bars_sha256": _phase3a1_sha256_file(bars_path),
        "event_weight_table_sha256": _afml_weight_table_sha256(table),
        "event_count": int(len(table)),
        "average_uniqueness": {
            "mean": float(table[AFML_AVERAGE_UNIQUENESS_COL].mean()),
            "min": float(table[AFML_AVERAGE_UNIQUENESS_COL].min()),
            "median": float(table[AFML_AVERAGE_UNIQUENESS_COL].median()),
            "max": float(table[AFML_AVERAGE_UNIQUENESS_COL].max()),
        },
        "return_attribution_weight": {
            "mean": float(table[AFML_RETURN_ATTRIBUTION_WEIGHT_COL].mean()),
            "min": float(table[AFML_RETURN_ATTRIBUTION_WEIGHT_COL].min()),
            "median": float(table[AFML_RETURN_ATTRIBUTION_WEIGHT_COL].median()),
            "max": float(table[AFML_RETURN_ATTRIBUTION_WEIGHT_COL].max()),
        },
        "weighting_note": (
            "AFML financial observation weight is normalized absolute "
            "concurrency-adjusted log-return attribution on the "
            "standard_quote_tick bar clock; class balancing is separate."
        ),
    }
'''

COMMON_HELPERS_TRAIN = COMMON_HELPERS_FEATURE.replace("FeatureSurgeryError", "MetaModelTrainingError").replace(
    "_phase3a1_sha256_file", "_phase3a_sha256_file"
)
COMMON_HELPERS_OOF = COMMON_HELPERS_FEATURE.replace("FeatureSurgeryError", "OOFGenerationError").replace(
    "_phase3a1_sha256_file", "_phase3a_sha256_file"
)


def patch_step01(text: str, path: Path) -> str:
    if PATCH_MARKER in text:
        return text
    if EXPECTED_AFML_MARKER not in text:
        raise PatchError(f"{path}: missing expected AFML Ch.4 marker")
    original_fp = output_contract_fingerprint(text)
    text = insert_after_once(text, EXPECTED_AFML_MARKER, "\n\n" + PATCH_MARKER, label=f"{path}: marker")
    anchor = 'def _phase3a1_validate_gross_target(df: pd.DataFrame) -> None:\n'
    text = replace_once(text, anchor, COMMON_HELPERS_FEATURE + "\n\n" + anchor, label=f"{path}: helper insertion")
    text = replace_once(
        text,
        '"mda_formula": "base_f1 - shuffled_f1",',
        '"mda_formula": "weighted_oos_binary_log_loss_after_permutation - weighted_oos_binary_log_loss_base",\n'
        '            "mda_scoring_weight": "canonical_afml_return_attribution_weight",\n'
        '            "mda_interpretation": "positive means permutation worsened AFML-weighted OOS probability loss",',
        label=f"{path}: stale MDA manifest wording",
    )
    text = replace_once(
        text,
        '    manifest = {\n        "created_at": now_utc(),',
        '    afml_provenance = _afml_provenance_snapshot()\n\n    manifest = {\n        "created_at": now_utc(),',
        label=f"{path}: Step01 provenance snapshot",
    )
    text = replace_once(
        text,
        '        "input_feature_schema": display_path(FEATURE_SCHEMA_PATH),\n        "rows_used": int(rows_used),',
        '        "input_feature_schema": display_path(FEATURE_SCHEMA_PATH),\n        "afml_provenance": afml_provenance,\n        "rows_used": int(rows_used),',
        label=f"{path}: Step01 manifest provenance field",
    )
    text = replace_once(
        text,
        '        "selected_features_sha256": _phase3a1_sha256_file(OUTPUT_SELECTED_FEATURES_JSON),\n        "phase3a_step01_provenance_status": "PASS",',
        '        "selected_features_sha256": _phase3a1_sha256_file(OUTPUT_SELECTED_FEATURES_JSON),\n'
        '        "afml_sample_weight_contract_version": afml_provenance["contract_version"],\n'
        '        "standard_quote_tick_bars_sha256": afml_provenance["standard_quote_tick_bars_sha256"],\n'
        '        "afml_event_weight_table_sha256": afml_provenance["event_weight_table_sha256"],\n'
        '        "phase3a_step01_provenance_status": "PASS",',
        label=f"{path}: Step01 top-level AFML lineage",
    )
    compile_text(text, path)
    if output_contract_fingerprint(text) != original_fp:
        raise PatchError(f"{path}: output-path assignment contract changed unexpectedly")
    return text


def patch_step02(text: str, path: Path) -> str:
    if PATCH_MARKER in text:
        return text
    if EXPECTED_AFML_MARKER not in text:
        raise PatchError(f"{path}: missing expected AFML Ch.4 marker")
    original_fp = output_contract_fingerprint(text)
    text = insert_after_once(text, EXPECTED_AFML_MARKER, "\n\n" + PATCH_MARKER, label=f"{path}: marker")
    anchor = 'def _phase3a_validate_gross_target(df: pd.DataFrame) -> None:\n'
    text = replace_once(text, anchor, COMMON_HELPERS_TRAIN + "\n\n" + anchor, label=f"{path}: helper insertion")

    old = '''    for key, artifact in checks.items():
        declared = str(payload.get(key, ""))
        actual = _phase3a_sha256_file(artifact)
        if not declared or declared != actual:
            raise MetaModelTrainingError(
                f"Step-01 provenance mismatch for {key}; rerun Step 01 before Step 02."
            )
    return payload
'''
    new = '''    for key, artifact in checks.items():
        declared = str(payload.get(key, ""))
        actual = _phase3a_sha256_file(artifact)
        if not declared or declared != actual:
            raise MetaModelTrainingError(
                f"Step-01 provenance mismatch for {key}; rerun Step 01 before Step 02."
            )

    afml = payload.get("afml_provenance")
    if not isinstance(afml, dict):
        raise MetaModelTrainingError(
            "Step-01 manifest has no AFML provenance block. Rerun patched Step 01 "
            "or use the guarded manifest backfill before Step 02."
        )
    if afml.get("contract_version") != AFML_SAMPLE_WEIGHT_CONTRACT_VERSION:
        raise MetaModelTrainingError(
            "Step-01 AFML sample-weight contract mismatch; rerun Step 01 before Step 02."
        )

    current_afml = _afml_provenance_snapshot()
    if str(afml.get("standard_quote_tick_bars_sha256", "")) != current_afml["standard_quote_tick_bars_sha256"]:
        raise MetaModelTrainingError(
            "Step-01 AFML provenance mismatch for standard_quote_tick_bars_sha256. "
            "The bar CSV in this run is not the exact bar file used by Step 01."
        )
    if str(afml.get("event_weight_table_sha256", "")) != current_afml["event_weight_table_sha256"]:
        raise MetaModelTrainingError(
            "Step-01 AFML provenance mismatch for event_weight_table_sha256. "
            "The model rows/bar clock no longer reproduce Step 01 AFML weights."
        )
    return payload
'''
    text = replace_once(text, old, new, label=f"{path}: Step01 AFML provenance validation")

    text = replace_once(
        text,
        '        metrics = score_at_threshold(y_test, test_proba, best_thr)\n        f1_scores.append(metrics["f1"])\n        row = {',
        '        metrics = score_at_threshold(y_test, test_proba, best_thr)\n'
        '        always_positive_f1 = float(\n'
        '            f1_score(\n'
        '                np.asarray(y_test).astype(int),\n'
        '                np.ones(len(y_test), dtype=int),\n'
        '                zero_division=0,\n'
        '            )\n'
        '        )\n'
        '        f1_scores.append(metrics["f1"])\n        row = {',
        label=f"{path}: baseline calculation",
    )
    text = replace_once(
        text,
        '            "test_recall": metrics["recall"],\n        }',
        '            "test_recall": metrics["recall"],\n'
        '            "always_positive_test_f1": always_positive_f1,\n'
        '            "model_minus_always_positive_f1": float(metrics["f1"] - always_positive_f1),\n'
        '        }',
        label=f"{path}: baseline fold fields",
    )
    text = replace_once(
        text,
        '    final_train_diag = evaluate_final_training_threshold(final_model, x_data, y)\n\n    manifest = {',
        '    final_train_diag = evaluate_final_training_threshold(final_model, x_data, y)\n'
        '    afml_provenance = _afml_provenance_snapshot()\n'
        '    baseline_values = [float(r["always_positive_test_f1"]) for r in best_fold_rows if "always_positive_test_f1" in r]\n'
        '    model_minus_baseline_values = [float(r["model_minus_always_positive_f1"]) for r in best_fold_rows if "model_minus_always_positive_f1" in r]\n\n'
        '    manifest = {',
        label=f"{path}: Step02 provenance/baseline summary",
    )
    text = replace_once(
        text,
        '        "input_feature_schema": display_path(FEATURE_SCHEMA_PATH),\n        "rows_used": int(len(df)),',
        '        "input_feature_schema": display_path(FEATURE_SCHEMA_PATH),\n        "afml_provenance": afml_provenance,\n        "rows_used": int(len(df)),',
        label=f"{path}: Step02 AFML manifest block",
    )
    text = replace_once(
        text,
        '            "note": "These F1 thresholds are model-selection diagnostics only. Final live threshold must use calibrated_probability later.",\n        },',
        '            "note": "These F1 thresholds are model-selection diagnostics only. Final live threshold must use calibrated_probability later.",\n'
        '            "always_positive_cpcv_f1_baseline": {\n'
        '                "diagnostic_only": True,\n'
        '                "mean_test_f1": float(np.mean(baseline_values)) if baseline_values else None,\n'
        '                "mean_model_minus_baseline_f1": float(np.mean(model_minus_baseline_values)) if model_minus_baseline_values else None,\n'
        '                "note": "Baseline is never used by Optuna or threshold selection.",\n'
        '            },\n'
        '        },',
        label=f"{path}: Step02 baseline manifest",
    )
    text = replace_once(
        text,
        '        "best_params_sha256": _phase3a_sha256_file(BEST_PARAMS_OUTPUT),\n        "phase3a_provenance_status": "PASS",',
        '        "best_params_sha256": _phase3a_sha256_file(BEST_PARAMS_OUTPUT),\n'
        '        "afml_sample_weight_contract_version": afml_provenance["contract_version"],\n'
        '        "standard_quote_tick_bars_sha256": afml_provenance["standard_quote_tick_bars_sha256"],\n'
        '        "afml_event_weight_table_sha256": afml_provenance["event_weight_table_sha256"],\n'
        '        "phase3a_provenance_status": "PASS",',
        label=f"{path}: Step02 top-level AFML lineage",
    )
    compile_text(text, path)
    if output_contract_fingerprint(text) != original_fp:
        raise PatchError(f"{path}: output-path assignment contract changed unexpectedly")
    return text


def patch_step03(text: str, path: Path) -> str:
    if PATCH_MARKER in text:
        return text
    if EXPECTED_AFML_MARKER not in text:
        raise PatchError(f"{path}: missing expected AFML Ch.4 marker")
    original_fp = output_contract_fingerprint(text)
    text = insert_after_once(text, EXPECTED_AFML_MARKER, "\n\n" + PATCH_MARKER, label=f"{path}: marker")
    anchor = 'def _phase3a_validate_gross_target(df: pd.DataFrame) -> None:\n'
    text = replace_once(text, anchor, COMMON_HELPERS_OOF + "\n\n" + anchor, label=f"{path}: helper insertion")

    old = '''    for key, path in checks.items():
        declared = str(payload.get(key, ""))
        actual = _phase3a_sha256_file(path)
        if not declared or declared != actual:
            raise OOFGenerationError(
                f"Step-02 provenance mismatch for {key}; rerun Step 02 before Step 03."
            )
    return payload
'''
    new = '''    for key, path in checks.items():
        declared = str(payload.get(key, ""))
        actual = _phase3a_sha256_file(path)
        if not declared or declared != actual:
            raise OOFGenerationError(
                f"Step-02 provenance mismatch for {key}; rerun Step 02 before Step 03."
            )

    afml = payload.get("afml_provenance")
    if not isinstance(afml, dict):
        raise OOFGenerationError(
            "Step-02 manifest has no AFML provenance block. Rerun patched Step 02 before Step 03."
        )
    if afml.get("contract_version") != AFML_SAMPLE_WEIGHT_CONTRACT_VERSION:
        raise OOFGenerationError("Step-02 AFML sample-weight contract is stale.")

    current_afml = _afml_provenance_snapshot()
    if str(afml.get("standard_quote_tick_bars_sha256", "")) != current_afml["standard_quote_tick_bars_sha256"]:
        raise OOFGenerationError("Step-02 AFML provenance mismatch for standard_quote_tick_bars_sha256.")
    if str(afml.get("event_weight_table_sha256", "")) != current_afml["event_weight_table_sha256"]:
        raise OOFGenerationError("Step-02 AFML provenance mismatch for event_weight_table_sha256.")
    return payload
'''
    text = replace_once(text, old, new, label=f"{path}: Step02 AFML provenance validation")
    text = replace_once(
        text,
        '    manifest = {\n        "created_at": now_utc(),',
        '    afml_provenance = _afml_provenance_snapshot()\n\n    manifest = {\n        "created_at": now_utc(),',
        label=f"{path}: Step03 provenance snapshot",
    )
    text = replace_once(
        text,
        '        "input_feature_schema": display_path(FEATURE_SCHEMA_PATH),\n        "selected_feature_count": int(len(selected_features)),',
        '        "input_feature_schema": display_path(FEATURE_SCHEMA_PATH),\n        "afml_provenance": afml_provenance,\n        "selected_feature_count": int(len(selected_features)),',
        label=f"{path}: Step03 AFML manifest block",
    )
    text = replace_once(
        text,
        '        "oof_predictions_sha256": _phase3a_sha256_file(OOF_PREDICTIONS_OUTPUT),\n        "phase3a_provenance_status": "PASS",',
        '        "oof_predictions_sha256": _phase3a_sha256_file(OOF_PREDICTIONS_OUTPUT),\n'
        '        "afml_sample_weight_contract_version": afml_provenance["contract_version"],\n'
        '        "standard_quote_tick_bars_sha256": afml_provenance["standard_quote_tick_bars_sha256"],\n'
        '        "afml_event_weight_table_sha256": afml_provenance["event_weight_table_sha256"],\n'
        '        "phase3a_provenance_status": "PASS",',
        label=f"{path}: Step03 top-level AFML lineage",
    )
    compile_text(text, path)
    if output_contract_fingerprint(text) != original_fp:
        raise PatchError(f"{path}: output-path assignment contract changed unexpectedly")
    return text


def discover(root: Path, name: str) -> list[Path]:
    return sorted((root / "assets").glob(f"*/edges/*/research/{name}"))


def require_root(root: Path) -> None:
    required = [root / "assets", root / "shared", root / "portfolio_allocator"]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise PatchError("Run this patcher from the trading_models_v2 root. Missing expected paths: " + repr(missing))


def parse_created_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def stable_table_hash(table) -> str:
    import pandas as pd
    h = hashlib.sha256()
    for ts, u, w in zip(
        pd.DatetimeIndex(table.index),
        table["average_uniqueness"].to_numpy(dtype="float64"),
        table["return_attribution_weight"].to_numpy(dtype="float64"),
    ):
        h.update(f"{int(pd.Timestamp(ts).value)}|{float(u):.12f}|{float(w):.12f}\n".encode("utf-8"))
    return h.hexdigest()


def compute_backfill_provenance(root: Path, asset: str, model_rows: Path) -> dict:
    sys.path.insert(0, str(root))
    try:
        from shared.models.afml_sample_weights import AFML_SAMPLE_WEIGHT_CONTRACT_VERSION, build_afml_event_weight_table
    finally:
        if sys.path and sys.path[0] == str(root):
            sys.path.pop(0)
    if AFML_SAMPLE_WEIGHT_CONTRACT_VERSION != AFML_CONTRACT:
        raise PatchError(f"Shared AFML contract mismatch: {AFML_SAMPLE_WEIGHT_CONTRACT_VERSION!r}")
    bars = root / "assets" / asset / "bars" / "standard_quote_tick" / f"{asset.lower()}_standard_quote_tick_bars.csv"
    if not bars.exists() or not model_rows.exists():
        raise PatchError(f"Cannot backfill {asset}: required bars/model rows missing")
    table = build_afml_event_weight_table(model_rows, bars)
    return {
        "contract_version": AFML_CONTRACT,
        "standard_quote_tick_bars": str(bars.relative_to(root)).replace("\\", "/"),
        "standard_quote_tick_bars_sha256": sha256_file(bars),
        "event_weight_table_sha256": stable_table_hash(table),
        "event_count": int(len(table)),
        "average_uniqueness": {
            "mean": float(table["average_uniqueness"].mean()),
            "min": float(table["average_uniqueness"].min()),
            "median": float(table["average_uniqueness"].median()),
            "max": float(table["average_uniqueness"].max()),
        },
        "return_attribution_weight": {
            "mean": float(table["return_attribution_weight"].mean()),
            "min": float(table["return_attribution_weight"].min()),
            "median": float(table["return_attribution_weight"].median()),
            "max": float(table["return_attribution_weight"].max()),
        },
        "weighting_note": "AFML financial observation weight is normalized absolute concurrency-adjusted log-return attribution on the standard_quote_tick bar clock; class balancing is separate.",
        "provenance_backfill": {
            "reconstructed": True,
            "note": "Added after the original AFML-era run from immutable model rows and the exact current standard_quote_tick bar file. Historical script SHA is not invented.",
        },
    }


def backfill_known_runs(root: Path, dry_run: bool, backup_dir: Path | None) -> list[Path]:
    changed = []
    for edge_id in KNOWN_AFML_EDGES:
        asset, edge_name = edge_id.split("/", 1)
        edge = root / "assets" / asset / "edges" / edge_name
        if not edge.exists():
            print(f"Backfill skip: {edge_id} | edge directory missing")
            continue
        stem = f"{asset.lower()}_{edge_name}"
        model_rows = edge / "artifacts" / "research_dataset" / f"{stem}_model_rows.csv"
        step1 = edge / "artifacts" / "feature_surgery" / "feature_surgery_manifest.json"
        step2 = edge / "artifacts" / "model" / "model_training_manifest.json"
        step1_ok = False
        step2_ok = False
        prov = None

        if step1.exists():
            p1 = json.loads(step1.read_text(encoding="utf-8"))
            created = parse_created_at(p1.get("created_at"))
            if p1.get("edge_id") == edge_id and created is not None and created >= BACKFILL_CUTOFF:
                prov = compute_backfill_provenance(root, asset, model_rows)
                p1["afml_provenance"] = prov
                p1["afml_sample_weight_contract_version"] = prov["contract_version"]
                p1["standard_quote_tick_bars_sha256"] = prov["standard_quote_tick_bars_sha256"]
                p1["afml_event_weight_table_sha256"] = prov["event_weight_table_sha256"]
                if isinstance(p1.get("scoring"), dict):
                    p1["scoring"]["mda_formula"] = "weighted_oos_binary_log_loss_after_permutation - weighted_oos_binary_log_loss_base"
                    p1["scoring"]["mda_scoring_weight"] = "canonical_afml_return_attribution_weight"
                    p1["scoring"]["mda_interpretation"] = "positive means permutation worsened AFML-weighted OOS probability loss"
                step1_ok = True
                if not dry_run:
                    dest = backup_dir / step1.relative_to(root)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(step1, dest)
                    save_json(step1, p1)
                    changed.append(step1)

        if step2.exists() and step1_ok:
            p2 = json.loads(step2.read_text(encoding="utf-8"))
            created = parse_created_at(p2.get("created_at"))
            if p2.get("edge_id") == edge_id and created is not None and created >= BACKFILL_CUTOFF:
                if prov is None:
                    prov = compute_backfill_provenance(root, asset, model_rows)
                p2["afml_provenance"] = prov
                p2["afml_sample_weight_contract_version"] = prov["contract_version"]
                p2["standard_quote_tick_bars_sha256"] = prov["standard_quote_tick_bars_sha256"]
                p2["afml_event_weight_table_sha256"] = prov["event_weight_table_sha256"]
                step2_ok = True
                if not dry_run:
                    p2["step01_feature_surgery_manifest_sha256"] = sha256_file(step1)
                    dest = backup_dir / step2.relative_to(root)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(step2, dest)
                    save_json(step2, p2)
                    changed.append(step2)

        print(f"Backfill eligible: {edge_id} | Step01={'yes' if step1_ok else 'no'} | Step02={'yes' if step2_ok else 'no'}")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backfill-known-afml-runs", action="store_true")
    args = parser.parse_args()

    root = Path.cwd().resolve()
    require_root(root)
    step01 = discover(root, STEP01_NAME)
    step02 = discover(root, STEP02_NAME)
    step03 = discover(root, STEP03_NAME)
    if not step01 or not step02 or not step03:
        raise PatchError("Could not discover the Step 01/02/03 research scripts.")
    if not (len(step01) == len(step02) == len(step03)):
        raise PatchError(f"Research script inventory mismatch: Step01={len(step01)} Step02={len(step02)} Step03={len(step03)}")

    transforms = []
    skipped = 0
    for path, fn in ([*( (p, patch_step01) for p in step01), *( (p, patch_step02) for p in step02), *( (p, patch_step03) for p in step03)]):
        old = path.read_text(encoding="utf-8")
        new = fn(old, path)
        if new == old:
            skipped += 1
        else:
            transforms.append((path, new))

    print("=" * 88)
    print("QUANTARA V3 — AFML PROVENANCE + CPCV BASELINE PATCH")
    print("=" * 88)
    print(f"Root                   : {root}")
    print(f"Step 01 copies         : {len(step01)}")
    print(f"Step 02 copies         : {len(step02)}")
    print(f"Step 03 copies         : {len(step03)}")
    print(f"Planned source writes  : {len(transforms)}")
    print(f"Already current/skipped: {skipped}")
    print(f"Dry run                : {args.dry_run}")
    print(f"Backfill known runs    : {args.backfill_known_afml_runs}")

    if args.dry_run:
        if args.backfill_known_afml_runs:
            backfill_known_runs(root, True, None)
        print("\nDRY RUN PASS")
        print("No files were changed.")
        return 0

    backup_dir = root / "_patch_backups" / f"afml_provenance_{now_tag()}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    written = []
    try:
        for path, new in transforms:
            dest = backup_dir / path.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            path.write_text(new, encoding="utf-8")
            written.append(path)
        backfilled = []
        if args.backfill_known_afml_runs:
            backfilled = backfill_known_runs(root, False, backup_dir)
        for path in written:
            compile_text(path.read_text(encoding="utf-8"), path)
    except Exception:
        for backup in sorted(backup_dir.rglob("*")):
            if backup.is_file():
                target = root / backup.relative_to(backup_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
        raise

    print("\nPATCH PASS")
    print(f"Backup                 : {backup_dir}")
    print(f"Source files wrote     : {len(written)}")
    if args.backfill_known_afml_runs:
        print(f"Manifests backfilled   : {len(backfilled)}")
    print("Artifact/output filenames and output-path assignments were preserved.")
    print("AFML weighting math was not changed.")
    print("Step 02 Optuna/CPCV model selection remains F1-based.")
    print("Step 02 now rejects wrong bar/AFML provenance before Optuna starts.")
    print("Always-positive CPCV F1 is diagnostic only.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchError as exc:
        raise SystemExit(f"PATCH ABORTED: {exc}") from exc
