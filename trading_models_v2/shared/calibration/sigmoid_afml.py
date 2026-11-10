# QUANTARA_V3_SEMANTIC_PHASE2_EMPIRICAL_EXECUTABLE_PAYOFF_V1_REV3
"""Shared sigmoid calibration and empirical executable-payoff AFML utilities for Quantara V3."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

RAW_PROBABILITY_COL = "raw_probability"
CALIBRATED_PROBABILITY_COL = "calibrated_probability"
AFML_BET_SIZE_COL = "afml_bet_size"
APPROVED_SIGNAL_COL = "approved_signal"
REWARD_RISK_RATIO_COL = "reward_risk_ratio"
BREAK_EVEN_PROBABILITY_COL = "break_even_probability"
AFML_ECONOMIC_STRENGTH_COL = "afml_economic_strength"
AFML_EFFECTIVE_PROBABILITY_COL = "afml_effective_probability"
AFML_SIZING_METHOD_COL = "afml_sizing_method"
AFML_SIZING_METHOD = "empirical_exact_executable_break_even_rescaled_afml_v1"
PAYOFF_PROFILE_VERSION = "quantara_v3_empirical_exact_executable_payoff_v1"
EXECUTED_TARGET_CONTRACT_VERSION = "quantara_v3_executed_positive_meta_label_v1"
DEFAULT_MIN_PAYOFF_WINS = 20
DEFAULT_MIN_PAYOFF_LOSSES = 20


class SigmoidAfmlError(RuntimeError):
    """Raised when probability/calibration/AFML logic must fail closed."""


def load_calibrator_json(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        raise SigmoidAfmlError(f"Missing calibrator JSON: {file_path}")
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SigmoidAfmlError(f"Invalid calibrator JSON {file_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SigmoidAfmlError(f"Calibrator JSON must be an object: {file_path}")
    validate_calibrator_payload(payload)
    return payload


def clip_probability(probability: Any, eps: float = 1e-6) -> np.ndarray:
    return np.clip(np.asarray(probability, dtype="float64"), eps, 1.0 - eps)


def logit(probability: Any, eps: float = 1e-6) -> np.ndarray:
    p = clip_probability(probability, eps=eps)
    return np.log(p / (1.0 - p))


def sigmoid(x: Any) -> np.ndarray:
    x_arr = np.asarray(x, dtype="float64")
    return 1.0 / (1.0 + np.exp(-np.clip(x_arr, -60.0, 60.0)))


def validate_calibrator_payload(calibrator: Mapping[str, Any]) -> None:
    method = calibrator.get("method")
    if method not in {"sigmoid_platt_logit", "identity"}:
        raise SigmoidAfmlError(f"Unsupported calibrator method: {method!r}")
    output_column = calibrator.get("output_column", CALIBRATED_PROBABILITY_COL)
    if output_column != CALIBRATED_PROBABILITY_COL:
        raise SigmoidAfmlError(
            f"Calibrator output_column must be {CALIBRATED_PROBABILITY_COL!r}, got {output_column!r}"
        )
    if method == "sigmoid_platt_logit":
        for key in ("coef", "intercept"):
            try:
                float(calibrator[key])
            except Exception as exc:
                raise SigmoidAfmlError(f"Sigmoid calibrator missing numeric {key!r}") from exc


def apply_sigmoid_calibrator(raw_probability: Any, calibrator: Mapping[str, Any]) -> np.ndarray:
    validate_calibrator_payload(calibrator)
    method = calibrator.get("method")
    eps = float(calibrator.get("probability_clip", 1e-6))
    if method == "identity":
        return clip_probability(raw_probability, eps=eps)
    coef = float(calibrator["coef"])
    intercept = float(calibrator["intercept"])
    return clip_probability(sigmoid(intercept + coef * logit(raw_probability, eps=eps)), eps=eps)


def normal_cdf(x: Any) -> np.ndarray:
    x_arr = np.asarray(x, dtype="float64")
    return 0.5 * (1.0 + np.vectorize(math.erf)(x_arr / math.sqrt(2.0)))


def afml_probability_bet_size(calibrated_probability: Any) -> np.ndarray:
    """AFML Ch.10 binary bet-size magnitude: m = 2*Phi(z) - 1,
    z = (p-1/2)/sqrt(p(1-p)), clipped to [0,1] since this function is only
    ever called on an already-directional "effective probability" (>= 0.5
    by construction upstream, see payoff_aware_afml_components below) -- a
    hypothetical p < 0.5 input would give z < 0 and is clipped to 0 rather
    than propagated negative, preserving this function's original contract.

    Delegates to shared.sizing.afml_ch10_reference.probability_to_signal
    (the single canonical AFML Ch.10 reference implementation, independently
    fuzz/fixture-tested in tests/test_mldp_afml_ch10_reference.py) rather
    than maintaining a second copy of the same z/Phi formula -- this module
    is not one of the Colab-standalone research stations (see
    docs/mldp/A3_PURGE_EMBARGO_VERIFICATION.md's operational note for that
    distinct constraint, which does not apply here), so there is no reason
    for this to be a separate implementation.
    """
    from shared.sizing.afml_ch10_reference import probability_to_signal

    p = clip_probability(calibrated_probability)
    size = probability_to_signal(p, predicted_label=1.0, num_classes=2)
    return np.clip(size, 0.0, 1.0)


def _validated_reward_risk_ratio(reward_risk_ratio: Any) -> np.ndarray:
    ratio = np.asarray(reward_risk_ratio, dtype="float64")
    if not np.all(np.isfinite(ratio)):
        raise SigmoidAfmlError("reward_risk_ratio must contain only finite values.")
    if np.any(ratio <= 0.0):
        raise SigmoidAfmlError("reward_risk_ratio must be strictly positive.")
    return ratio


def break_even_probability_from_reward_risk(reward_risk_ratio: Any) -> np.ndarray:
    """Geometry/static diagnostic only; executable AFML uses empirical payoff profiles."""
    ratio = _validated_reward_risk_ratio(reward_risk_ratio)
    return 1.0 / (1.0 + ratio)


def empirical_payoff_profile(
    executed_r: Any,
    *,
    min_wins: int = DEFAULT_MIN_PAYOFF_WINS,
    min_losses: int = DEFAULT_MIN_PAYOFF_LOSSES,
) -> dict[str, Any]:
    r = np.asarray(executed_r, dtype="float64")
    r = r[np.isfinite(r)]
    wins = r[r > 0.0]
    losses = r[r < 0.0]
    zeros = r[r == 0.0]
    profile: dict[str, Any] = {
        "payoff_profile_version": PAYOFF_PROFILE_VERSION,
        "payoff_source": "exact_oof_executed_r_selected_probability_population",
        "payoff_trade_count": int(len(r)),
        "payoff_win_count": int(len(wins)),
        "payoff_loss_count": int(len(losses)),
        "payoff_zero_count": int(len(zeros)),
        "payoff_zero_rate": float(len(zeros) / len(r)) if len(r) else float("nan"),
        "min_wins_required": int(min_wins),
        "min_losses_required": int(min_losses),
        "empirical_avg_win_r": float(np.mean(wins)) if len(wins) else float("nan"),
        "empirical_avg_loss_r": float(np.mean(losses)) if len(losses) else float("nan"),
        "empirical_reward_risk_ratio": float("nan"),
        "empirical_break_even_probability": float("nan"),
        "payoff_profile_valid": False,
        "payoff_profile_invalid_reason": "",
    }
    if len(wins) < int(min_wins):
        profile["payoff_profile_invalid_reason"] = f"wins_below_min:{len(wins)}<{int(min_wins)}"
        return profile
    if len(losses) < int(min_losses):
        profile["payoff_profile_invalid_reason"] = f"losses_below_min:{len(losses)}<{int(min_losses)}"
        return profile
    avg_win = float(np.mean(wins))
    avg_loss = float(np.mean(losses))
    if not np.isfinite(avg_win) or avg_win <= 0.0 or not np.isfinite(avg_loss) or avg_loss >= 0.0:
        profile["payoff_profile_invalid_reason"] = "invalid_mean_win_or_loss"
        return profile
    loss_abs = abs(avg_loss)
    denom = avg_win + loss_abs
    if denom <= 0.0:
        profile["payoff_profile_invalid_reason"] = "nonpositive_payoff_denominator"
        return profile
    rr = avg_win / loss_abs
    zero_rate = float(len(zeros) / len(r)) if len(r) else 0.0
    # Calibration targets P(executed_r > 0); exact-zero outcomes belong to the
    # non-positive class but contribute 0R. Hold the observed zero mass fixed
    # when solving E[R]=0 so the break-even probability matches that target.
    p_be = (1.0 - zero_rate) * loss_abs / denom
    profile.update({
        "empirical_reward_risk_ratio": float(rr),
        "empirical_break_even_probability": float(p_be),
        "payoff_profile_valid": True,
        "payoff_profile_invalid_reason": "",
    })
    return profile


def validate_payoff_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(profile, Mapping):
        raise SigmoidAfmlError("payoff profile must be a mapping")
    if str(profile.get("payoff_profile_version", "")) != PAYOFF_PROFILE_VERSION:
        raise SigmoidAfmlError(
            f"payoff_profile_version must be {PAYOFF_PROFILE_VERSION!r}, got {profile.get('payoff_profile_version')!r}"
        )
    _valid_flag = profile.get("payoff_profile_valid")
    if not isinstance(_valid_flag, (bool, np.bool_)) or not bool(_valid_flag):
        raise SigmoidAfmlError(
            f"payoff profile is not valid: {profile.get('payoff_profile_invalid_reason')!r}"
        )
    try:
        wins = int(profile["payoff_win_count"])
        losses = int(profile["payoff_loss_count"])
        zeros = int(profile.get("payoff_zero_count", 0))
        trade_count = int(profile["payoff_trade_count"])
        zero_rate = float(profile.get("payoff_zero_rate", zeros / trade_count if trade_count else float("nan")))
        min_wins = int(profile.get("min_wins_required", DEFAULT_MIN_PAYOFF_WINS))
        min_losses = int(profile.get("min_losses_required", DEFAULT_MIN_PAYOFF_LOSSES))
        avg_win = float(profile["empirical_avg_win_r"])
        avg_loss = float(profile["empirical_avg_loss_r"])
        rr = float(profile["empirical_reward_risk_ratio"])
        p_be = float(profile["empirical_break_even_probability"])
    except Exception as exc:
        raise SigmoidAfmlError("payoff profile is missing required numeric fields") from exc
    if wins < min_wins or losses < min_losses:
        raise SigmoidAfmlError(
            f"payoff profile has insufficient wins/losses: wins={wins}/{min_wins} losses={losses}/{min_losses}"
        )
    if trade_count != wins + losses + zeros or trade_count <= 0:
        raise SigmoidAfmlError(
            f"payoff count identity mismatch: trades={trade_count} wins={wins} losses={losses} zeros={zeros}"
        )
    expected_zero_rate = zeros / trade_count
    if not np.isfinite(zero_rate) or not np.isclose(zero_rate, expected_zero_rate, atol=1e-15, rtol=0.0):
        raise SigmoidAfmlError(
            f"payoff zero-rate identity mismatch: {zero_rate} vs {expected_zero_rate}"
        )
    if not (np.isfinite(avg_win) and avg_win > 0.0 and np.isfinite(avg_loss) and avg_loss < 0.0):
        raise SigmoidAfmlError("payoff profile average win/loss signs are invalid")
    expected_rr = avg_win / abs(avg_loss)
    expected_pbe = (1.0 - zero_rate) * abs(avg_loss) / (avg_win + abs(avg_loss))
    if not np.isclose(rr, expected_rr, atol=1e-12, rtol=0.0):
        raise SigmoidAfmlError(f"empirical reward:risk identity mismatch: {rr} vs {expected_rr}")
    if not np.isclose(p_be, expected_pbe, atol=1e-12, rtol=0.0):
        raise SigmoidAfmlError(f"empirical break-even identity mismatch: {p_be} vs {expected_pbe}")
    if not 0.0 < p_be < 1.0:
        raise SigmoidAfmlError(f"empirical break-even probability must be in (0,1), got {p_be}")
    out = dict(profile)
    out["payoff_zero_rate"] = zero_rate
    out["empirical_reward_risk_ratio"] = rr
    out["empirical_break_even_probability"] = p_be
    return out


def payoff_profile_from_threshold_payload(threshold_payload: Mapping[str, Any]) -> dict[str, Any]:
    profile = threshold_payload.get("payoff_profile")
    if not isinstance(profile, Mapping):
        selected = threshold_payload.get("selected")
        if isinstance(selected, Mapping):
            profile = selected.get("payoff_profile")
    if not isinstance(profile, Mapping):
        raise SigmoidAfmlError(
            "Selected threshold artifact is stale and missing an empirical exact-executable payoff_profile. Rerun Step 04."
        )
    return validate_payoff_profile(profile)


def payoff_aware_afml_components(
    calibrated_probability: Any,
    reward_risk_ratio: Any,
    *,
    break_even_probability: Any | None = None,
) -> dict[str, np.ndarray]:
    probability = clip_probability(calibrated_probability)
    ratio = _validated_reward_risk_ratio(reward_risk_ratio)
    try:
        probability, ratio = np.broadcast_arrays(probability, ratio)
    except ValueError as exc:
        raise SigmoidAfmlError(
            "calibrated_probability and reward_risk_ratio are not broadcast-compatible."
        ) from exc
    if break_even_probability is None:
        break_even = 1.0 / (1.0 + ratio)
    else:
        break_even = np.asarray(break_even_probability, dtype="float64")
        try:
            break_even = np.broadcast_to(break_even, probability.shape)
        except ValueError as exc:
            raise SigmoidAfmlError("break_even_probability is not broadcast-compatible") from exc
        if not np.all(np.isfinite(break_even)) or np.any((break_even <= 0.0) | (break_even >= 1.0)):
            raise SigmoidAfmlError("break_even_probability must be finite and in (0,1)")
    denominator = np.maximum(1.0 - break_even, np.finfo("float64").eps)
    economic_strength = np.clip((probability - break_even) / denominator, 0.0, 1.0)
    effective_probability = 0.5 + 0.5 * economic_strength
    bet_size = afml_probability_bet_size(effective_probability)
    return {
        REWARD_RISK_RATIO_COL: ratio.astype("float64"),
        BREAK_EVEN_PROBABILITY_COL: np.asarray(break_even, dtype="float64"),
        AFML_ECONOMIC_STRENGTH_COL: economic_strength.astype("float64"),
        AFML_EFFECTIVE_PROBABILITY_COL: effective_probability.astype("float64"),
        AFML_BET_SIZE_COL: np.asarray(bet_size, dtype="float64"),
    }


def payoff_aware_afml_bet_size(calibrated_probability: Any, reward_risk_ratio: Any) -> np.ndarray:
    return payoff_aware_afml_components(calibrated_probability, reward_risk_ratio)[AFML_BET_SIZE_COL]


def payoff_profile_afml_components(calibrated_probability: Any, payoff_profile: Mapping[str, Any]) -> dict[str, np.ndarray]:
    profile = validate_payoff_profile(payoff_profile)
    return payoff_aware_afml_components(
        calibrated_probability,
        profile["empirical_reward_risk_ratio"],
        break_even_probability=profile["empirical_break_even_probability"],
    )


def validate_threshold_payload(threshold_payload: Mapping[str, Any]) -> float:
    threshold_probability_column = threshold_payload.get("threshold_probability_column")
    if threshold_probability_column != CALIBRATED_PROBABILITY_COL:
        raise SigmoidAfmlError(
            f"Selected threshold must use calibrated_probability, got {threshold_probability_column!r}"
        )
    selected = threshold_payload.get("selected", {})
    value = None
    if isinstance(selected, Mapping):
        value = selected.get("selected_threshold", selected.get("threshold"))
    if value is None:
        value = threshold_payload.get("selected_threshold", threshold_payload.get("threshold"))
    if value is None:
        raise SigmoidAfmlError("Selected threshold artifact does not contain selected_threshold/threshold.")
    threshold = float(value)
    if not np.isfinite(threshold) or threshold <= 0.0 or threshold >= 1.0:
        raise SigmoidAfmlError(f"Selected threshold must be in (0,1), got {threshold!r}")
    return threshold


def apply_probability_flow(
    raw_probability: Any,
    calibrator: Mapping[str, Any],
    selected_threshold: float,
    *,
    payoff_profile: Mapping[str, Any] | None = None,
    reward_risk_ratio: Any | None = None,
) -> pd.DataFrame:
    raw = clip_probability(raw_probability)
    calibrated = apply_sigmoid_calibrator(raw, calibrator)
    if payoff_profile is not None:
        profile = validate_payoff_profile(payoff_profile)
        components = payoff_profile_afml_components(calibrated, profile)
    elif reward_risk_ratio is not None:
        # Backward-compatible diagnostic path only. Quantara V3 scoring must pass payoff_profile.
        components = payoff_aware_afml_components(calibrated, reward_risk_ratio)
    else:
        raise SigmoidAfmlError("apply_probability_flow requires payoff_profile for Quantara V3")
    threshold = float(selected_threshold)
    approved = calibrated >= threshold
    row_count = int(np.asarray(calibrated).size)
    return pd.DataFrame({
        RAW_PROBABILITY_COL: raw,
        CALIBRATED_PROBABILITY_COL: calibrated,
        REWARD_RISK_RATIO_COL: components[REWARD_RISK_RATIO_COL],
        BREAK_EVEN_PROBABILITY_COL: components[BREAK_EVEN_PROBABILITY_COL],
        AFML_ECONOMIC_STRENGTH_COL: components[AFML_ECONOMIC_STRENGTH_COL],
        AFML_EFFECTIVE_PROBABILITY_COL: components[AFML_EFFECTIVE_PROBABILITY_COL],
        AFML_BET_SIZE_COL: components[AFML_BET_SIZE_COL],
        AFML_SIZING_METHOD_COL: [AFML_SIZING_METHOD] * row_count,
        "selected_threshold": threshold,
        "threshold_probability_column": CALIBRATED_PROBABILITY_COL,
        APPROVED_SIGNAL_COL: approved.astype(bool),
    })


def _run_self_test() -> None:
    r = np.array([1.0] * 20 + [-0.5] * 20, dtype=float)
    profile = empirical_payoff_profile(r)
    validate_payoff_profile(profile)
    if not np.isclose(profile["empirical_reward_risk_ratio"], 2.0, atol=1e-15):
        raise SystemExit("❌ empirical RR self-test failed")
    if not np.isclose(profile["empirical_break_even_probability"], 1.0 / 3.0, atol=1e-15):
        raise SystemExit("❌ empirical pBE self-test failed")
    raw = np.array([0.30, 1.0 / 3.0, 0.40, 0.75])
    calibrator = {"method": "identity", "output_column": CALIBRATED_PROBABILITY_COL, "probability_clip": 1e-6}
    out = apply_probability_flow(raw, calibrator, 0.40, payoff_profile=profile)
    if not np.isclose(float(out.loc[1, AFML_BET_SIZE_COL]), 0.0, atol=1e-12):
        raise SystemExit("❌ AFML must be zero at empirical break-even")
    if not float(out.loc[2, AFML_BET_SIZE_COL]) > 0.0:
        raise SystemExit("❌ AFML must be positive above empirical break-even")
    r_zero = np.array([1.0] * 20 + [-0.5] * 20 + [0.0] * 10, dtype=float)
    profile_zero = validate_payoff_profile(empirical_payoff_profile(r_zero))
    expected_zero_pbe = (1.0 - 0.20) * (0.5 / 1.5)
    if not np.isclose(profile_zero["payoff_zero_rate"], 0.20, atol=1e-15):
        raise SystemExit("❌ payoff zero-rate self-test failed")
    if not np.isclose(profile_zero["empirical_break_even_probability"], expected_zero_pbe, atol=1e-15):
        raise SystemExit("❌ zero-adjusted empirical pBE self-test failed")
    print("PASS: Quantara V3 empirical exact-executable payoff/AFML self-test passed.")


if __name__ == "__main__":
    _run_self_test()
