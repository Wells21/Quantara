# QUANTARA_V3_PHASE3C_SEMANTIC_GENERATION_CLOSURE_V1
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from shared.validation.v3_semantic_contract import (
    ELIGIBILITY_COLUMN,
    EXECUTED_TARGET_CONTRACT_VERSION,
    EXECUTION_CONTRACT_VERSION,
    GROSS_LABEL_CONTRACT_VERSION,
    GROSS_TARGET_COLUMN,
    GROSS_TARGET_DEFINITION,
    PAYOFF_CONTEXT_VERSION,
    PAYOFF_PROFILE_VERSION,
    TARGET_COLUMN,
    THRESHOLD_PROBABILITY_COLUMN,
    build_payoff_context,
    profile_from_mapping,
    sha256_file,
    validate_representative_payoff_context_handoff,
    validate_research_generation_chain,
    validate_semantic_generation_snapshot,
    validate_step03_generation,
)


def _profile() -> dict:
    avg_win, avg_loss, zero_rate = 0.71, -0.48, 0.02
    return {
        "payoff_profile_version": PAYOFF_PROFILE_VERSION,
        "payoff_profile_valid": True,
        "payoff_profile_invalid_reason": "",
        "payoff_trade_count": 100,
        "payoff_win_count": 56,
        "payoff_loss_count": 42,
        "payoff_zero_count": 2,
        "payoff_zero_rate": zero_rate,
        "min_wins_required": 5,
        "min_losses_required": 5,
        "empirical_avg_win_r": avg_win,
        "empirical_avg_loss_r": avg_loss,
        "empirical_reward_risk_ratio": avg_win / abs(avg_loss),
        "empirical_break_even_probability": (1.0 - zero_rate) * abs(avg_loss) / (avg_win + abs(avg_loss)),
    }


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_semantic_generation_snapshot_rejects_stale_identity() -> None:
    current = {"step03_oof_predictions_sha256": "new", "payoff_context_id": "ctx"}
    validate_semantic_generation_snapshot(dict(current), current, context="unit")
    with pytest.raises(Exception):
        validate_semantic_generation_snapshot(
            {"step03_oof_predictions_sha256": "old", "payoff_context_id": "ctx"},
            current, context="unit-stale",
        )


def test_payoff_profile_arithmetic_is_canonical() -> None:
    profile_from_mapping(_profile())
    for key, bad_value in (
        ("payoff_zero_rate", 0.03),
        ("empirical_reward_risk_ratio", 99.0),
        ("empirical_break_even_probability", 0.99),
    ):
        bad = _profile()
        bad[key] = bad_value
        with pytest.raises(Exception):
            profile_from_mapping(bad)


def test_representative_payoff_context_handoff_binds_hrp_to_ledger() -> None:
    ledger = pd.DataFrame({
        "representative_id": ["rep1", "rep1", "rep2"],
        "edge_id": ["A/x_long", "A/x_long", "B/y_short"],
        "threshold": [0.61, 0.61, 0.67],
        "payoff_context_version": [PAYOFF_CONTEXT_VERSION] * 3,
        "payoff_context_id": ["ctx1", "ctx1", "ctx2"],
    })
    weights = pd.DataFrame({
        "representative_id": ["rep1", "rep2"],
        "edge_id": ["A/x_long", "B/y_short"],
        "threshold": [0.61, 0.67],
        "payoff_context_version": [PAYOFF_CONTEXT_VERSION] * 2,
        "payoff_context_id": ["ctx1", "ctx2"],
    })
    result = validate_representative_payoff_context_handoff(ledger, weights, context="unit")
    assert result["status"] == "PASS"
    bad = weights.copy()
    bad.loc[0, "payoff_context_id"] = "other"
    with pytest.raises(Exception):
        validate_representative_payoff_context_handoff(ledger, bad, context="unit-bad")


