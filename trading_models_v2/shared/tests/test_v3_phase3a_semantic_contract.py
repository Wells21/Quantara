# QUANTARA_V3_SEMANTIC_PHASE3A_PROVENANCE_CONTEXT_V1
from __future__ import annotations

# QUANTARA_V3_SEMANTIC_PHASE3A1_STEP01_PROVENANCE_V1

# QUANTARA_V3_PHASE3C_SEMANTIC_GENERATION_CLOSURE_V1
import copy

import pandas as pd
import pytest

from shared.validation.v3_semantic_contract import (
    AFML_SIZING_METHOD, EXECUTED_TARGET_CONTRACT_VERSION, EXECUTION_CONTRACT_VERSION,
    GROSS_LABEL_CONTRACT_VERSION, PAYOFF_CONTEXT_VERSION, PAYOFF_PROFILE_VERSION,
    build_payoff_context, validate_gross_label_frame, validate_payoff_context_mapping,
)


def _profile() -> dict:
    return {
        "payoff_profile_version": PAYOFF_PROFILE_VERSION,
        "payoff_profile_valid": True,
        "payoff_profile_invalid_reason": "",
        "payoff_trade_count": 100,
        "payoff_win_count": 56,
        "payoff_loss_count": 42,
        "payoff_zero_count": 2,
        "payoff_zero_rate": 0.02,
        "min_wins_required": 5,
        "min_losses_required": 5,
        "empirical_avg_win_r": 0.71,
        "empirical_avg_loss_r": -0.48,
        "empirical_reward_risk_ratio": 0.71 / 0.48,
        "empirical_break_even_probability": (1.0 - 0.02) * 0.48 / (0.71 + 0.48),
    }


def test_gross_label_contract_is_realized_r_positive() -> None:
    frame = pd.DataFrame({"realized_r": [-0.2, 0.0, 0.1], "meta_label": [0, 0, 1]})
    validate_gross_label_frame(frame, context="unit")
    bad = frame.copy()
    bad.loc[1, "meta_label"] = 1
    with pytest.raises(Exception):
        validate_gross_label_frame(bad, context="unit_bad")


def test_payoff_context_binds_threshold_profile_and_generation() -> None:
    context = build_payoff_context(
        edge_id="XAUUSD/absorption_long", threshold=0.63,
        geometry_hash="abc123", payoff_profile=_profile(),
    )
    assert context["payoff_context_version"] == PAYOFF_CONTEXT_VERSION
    assert context["gross_label_contract_version"] == GROSS_LABEL_CONTRACT_VERSION
    assert context["execution_contract_version"] == EXECUTION_CONTRACT_VERSION
    assert context["executed_target_contract_version"] == EXECUTED_TARGET_CONTRACT_VERSION
    assert context["afml_sizing_method"] == AFML_SIZING_METHOD
    assert context["payoff_profile_valid"] is True
    assert context["payoff_profile_invalid_reason"] == ""
    assert context["min_wins_required"] == 5
    assert context["min_losses_required"] == 5
    validate_payoff_context_mapping(
        context, expected_edge_id="XAUUSD/absorption_long",
        expected_threshold=0.63, expected_geometry_hash="abc123",
    )
    bad_threshold = copy.deepcopy(context)
    bad_threshold["threshold"] = 0.64
    with pytest.raises(Exception):
        validate_payoff_context_mapping(bad_threshold)
    bad_profile = copy.deepcopy(context)
    bad_profile["empirical_avg_win_r"] = 0.72
    with pytest.raises(Exception):
        validate_payoff_context_mapping(bad_profile)
    bad_minimum = copy.deepcopy(context)
    bad_minimum["min_wins_required"] = 6
    with pytest.raises(Exception):
        validate_payoff_context_mapping(bad_minimum)


def test_payoff_profile_arithmetic_identities_fail_closed() -> None:
    bad_zero_rate = _profile()
    bad_zero_rate["payoff_zero_rate"] = 0.03
    with pytest.raises(Exception):
        build_payoff_context(
            edge_id="XAUUSD/absorption_long", threshold=0.63,
            geometry_hash="abc123", payoff_profile=bad_zero_rate,
        )

    bad_rr = _profile()
    bad_rr["empirical_reward_risk_ratio"] *= 1.01
    with pytest.raises(Exception):
        build_payoff_context(
            edge_id="XAUUSD/absorption_long", threshold=0.63,
            geometry_hash="abc123", payoff_profile=bad_rr,
        )

    bad_pbe = _profile()
    bad_pbe["empirical_break_even_probability"] += 0.01
    with pytest.raises(Exception):
        build_payoff_context(
            edge_id="XAUUSD/absorption_long", threshold=0.63,
            geometry_hash="abc123", payoff_profile=bad_pbe,
        )
