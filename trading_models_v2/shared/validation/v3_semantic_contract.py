# QUANTARA_V3_SEMANTIC_PHASE3A_PROVENANCE_CONTEXT_V1
from __future__ import annotations

# QUANTARA_V3_SEMANTIC_PHASE3A1_STEP01_PROVENANCE_V1

# QUANTARA_V3_PHASE3C_SEMANTIC_GENERATION_CLOSURE_V1
# Fail-closed Quantara V3 Phase-3A semantic-generation and payoff-context contract.

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from shared.edges.trade_geometry import load_trade_geometry

GROSS_LABEL_CONTRACT_VERSION = "quantara_v3_gross_economic_meta_label_v1"
# Per docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md: the gross
# layer is now gross_meta_label/gross_r (Task 2A resolution CSV columns),
# and the frozen training/OOF/calibration target is
# quote_executable_meta_label restricted to mldp_target_eligible==True rows.
GROSS_TARGET_COLUMN = "gross_meta_label"
GROSS_REALIZED_R_COLUMN = "gross_r"
GROSS_TARGET_DEFINITION = "int(gross_r > 0)"
TARGET_COLUMN = "quote_executable_meta_label"
ELIGIBILITY_COLUMN = "mldp_target_eligible"
EXECUTION_CONTRACT_VERSION = "v3_oof_exact_bidask_economic_label_v2"
EXECUTED_TARGET_CONTRACT_VERSION = "quantara_v3_executed_positive_meta_label_v1"
EXECUTED_TARGET_COLUMN = "executed_meta_label"
PAYOFF_PROFILE_VERSION = "quantara_v3_empirical_exact_executable_payoff_v1"
AFML_SIZING_METHOD = "empirical_exact_executable_break_even_rescaled_afml_v1"
PAYOFF_CONTEXT_VERSION = "quantara_v3_threshold_payoff_context_v1"
THRESHOLD_PROBABILITY_COLUMN = "calibrated_probability"

_PROFILE_KEYS = (
    "payoff_profile_version", "payoff_profile_valid", "payoff_profile_invalid_reason",
    "payoff_trade_count", "payoff_win_count", "payoff_loss_count", "payoff_zero_count",
    "payoff_zero_rate", "min_wins_required", "min_losses_required",
    "empirical_avg_win_r", "empirical_avg_loss_r",
    "empirical_reward_risk_ratio", "empirical_break_even_probability",
)


class V3SemanticContractError(RuntimeError):
    pass


