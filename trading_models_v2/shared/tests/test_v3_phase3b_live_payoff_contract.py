# QUANTARA_V3_PHASE3B_ALLOCATOR_FROZEN_LIVE_PAYOFF_V1
from __future__ import annotations

from pathlib import Path

import pytest

from shared.live_payoff_contract import (
    LivePayoffContractError,
    compute_frozen_live_afml,
    freeze_live_payoff_from_representative,
    validate_live_payoff_edge,
    verify_frozen_live_afml,
)
from shared.validation.v3_semantic_contract import build_payoff_context


def _representative() -> dict:
    profile = {
        "payoff_profile_version": "quantara_v3_empirical_exact_executable_payoff_v1",
        "payoff_profile_valid": True,
        "payoff_profile_invalid_reason": "",
        "payoff_trade_count": 50,
        "payoff_win_count": 25,
        "payoff_loss_count": 20,
        "payoff_zero_count": 5,
        "payoff_zero_rate": 0.10,
        "min_wins_required": 20,
        "min_losses_required": 20,
        "empirical_avg_win_r": 0.80,
        "empirical_avg_loss_r": -0.40,
        "empirical_reward_risk_ratio": 2.0,
        "empirical_break_even_probability": 0.30,
    }
    context = build_payoff_context(
        edge_id="TEST/edge_long",
        threshold=0.61,
        geometry_hash="geometry-test-hash",
        payoff_profile=profile,
    )
    return {
        "representative_id": "TEST_edge_long_rep_thr0p61",
        "edge_id": "TEST/edge_long",
        "threshold": 0.61,
        "threshold_probability_column": "calibrated_probability",
        **profile,
        "payoff_context_version": context["payoff_context_version"],
        "payoff_context_id": context["payoff_context_id"],
        "payoff_context_edge_id": context["edge_id"],
        "gross_label_contract_version": context["gross_label_contract_version"],
        "execution_contract_version": context["execution_contract_version"],
        "executed_target_contract_version": context["executed_target_contract_version"],
        "geometry_hash": context["geometry_hash"],
        "afml_sizing_method": context["afml_sizing_method"],
    }


def _live_edge() -> dict:
    rep = _representative()
    frozen = freeze_live_payoff_from_representative(
        rep,
        edge_id=rep["edge_id"],
        threshold=rep["threshold"],
        geometry_hash=rep["geometry_hash"],
    )
    return {
        "edge_id": rep["edge_id"],
        "final_live_threshold": rep["threshold"],
        "geometry_hash": rep["geometry_hash"],
        "live_payoff_contract_version": frozen["live_payoff_contract_version"],
        "payoff_context_version": frozen["payoff_context_version"],
        "payoff_context_id": frozen["payoff_context_id"],
        "live_afml_sizing_method": frozen["afml_sizing_method"],
        "live_payoff_profile": frozen["payoff_profile"],
        "live_payoff_context": frozen["payoff_context"],
    }


def test_allocator_frozen_payoff_drives_live_afml_and_executor_reverification() -> None:
    edge = _live_edge()
    validated = validate_live_payoff_edge(edge)
    assert validated["payoff_context_id"] == edge["payoff_context_id"]
    result = compute_frozen_live_afml(0.70, edge)
    assert 0.0 < float(result["afml_bet_size"]) <= 1.0
    checked = verify_frozen_live_afml(
        0.70,
        float(result["afml_bet_size"]),
        edge,
        claimed_payoff_context_id=str(result["payoff_context_id"]),
    )
    assert checked["payoff_context_id"] == result["payoff_context_id"]

    with pytest.raises(LivePayoffContractError):
        verify_frozen_live_afml(
            0.70,
            float(result["afml_bet_size"]) + 1e-4,
            edge,
            claimed_payoff_context_id=str(result["payoff_context_id"]),
        )


def test_stale_or_tampered_live_payoff_fails_closed() -> None:
    edge = _live_edge()
    stale = dict(edge)
    stale.pop("live_payoff_contract_version")
    with pytest.raises(LivePayoffContractError):
        validate_live_payoff_edge(stale)

    tampered = dict(edge)
    tampered["live_payoff_profile"] = dict(edge["live_payoff_profile"])
    tampered["live_payoff_profile"]["empirical_avg_win_r"] = 0.81
    with pytest.raises(Exception):
        validate_live_payoff_edge(tampered)

    good = compute_frozen_live_afml(0.70, edge)
    with pytest.raises(LivePayoffContractError):
        verify_frozen_live_afml(
            0.70,
            float(good["afml_bet_size"]),
            edge,
            claimed_payoff_context_id="wrong-context",
        )


def test_phase3b_source_hooks_are_present() -> None:
    root = Path(__file__).resolve().parents[2]
    checks = {
        root / "portfolio_allocator" / "04c_select_representative_threshold_candidates.py":
            ["validate_payoff_context_frame", "PHASE3B_CONTEXT_REQUIRED_COLUMNS"],
        root / "portfolio_live" / "00_build_live_deployment_manifest.py":
            ["freeze_live_payoff_from_representative", "live_payoff_contract_version"],
        root / "portfolio_live" / "01_validate_live_manifest.py":
            ["validate_live_payoff_edge", "LIVE_PAYOFF_CONTRACT_VERSION"],
        root / "portfolio_live" / "portfolio_live_dashboard_orchestrator.py":
            ["compute_frozen_live_afml", "runtime_research_afml_bet_size", "payoff_context_id"],
        root / "portfolio_live" / "portfolio_execution_bot.py":
            ["verify_frozen_live_afml", "live_manifest_threshold_mismatch", "payoff_context_id"],
    }
    for path, needles in checks.items():
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle in text, f"{path.name} missing Phase-3B hook {needle}"