def test_research_generation_chain_rejects_partial_rerun(tmp_path) -> None:
    edge_id = "XAUUSD/absorption_long"
    stem = "xauusd_absorption_long"
    research_dataset = tmp_path / "research_dataset"
    model_rows = research_dataset / f"{stem}_model_rows.csv"
    selected = tmp_path / "selected.json"
    schema = tmp_path / "schema.json"
    params = tmp_path / "params.json"
    model = tmp_path / "model.pkl"
    oof = tmp_path / "oof.csv"
    edge_yaml = tmp_path / "edge.yaml"
    m1 = tmp_path / "step01.json"
    m2 = tmp_path / "step02.json"
    m3 = tmp_path / "step03.json"
    calibrator_path = tmp_path / "calibrator.json"
    threshold_path = tmp_path / "threshold.json"
    calibration_manifest_path = tmp_path / "calibration_manifest.json"

    _write(model_rows, "Datetime,realized_r,meta_label\n2026-01-01T00:00:00Z,0.1,1\n")
    _write_json(selected, {"selected_features": ["f1"]})
    _write_json(schema, {"features": [{"feature_name": "f1"}]})
    _write_json(params, {"max_depth": 2})
    _write(model, "model-bytes")
    _write(oof, "Datetime,raw_probability\n2026-01-01T00:00:00Z,0.6\n")
    # Retired-Step03A replacement: geometry identity now comes straight from
    # edge.yaml's labeling: block (the same source Task 2A/Step04 hash from),
    # not a dedicated Step03A execution-audit manifest.
    _write(
        edge_yaml,
        "edge_id: XAUUSD/absorption_long\n"
        "side_name: long\n"
        "labeling:\n"
        "  vertical_horizon: 20\n"
        "  pt_multiplier: 1.5\n"
        "  sl_multiplier: 1.0\n"
        "  tie_policy: sl_first\n"
        "  tick_resolution_price: mid\n"
        "  barrier_contract: pt_price_above_entry_sl_price_below_entry\n",
    )

    layout = SimpleNamespace(
        edge_id=edge_id, edge_stem=stem, side_name="long", research_dataset_dir=research_dataset,
        resolution_csv=model_rows, edge_yaml=edge_yaml,
        feature_surgery_manifest_json=m1, feature_schema_json=schema,
        feature_surgery_selected_features_json=selected, model_training_manifest_json=m2,
        model_best_params_json=params, model_pkl=model, oof_manifest_json=m3,
        oof_predictions_csv=oof, calibrator_json=calibrator_path,
        selected_threshold_json=threshold_path, calibration_manifest_json=calibration_manifest_path,
    )

    _write_json(m1, {
        "edge_id": edge_id, "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
        "gross_target_column": GROSS_TARGET_COLUMN, "gross_target_definition": GROSS_TARGET_DEFINITION,
        "training_target_column": TARGET_COLUMN, "training_eligibility_column": ELIGIBILITY_COLUMN,
        "phase3a_step01_provenance_status": "PASS", "model_rows_sha256": sha256_file(model_rows),
        "feature_schema_sha256": sha256_file(schema), "selected_features_sha256": sha256_file(selected),
    })
    _write_json(m2, {
        "edge_id": edge_id, "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
        "gross_target_column": GROSS_TARGET_COLUMN, "gross_target_definition": GROSS_TARGET_DEFINITION,
        "training_target_column": TARGET_COLUMN, "training_eligibility_column": ELIGIBILITY_COLUMN,
        "step01_feature_surgery_manifest_sha256": sha256_file(m1),
        "model_rows_sha256": sha256_file(model_rows), "selected_features_sha256": sha256_file(selected),
        "feature_schema_sha256": sha256_file(schema), "best_params_sha256": sha256_file(params),
        "model_artifact_sha256": sha256_file(model),
    })
    _write_json(m3, {
        "edge_id": edge_id, "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
        "gross_target_column": GROSS_TARGET_COLUMN, "gross_target_definition": GROSS_TARGET_DEFINITION,
        "training_target_column": TARGET_COLUMN, "training_eligibility_column": ELIGIBILITY_COLUMN,
        "step02_training_manifest_sha256": sha256_file(m2),
        "step01_feature_surgery_manifest_sha256": sha256_file(m1),
        "model_rows_sha256": sha256_file(model_rows), "selected_features_sha256": sha256_file(selected),
        "feature_schema_sha256": sha256_file(schema), "best_params_sha256": sha256_file(params),
        "oof_predictions_sha256": sha256_file(oof),
    })

    upstream = validate_step03_generation(layout)
    from shared.edges.trade_geometry import load_trade_geometry
    geometry_hash = load_trade_geometry(edge_yaml, expected_edge_id=edge_id, expected_side_name="long")["geometry_hash"]

    calibrator = {
        "edge_id": edge_id, "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
        "target_contract_version": EXECUTED_TARGET_CONTRACT_VERSION,
        "execution_contract_version": EXECUTION_CONTRACT_VERSION, "coef": 1.25,
    }
    _write_json(calibrator_path, calibrator)
    context = build_payoff_context(edge_id=edge_id, threshold=0.63, geometry_hash=geometry_hash, payoff_profile=_profile())
    threshold = {
        "edge_id": edge_id, "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
        "calibration_target_contract_version": EXECUTED_TARGET_CONTRACT_VERSION,
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "payoff_context_id": context["payoff_context_id"], "payoff_context": context,
        "payoff_profile_provenance": {
            "oof_predictions_sha256": sha256_file(oof),
            "execution_contract_version": EXECUTION_CONTRACT_VERSION,
            "geometry_hash": geometry_hash, "threshold": 0.63,
            "probability_column": THRESHOLD_PROBABILITY_COLUMN,
        },
        "upstream_generation": upstream,
    }
    _write_json(threshold_path, threshold)
    _write_json(calibration_manifest_path, {
        "edge_id": edge_id, "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "calibration_target_contract_version": EXECUTED_TARGET_CONTRACT_VERSION,
        "upstream_generation": upstream, "live_sigmoid_calibrator": calibrator,
        "selected_threshold": threshold, "payoff_context_id": context["payoff_context_id"],
    })

    current = validate_research_generation_chain(layout)
    assert current["payoff_context_id"] == context["payoff_context_id"]

    # Simulate a partial rerun: OOF predictions change but Step03's manifest
    # (and everything downstream referencing its hash) is not regenerated.
    _write(oof, "Datetime,raw_probability\n2026-01-01T00:00:00Z,0.9\n")
    with pytest.raises(Exception):
        validate_research_generation_chain(layout)