def sha256_file(path: str | Path) -> str:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise V3SemanticContractError(f"Missing artifact for SHA256: {p}")
    h = hashlib.sha256()
    with p.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise V3SemanticContractError(f"Missing semantic artifact: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise V3SemanticContractError(f"Could not read JSON artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise V3SemanticContractError(f"JSON artifact must be an object: {path}")
    return payload


def _finite_float(value: Any, name: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise V3SemanticContractError(f"{name} must be numeric, got {value!r}") from exc
    if not math.isfinite(out):
        raise V3SemanticContractError(f"{name} must be finite, got {value!r}")
    return out


def _int_value(value: Any, name: str) -> int:
    out = _finite_float(value, name)
    rounded = int(round(out))
    if abs(out - rounded) > 1e-12:
        raise V3SemanticContractError(f"{name} must be integer-like, got {value!r}")
    return rounded


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    raise V3SemanticContractError(f"Boolean value is invalid: {value!r}")


def _canonical_float(value: Any, name: str) -> str:
    # .17g (full float64 precision) was found empirically to be too strict for
    # an identity check: recomputing the exact same empirical_payoff_profile
    # arithmetic (np.mean/division reductions) from the same underlying data
    # can legitimately differ by 1 ULP between runs (confirmed: 0.33903203775
    # 013546 vs 0.3390320377501354, relative difference 1.6e-16 -- pure
    # floating-point non-associativity, not a data or logic discrepancy), and
    # pandas' CSV float parser is not always bit-exact even with
    # float_precision="round_trip" for every case. 12 significant figures is
    # still far more precision than any economic quantity here needs (win
    # rate, R-multiples, probabilities) while giving a wide margin against
    # this kind of harmless numerical noise. See
    # config/mldp_deviation_register.json for the full derivation.
    return format(_finite_float(value, name), ".12g")


def profile_from_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    missing = [key for key in _PROFILE_KEYS if key not in mapping]
    if missing:
        raise V3SemanticContractError(f"Payoff profile missing fields: {missing}")
    profile = {key: mapping.get(key) for key in _PROFILE_KEYS}
    # A valid profile's invalid_reason is written as "" (see
    # fit_sigmoid_calibrator_and_thresholds.py), but pandas.read_csv reads an
    # empty CSV field back as NaN -- normalize that round-trip artifact back
    # to "" here so the identity hash matches what was originally computed
    # and embedded, instead of hashing the string "nan".
    reason = profile.get("payoff_profile_invalid_reason")
    if reason is None or (isinstance(reason, float) and math.isnan(reason)):
        profile["payoff_profile_invalid_reason"] = ""
    profile["payoff_profile_valid"] = _bool_value(profile["payoff_profile_valid"])
    if str(profile["payoff_profile_version"]) != PAYOFF_PROFILE_VERSION:
        raise V3SemanticContractError("Payoff profile version mismatch.")
    if profile["payoff_profile_valid"] is not True:
        raise V3SemanticContractError(
            f"Payoff profile is invalid: {profile.get('payoff_profile_invalid_reason')!r}"
        )
    for key in (
        "payoff_trade_count", "payoff_win_count", "payoff_loss_count", "payoff_zero_count",
        "min_wins_required", "min_losses_required",
    ):
        profile[key] = _int_value(profile[key], key)
    for key in (
        "payoff_zero_rate", "empirical_avg_win_r", "empirical_avg_loss_r",
        "empirical_reward_risk_ratio", "empirical_break_even_probability",
    ):
        profile[key] = _finite_float(profile[key], key)
    if profile["payoff_trade_count"] != (
        profile["payoff_win_count"] + profile["payoff_loss_count"] + profile["payoff_zero_count"]
    ):
        raise V3SemanticContractError("Payoff counts do not sum.")
    if profile["payoff_trade_count"] <= 0:
        raise V3SemanticContractError("payoff_trade_count must be > 0.")
    expected_zero_rate = profile["payoff_zero_count"] / profile["payoff_trade_count"]
    if not math.isclose(
        profile["payoff_zero_rate"], expected_zero_rate, rel_tol=0.0, abs_tol=1e-15
    ):
        raise V3SemanticContractError(
            f"payoff_zero_rate identity mismatch: {profile['payoff_zero_rate']} vs {expected_zero_rate}"
        )
    if profile["payoff_win_count"] < profile["min_wins_required"]:
        raise V3SemanticContractError("Payoff profile has too few wins.")
    if profile["payoff_loss_count"] < profile["min_losses_required"]:
        raise V3SemanticContractError("Payoff profile has too few losses.")
    if profile["empirical_avg_win_r"] <= 0.0:
        raise V3SemanticContractError("empirical_avg_win_r must be > 0.")
    if profile["empirical_avg_loss_r"] >= 0.0:
        raise V3SemanticContractError("empirical_avg_loss_r must be < 0.")
    if profile["empirical_reward_risk_ratio"] <= 0.0:
        raise V3SemanticContractError("empirical_reward_risk_ratio must be > 0.")
    expected_rr = profile["empirical_avg_win_r"] / abs(profile["empirical_avg_loss_r"])
    if not math.isclose(
        profile["empirical_reward_risk_ratio"], expected_rr, rel_tol=0.0, abs_tol=1e-12
    ):
        raise V3SemanticContractError("empirical_reward_risk_ratio arithmetic identity mismatch.")
    expected_pbe = (
        (1.0 - profile["payoff_zero_rate"])
        * abs(profile["empirical_avg_loss_r"])
        / (profile["empirical_avg_win_r"] + abs(profile["empirical_avg_loss_r"]))
    )
    if not math.isclose(
        profile["empirical_break_even_probability"], expected_pbe, rel_tol=0.0, abs_tol=1e-12
    ):
        raise V3SemanticContractError("empirical_break_even_probability arithmetic identity mismatch.")
    if not 0.0 < profile["empirical_break_even_probability"] < 1.0:
        raise V3SemanticContractError("empirical_break_even_probability must be in (0,1).")
    return profile


def _identity_projection(context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "payoff_context_version": str(context["payoff_context_version"]),
        "gross_label_contract_version": str(context["gross_label_contract_version"]),
        "execution_contract_version": str(context["execution_contract_version"]),
        "executed_target_contract_version": str(context["executed_target_contract_version"]),
        "edge_id": str(context["edge_id"]),
        "threshold": _canonical_float(context["threshold"], "threshold"),
        "threshold_probability_column": str(context["threshold_probability_column"]),
        "geometry_hash": str(context["geometry_hash"]),
        "payoff_profile_version": str(context["payoff_profile_version"]),
        "payoff_profile_valid": _bool_value(context["payoff_profile_valid"]),
        "payoff_profile_invalid_reason": str(context["payoff_profile_invalid_reason"]),
        "payoff_trade_count": _int_value(context["payoff_trade_count"], "payoff_trade_count"),
        "payoff_win_count": _int_value(context["payoff_win_count"], "payoff_win_count"),
        "payoff_loss_count": _int_value(context["payoff_loss_count"], "payoff_loss_count"),
        "payoff_zero_count": _int_value(context["payoff_zero_count"], "payoff_zero_count"),
        "payoff_zero_rate": _canonical_float(context["payoff_zero_rate"], "payoff_zero_rate"),
        "min_wins_required": _int_value(context["min_wins_required"], "min_wins_required"),
        "min_losses_required": _int_value(context["min_losses_required"], "min_losses_required"),
        "empirical_avg_win_r": _canonical_float(context["empirical_avg_win_r"], "empirical_avg_win_r"),
        "empirical_avg_loss_r": _canonical_float(context["empirical_avg_loss_r"], "empirical_avg_loss_r"),
        "empirical_reward_risk_ratio": _canonical_float(
            context["empirical_reward_risk_ratio"], "empirical_reward_risk_ratio"
        ),
        "empirical_break_even_probability": _canonical_float(
            context["empirical_break_even_probability"], "empirical_break_even_probability"
        ),
        "afml_sizing_method": str(context["afml_sizing_method"]),
    }


def payoff_context_id(context: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        _identity_projection(context), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_payoff_context(
    *, edge_id: str, threshold: float, geometry_hash: str,
    payoff_profile: Mapping[str, Any],
    threshold_probability_column: str = THRESHOLD_PROBABILITY_COLUMN,
) -> dict[str, Any]:
    profile = profile_from_mapping(payoff_profile)
    if not str(edge_id).strip():
        raise V3SemanticContractError("edge_id must be non-empty.")
    if not str(geometry_hash).strip():
        raise V3SemanticContractError("geometry_hash must be non-empty.")
    threshold_value = _finite_float(threshold, "threshold")
    if not 0.0 < threshold_value < 1.0:
        raise V3SemanticContractError(f"threshold must be in (0,1), got {threshold_value}")
    if threshold_probability_column != THRESHOLD_PROBABILITY_COLUMN:
        raise V3SemanticContractError("threshold probability column must be calibrated_probability.")
    context = {
        "payoff_context_version": PAYOFF_CONTEXT_VERSION,
        "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "executed_target_contract_version": EXECUTED_TARGET_CONTRACT_VERSION,
        "edge_id": str(edge_id),
        "threshold": threshold_value,
        "threshold_probability_column": THRESHOLD_PROBABILITY_COLUMN,
        "geometry_hash": str(geometry_hash),
        "payoff_profile_version": profile["payoff_profile_version"],
        "payoff_profile_valid": profile["payoff_profile_valid"],
        "payoff_profile_invalid_reason": profile["payoff_profile_invalid_reason"],
        "payoff_trade_count": profile["payoff_trade_count"],
        "payoff_win_count": profile["payoff_win_count"],
        "payoff_loss_count": profile["payoff_loss_count"],
        "payoff_zero_count": profile["payoff_zero_count"],
        "payoff_zero_rate": profile["payoff_zero_rate"],
        "min_wins_required": profile["min_wins_required"],
        "min_losses_required": profile["min_losses_required"],
        "empirical_avg_win_r": profile["empirical_avg_win_r"],
        "empirical_avg_loss_r": profile["empirical_avg_loss_r"],
        "empirical_reward_risk_ratio": profile["empirical_reward_risk_ratio"],
        "empirical_break_even_probability": profile["empirical_break_even_probability"],
        "afml_sizing_method": AFML_SIZING_METHOD,
    }
    context["payoff_context_id"] = payoff_context_id(context)
    return context


def context_from_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return build_payoff_context(
        edge_id=str(mapping.get("payoff_context_edge_id", mapping.get("edge_id"))),
        threshold=_finite_float(mapping.get("threshold", mapping.get("selected_threshold")), "threshold"),
        geometry_hash=str(mapping.get("geometry_hash", mapping.get("execution_geometry_hash"))),
        payoff_profile=profile_from_mapping(mapping),
        threshold_probability_column=str(
            mapping.get("threshold_probability_column", mapping.get("probability_column", ""))
        ),
    )


def validate_payoff_context_mapping(
    mapping: Mapping[str, Any], *, expected_edge_id: str | None = None,
    expected_threshold: float | None = None, expected_geometry_hash: str | None = None,
) -> dict[str, Any]:
    rebuilt = context_from_mapping(mapping)
    supplied = str(mapping.get("payoff_context_id", ""))
    if not supplied:
        raise V3SemanticContractError("Missing payoff_context_id.")
    if supplied != rebuilt["payoff_context_id"]:
        raise V3SemanticContractError("Threshold/payoff semantic identity mismatch.")
    for key, expected in (
        ("payoff_context_version", PAYOFF_CONTEXT_VERSION),
        ("gross_label_contract_version", GROSS_LABEL_CONTRACT_VERSION),
        ("execution_contract_version", EXECUTION_CONTRACT_VERSION),
        ("executed_target_contract_version", EXECUTED_TARGET_CONTRACT_VERSION),
        ("afml_sizing_method", AFML_SIZING_METHOD),
    ):
        if str(mapping.get(key, "")) != expected:
            raise V3SemanticContractError(f"{key} mismatch.")
    if expected_edge_id is not None and rebuilt["edge_id"] != str(expected_edge_id):
        raise V3SemanticContractError("payoff context belongs to another edge.")
    if expected_threshold is not None and not math.isclose(
        float(rebuilt["threshold"]), float(expected_threshold), rel_tol=0.0, abs_tol=1e-12
    ):
        raise V3SemanticContractError("payoff context threshold mismatch.")
    if expected_geometry_hash is not None and rebuilt["geometry_hash"] != str(expected_geometry_hash):
        raise V3SemanticContractError("payoff context geometry hash mismatch.")
    return rebuilt


def attach_payoff_context_columns(sweep: Any, *, edge_id: str, geometry_hash: str) -> Any:
    """A payoff context is only meaningful for a threshold whose empirical
    payoff profile is actually valid (build_payoff_context itself requires
    one). A threshold sweep is expected to contain some invalid-profile rows
    -- e.g. a high threshold with too few selected trades to have >= the
    minimum required wins/losses -- since choose_selected_threshold filters
    for payoff_profile_valid==True downstream anyway. Those rows simply get
    no payoff context (None placeholders) rather than crashing the whole
    sweep."""
    out = sweep.copy()
    columns = (
        "payoff_context_version", "payoff_context_id", "payoff_context_edge_id",
        "gross_label_contract_version", "execution_contract_version",
        "executed_target_contract_version", "threshold_probability_column", "geometry_hash",
    )
    if out.empty:
        for col in columns:
            if col not in out.columns:
                out[col] = []
        return out
    contexts = []
    for _, row in out.iterrows():
        if _bool_value(row.get("payoff_profile_valid")) is not True:
            contexts.append({
                "payoff_context_version": None, "payoff_context_id": None, "edge_id": None,
                "gross_label_contract_version": None, "execution_contract_version": None,
                "executed_target_contract_version": None, "threshold_probability_column": None,
                "geometry_hash": None,
            })
            continue
        contexts.append(build_payoff_context(
            edge_id=edge_id, threshold=float(row["threshold"]), geometry_hash=geometry_hash,
            payoff_profile=profile_from_mapping(row),
            threshold_probability_column=str(row.get("probability_column", "")),
        ))
    out["payoff_context_version"] = [c["payoff_context_version"] for c in contexts]
    out["payoff_context_id"] = [c["payoff_context_id"] for c in contexts]
    out["payoff_context_edge_id"] = [c["edge_id"] for c in contexts]
    out["gross_label_contract_version"] = [c["gross_label_contract_version"] for c in contexts]
    out["execution_contract_version"] = [c["execution_contract_version"] for c in contexts]
    out["executed_target_contract_version"] = [c["executed_target_contract_version"] for c in contexts]
    out["threshold_probability_column"] = [c["threshold_probability_column"] for c in contexts]
    out["geometry_hash"] = [c["geometry_hash"] for c in contexts]
    return out


def validate_payoff_context_frame(frame: Any, *, context: str = "frame") -> None:
    required = [
        "edge_id", "threshold", "payoff_context_version", "payoff_context_id",
        "payoff_context_edge_id", "gross_label_contract_version", "execution_contract_version",
        "executed_target_contract_version", "threshold_probability_column", "geometry_hash",
        "afml_sizing_method", *_PROFILE_KEYS,
    ]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise V3SemanticContractError(f"{context} missing payoff-context columns: {missing}")
    if frame.empty:
        raise V3SemanticContractError(f"{context} is empty.")
    for context_id, group in frame.groupby("payoff_context_id", dropna=False, sort=False):
        if str(context_id).strip() in {"", "nan", "None"}:
            raise V3SemanticContractError(f"{context} contains empty payoff_context_id.")
        first = group.iloc[0]
        validate_payoff_context_mapping(
            first, expected_edge_id=str(first["edge_id"]),
            expected_threshold=float(first["threshold"]),
            expected_geometry_hash=str(first["geometry_hash"]),
        )
        for col in (
            "edge_id", "threshold", "payoff_context_edge_id", "gross_label_contract_version",
            "execution_contract_version", "executed_target_contract_version",
            "threshold_probability_column", "geometry_hash", "afml_sizing_method",
            "payoff_profile_version", "payoff_profile_valid", "payoff_profile_invalid_reason",
            "payoff_trade_count", "payoff_win_count", "payoff_loss_count",
            "payoff_zero_count", "payoff_zero_rate", "min_wins_required", "min_losses_required",
            "empirical_avg_win_r", "empirical_avg_loss_r",
            "empirical_reward_risk_ratio", "empirical_break_even_probability",
        ):
            if group[col].astype(str).nunique(dropna=False) != 1:
                raise V3SemanticContractError(
                    f"{context}: payoff_context_id {context_id} has conflicting {col} values."
                )


def validate_gross_label_frame(frame: Any, *, context: str = "frame") -> None:
    import pandas as pd
    missing = [c for c in (GROSS_TARGET_COLUMN, GROSS_REALIZED_R_COLUMN) if c not in frame.columns]
    if missing:
        raise V3SemanticContractError(f"{context} missing gross-label source columns: {missing}")
    target = pd.to_numeric(frame[GROSS_TARGET_COLUMN], errors="coerce")
    realized = pd.to_numeric(frame[GROSS_REALIZED_R_COLUMN], errors="coerce")
    if target.isna().any() or realized.isna().any():
        raise V3SemanticContractError(f"{context} gross-label source contains NaN/non-numeric values.")
    if not target.astype(int).equals((realized > 0.0).astype(int)):
        raise V3SemanticContractError(
            f"{context}: meta_label violates {GROSS_LABEL_CONTRACT_VERSION}; "
            f"expected {GROSS_TARGET_DEFINITION}."
        )


def _require_equal(payload: Mapping[str, Any], key: str, expected: Any, context: str) -> None:
    if payload.get(key) != expected:
        raise V3SemanticContractError(
            f"{context}: {key} mismatch; expected {expected!r}, got {payload.get(key)!r}"
        )


def _require_hash(payload: Mapping[str, Any], key: str, path: Path, context: str) -> None:
    declared = str(payload.get(key, ""))
    actual = sha256_file(path)
    if not declared or declared != actual:
        raise V3SemanticContractError(
            f"{context}: stale/mismatched {key} for {path}. Rerun the owning lifecycle step."
        )



def validate_semantic_generation_snapshot(
    snapshot: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    context: str,
) -> dict[str, Any]:
    """Fail closed unless a stored semantic-generation snapshot equals current bytes."""
    if not isinstance(snapshot, Mapping):
        raise V3SemanticContractError(f"{context}: semantic generation snapshot is missing/invalid.")
    snapshot_dict = dict(snapshot)
    current_dict = dict(current)
    if snapshot_dict != current_dict:
        snapshot_keys = set(snapshot_dict)
        current_keys = set(current_dict)
        missing = sorted(current_keys - snapshot_keys)
        extra = sorted(snapshot_keys - current_keys)
        mismatched = sorted(
            key for key in snapshot_keys & current_keys
            if snapshot_dict.get(key) != current_dict.get(key)
        )
        raise V3SemanticContractError(
            f"{context}: stale semantic generation. "
            f"missing={missing} extra={extra} mismatched={mismatched}"
        )
    return current_dict


def validate_representative_payoff_context_handoff(
    ledger: Any,
    weights: Any,
    *,
    context: str,
) -> dict[str, Any]:
    """Bind representative ledger rows to the exact payoff context carried by HRP weights."""
    import pandas as pd

    required = ["representative_id", "payoff_context_version", "payoff_context_id"]
    for name, frame in (("ledger", ledger), ("weights", weights)):
        if frame is None or not hasattr(frame, "columns"):
            raise V3SemanticContractError(f"{context}: {name} is not DataFrame-like.")
        missing = [col for col in required if col not in frame.columns]
        if missing:
            raise V3SemanticContractError(f"{context}: {name} missing payoff-context columns: {missing}")
        if frame.empty:
            raise V3SemanticContractError(f"{context}: {name} is empty.")
        empty_id = frame["payoff_context_id"].astype(str).str.strip().isin({"", "nan", "None"})
        if empty_id.any():
            raise V3SemanticContractError(
                f"{context}: {name} contains {int(empty_id.sum())} empty payoff_context_id rows."
            )
        if not frame["payoff_context_version"].astype(str).eq(PAYOFF_CONTEXT_VERSION).all():
            raise V3SemanticContractError(f"{context}: {name} contains stale payoff_context_version rows.")

    weight_conflicts = weights.groupby("representative_id")["payoff_context_id"].nunique(dropna=False)
    if (weight_conflicts != 1).any():
        raise V3SemanticContractError(
            f"{context}: HRP weights contain conflicting payoff contexts: "
            f"{weight_conflicts[weight_conflicts != 1].to_dict()}"
        )
    ledger_conflicts = ledger.groupby("representative_id")["payoff_context_id"].nunique(dropna=False)
    if (ledger_conflicts != 1).any():
        raise V3SemanticContractError(
            f"{context}: representative ledger contains conflicting payoff contexts: "
            f"{ledger_conflicts[ledger_conflicts != 1].to_dict()}"
        )

    representative_ids = sorted(set(weights["representative_id"].astype(str)))
    ledger_ids = set(ledger["representative_id"].astype(str))
    missing_ledger = sorted(set(representative_ids) - ledger_ids)
    if missing_ledger:
        raise V3SemanticContractError(
            f"{context}: HRP representatives missing from ledger: {missing_ledger[:20]}"
        )

    for representative_id in representative_ids:
        w = weights[weights["representative_id"].astype(str).eq(representative_id)]
        l = ledger[ledger["representative_id"].astype(str).eq(representative_id)]
        weight_context_id = str(w["payoff_context_id"].iloc[0]).strip()
        ledger_context_id = str(l["payoff_context_id"].iloc[0]).strip()
        if weight_context_id != ledger_context_id:
            raise V3SemanticContractError(
                f"{context}: {representative_id} payoff_context_id mismatch "
                f"weights={weight_context_id} ledger={ledger_context_id}"
            )
        if "edge_id" in w.columns and "edge_id" in l.columns:
            w_edges = set(w["edge_id"].astype(str))
            l_edges = set(l["edge_id"].astype(str))
            if len(w_edges) != 1 or len(l_edges) != 1 or w_edges != l_edges:
                raise V3SemanticContractError(f"{context}: {representative_id} edge identity mismatch.")
        if "threshold" in w.columns and "threshold" in l.columns:
            wt = pd.to_numeric(w["threshold"], errors="coerce")
            lt = pd.to_numeric(l["threshold"], errors="coerce")
            if wt.isna().any() or lt.isna().any():
                raise V3SemanticContractError(f"{context}: {representative_id} threshold is non-numeric.")
            reference = float(wt.iloc[0])
            if not all(math.isclose(float(x), reference, rel_tol=0.0, abs_tol=1e-12) for x in lt):
                raise V3SemanticContractError(
                    f"{context}: {representative_id} threshold mismatch between HRP and ledger."
                )

    return {
        "status": "PASS",
        "payoff_context_version": PAYOFF_CONTEXT_VERSION,
        "representatives_checked": len(representative_ids),
    }


def validate_step01_generation(layout: Any) -> dict[str, Any]:
    manifest = _load_json_object(layout.feature_surgery_manifest_json)
    _require_equal(manifest, "edge_id", layout.edge_id, "Step01")
    _require_equal(manifest, "gross_label_contract_version", GROSS_LABEL_CONTRACT_VERSION, "Step01")
    _require_equal(manifest, "gross_target_column", GROSS_TARGET_COLUMN, "Step01")
    _require_equal(manifest, "gross_target_definition", GROSS_TARGET_DEFINITION, "Step01")
    _require_equal(manifest, "training_target_column", TARGET_COLUMN, "Step01")
    _require_equal(manifest, "training_eligibility_column", ELIGIBILITY_COLUMN, "Step01")
    _require_equal(manifest, "phase3a_step01_provenance_status", "PASS", "Step01")
    model_rows = layout.resolution_csv
    _require_hash(manifest, "model_rows_sha256", model_rows, "Step01")
    _require_hash(manifest, "feature_schema_sha256", layout.feature_schema_json, "Step01")
    _require_hash(manifest, "selected_features_sha256", layout.feature_surgery_selected_features_json, "Step01")
    return {
        "step01_feature_surgery_manifest_sha256": sha256_file(layout.feature_surgery_manifest_json),
        "step01_selected_features_sha256": sha256_file(layout.feature_surgery_selected_features_json),
        "step01_model_rows_sha256": sha256_file(model_rows),
        "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
    }


def validate_step02_generation(layout: Any) -> dict[str, Any]:
    upstream = validate_step01_generation(layout)
    manifest = _load_json_object(layout.model_training_manifest_json)
    _require_equal(manifest, "edge_id", layout.edge_id, "Step02")
    _require_equal(manifest, "gross_label_contract_version", GROSS_LABEL_CONTRACT_VERSION, "Step02")
    _require_equal(manifest, "gross_target_column", GROSS_TARGET_COLUMN, "Step02")
    _require_equal(manifest, "gross_target_definition", GROSS_TARGET_DEFINITION, "Step02")
    _require_equal(manifest, "training_target_column", TARGET_COLUMN, "Step02")
    _require_equal(manifest, "training_eligibility_column", ELIGIBILITY_COLUMN, "Step02")
    _require_equal(
        manifest, "step01_feature_surgery_manifest_sha256",
        upstream["step01_feature_surgery_manifest_sha256"], "Step02"
    )
    model_rows = layout.resolution_csv
    _require_hash(manifest, "model_rows_sha256", model_rows, "Step02")
    _require_hash(manifest, "selected_features_sha256", layout.feature_surgery_selected_features_json, "Step02")
    _require_hash(manifest, "feature_schema_sha256", layout.feature_schema_json, "Step02")
    _require_hash(manifest, "best_params_sha256", layout.model_best_params_json, "Step02")
    _require_hash(manifest, "model_artifact_sha256", layout.model_pkl, "Step02")
    return {
        **upstream,
        "step02_training_manifest_sha256": sha256_file(layout.model_training_manifest_json),
        "step02_model_rows_sha256": sha256_file(model_rows),
        "step02_best_params_sha256": sha256_file(layout.model_best_params_json),
        "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
    }


def validate_step03_generation(layout: Any) -> dict[str, Any]:
    upstream = validate_step02_generation(layout)
    manifest = _load_json_object(layout.oof_manifest_json)
    _require_equal(manifest, "edge_id", layout.edge_id, "Step03")
    _require_equal(manifest, "gross_label_contract_version", GROSS_LABEL_CONTRACT_VERSION, "Step03")
    _require_equal(manifest, "gross_target_column", GROSS_TARGET_COLUMN, "Step03")
    _require_equal(manifest, "gross_target_definition", GROSS_TARGET_DEFINITION, "Step03")
    _require_equal(manifest, "training_target_column", TARGET_COLUMN, "Step03")
    _require_equal(manifest, "training_eligibility_column", ELIGIBILITY_COLUMN, "Step03")
    _require_equal(manifest, "step02_training_manifest_sha256",
                   upstream["step02_training_manifest_sha256"], "Step03")
    _require_equal(manifest, "step01_feature_surgery_manifest_sha256",
                   upstream["step01_feature_surgery_manifest_sha256"], "Step03")
    model_rows = layout.resolution_csv
    _require_hash(manifest, "model_rows_sha256", model_rows, "Step03")
    _require_hash(manifest, "selected_features_sha256", layout.feature_surgery_selected_features_json, "Step03")
    _require_hash(manifest, "feature_schema_sha256", layout.feature_schema_json, "Step03")
    _require_hash(manifest, "best_params_sha256", layout.model_best_params_json, "Step03")
    _require_hash(manifest, "oof_predictions_sha256", layout.oof_predictions_csv, "Step03")
    return {
        **upstream,
        "step03_oof_manifest_sha256": sha256_file(layout.oof_manifest_json),
        "step03_oof_predictions_sha256": sha256_file(layout.oof_predictions_csv),
    }


# Step03A (shared/execution/oof_exact_execution.py-based exact-execution audit
# manifest) is retired: Task 2A's resolution_csv already carries exact
# Bid/Ask executable outcomes end-to-end, so Step03's OOF predictions are
# themselves generated directly against that executable target. Geometry
# identity is now verified against the edge's live edge.yaml (the same
# geometry_hash Task 2A/Step04 compute), not a Step03A manifest file.


def payoff_context_from_threshold_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    context = payload.get("payoff_context")
    if not isinstance(context, Mapping):
        raise V3SemanticContractError("Selected threshold artifact missing payoff_context object.")
    supplied_id = str(payload.get("payoff_context_id", ""))
    if supplied_id and supplied_id != str(context.get("payoff_context_id", "")):
        raise V3SemanticContractError("Selected threshold payoff_context_id disagrees with context object.")
    return validate_payoff_context_mapping(context)


def validate_research_generation_chain(layout: Any) -> dict[str, Any]:
    upstream = validate_step03_generation(layout)
    geometry = load_trade_geometry(
        layout.edge_yaml, expected_edge_id=layout.edge_id, expected_side_name=layout.side_name,
    )
    geometry_hash = str(geometry["geometry_hash"])

    calibrator = _load_json_object(layout.calibrator_json)
    _require_equal(calibrator, "edge_id", layout.edge_id, "Step04 calibrator")
    _require_equal(calibrator, "gross_label_contract_version", GROSS_LABEL_CONTRACT_VERSION, "Step04 calibrator")
    _require_equal(calibrator, "target_contract_version", EXECUTED_TARGET_CONTRACT_VERSION, "Step04 calibrator")
    _require_equal(calibrator, "execution_contract_version", EXECUTION_CONTRACT_VERSION, "Step04 calibrator")

    threshold = _load_json_object(layout.selected_threshold_json)
    _require_equal(threshold, "edge_id", layout.edge_id, "Step04 threshold")
    _require_equal(threshold, "gross_label_contract_version", GROSS_LABEL_CONTRACT_VERSION, "Step04 threshold")
    _require_equal(threshold, "calibration_target_contract_version", EXECUTED_TARGET_CONTRACT_VERSION, "Step04 threshold")
    _require_equal(threshold, "execution_contract_version", EXECUTION_CONTRACT_VERSION, "Step04 threshold")
    validate_semantic_generation_snapshot(
        threshold.get("upstream_generation"), upstream,
        context="Step04 threshold upstream_generation",
    )

    provenance = threshold.get("payoff_profile_provenance")
    if not isinstance(provenance, Mapping):
        raise V3SemanticContractError("Step04 threshold missing payoff_profile_provenance.")
    _require_equal(provenance, "oof_predictions_sha256", upstream["step03_oof_predictions_sha256"], "Step04 threshold payoff provenance")
    _require_equal(provenance, "execution_contract_version", EXECUTION_CONTRACT_VERSION, "Step04 threshold payoff provenance")
    _require_equal(provenance, "geometry_hash", geometry_hash, "Step04 threshold payoff provenance")
    _require_equal(provenance, "probability_column", THRESHOLD_PROBABILITY_COLUMN, "Step04 threshold payoff provenance")

    context = payoff_context_from_threshold_payload(threshold)
    validate_payoff_context_mapping(
        context, expected_edge_id=layout.edge_id,
        expected_threshold=float(context["threshold"]),
        expected_geometry_hash=geometry_hash,
    )
    provenance_threshold = _finite_float(provenance.get("threshold"), "Step04 payoff provenance threshold")
    if not math.isclose(provenance_threshold, float(context["threshold"]), rel_tol=0.0, abs_tol=1e-12):
        raise V3SemanticContractError("Step04 threshold payoff provenance threshold mismatch.")

    calibration_manifest = _load_json_object(layout.calibration_manifest_json)
    _require_equal(calibration_manifest, "edge_id", layout.edge_id, "Step04 calibration manifest")
    _require_equal(calibration_manifest, "execution_contract_version", EXECUTION_CONTRACT_VERSION, "Step04 calibration manifest")
    _require_equal(calibration_manifest, "calibration_target_contract_version", EXECUTED_TARGET_CONTRACT_VERSION, "Step04 calibration manifest")
    validate_semantic_generation_snapshot(
        calibration_manifest.get("upstream_generation"), upstream,
        context="Step04 calibration manifest upstream_generation",
    )
    if calibration_manifest.get("live_sigmoid_calibrator") != calibrator:
        raise V3SemanticContractError(
            "Step04 calibrator bytes do not match the calibrator embedded in the current calibration manifest."
        )
    embedded_threshold = calibration_manifest.get("selected_threshold")
    if not isinstance(embedded_threshold, Mapping):
        raise V3SemanticContractError("Step04 calibration manifest missing embedded selected_threshold.")
    validate_semantic_generation_snapshot(
        embedded_threshold.get("upstream_generation"), upstream,
        context="Step04 calibration manifest embedded threshold upstream_generation",
    )
    embedded_context = payoff_context_from_threshold_payload(embedded_threshold)
    if str(embedded_context["payoff_context_id"]) != str(context["payoff_context_id"]):
        raise V3SemanticContractError(
            "Step04 selected-threshold semantic identity differs from the threshold embedded in the current calibration manifest."
        )
    if str(calibration_manifest.get("payoff_context_id", "")) != str(context["payoff_context_id"]):
        raise V3SemanticContractError("Step04 calibration manifest payoff_context_id mismatch.")

    return {
        **upstream,
        "geometry_hash": geometry_hash,
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "executed_target_contract_version": EXECUTED_TARGET_CONTRACT_VERSION,
        "calibrator_sha256": sha256_file(layout.calibrator_json),
        "selected_threshold_sha256": sha256_file(layout.selected_threshold_json),
        "step04_calibration_manifest_sha256": sha256_file(layout.calibration_manifest_json),
        "payoff_context": context,
        "payoff_context_id": context["payoff_context_id"],
        "payoff_context_version": PAYOFF_CONTEXT_VERSION,
        "payoff_profile_version": PAYOFF_PROFILE_VERSION,
        "afml_sizing_method": AFML_SIZING_METHOD,
    }
