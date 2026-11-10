from __future__ import annotations

"""Shared, fail-closed MT5 broker-server clock contract.

MT5 exposes broker-server wall-clock values in ``tick.time``/``time_msc``.
Those values are not necessarily UTC.  The dashboard is the sole clock
authority: it measures one account-level offset from active symbols and
atomically publishes this contract.  The executor validates the contract and
normalises every tick before applying future/stale guards.
"""

import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd


CLOCK_CONTRACT_SCHEMA = "v2_account_broker_clock_contract_v1"
CLOCK_CONTRACT_VERSION = "2026-08-12_authoritative_broker_clock_v1"
DEFAULT_MAX_OFFSET_SECONDS = 14 * 3600
DEFAULT_MAX_PUBLISHED_AGE_SECONDS = 900.0
DEFAULT_MAX_MEASUREMENT_AGE_SECONDS = 900.0
DEFAULT_FUTURE_TOLERANCE_SECONDS = 5.0
DEFAULT_MAX_SAMPLE_RESIDUAL_SECONDS = 180.0

CLOCK_CONTRACT_RETRY = "CLOCK_CONTRACT_RETRY"
CLOCK_CONTRACT_INVALID = "CLOCK_CONTRACT_INVALID"
GENUINE_FUTURE_TICK = "GENUINE_FUTURE_TICK"
STALE_TICK = "STALE_TICK"


class BrokerClockContractError(RuntimeError):
    """Raised when broker time cannot be normalised safely."""


def utc_now_ts() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def ensure_utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def raw_tick_timestamp(tick: Any) -> pd.Timestamp | None:
    time_msc = getattr(tick, "time_msc", None)
    try:
        if time_msc is not None and int(time_msc) > 0:
            return pd.Timestamp(int(time_msc), unit="ms", tz="UTC")
    except Exception:
        pass
    seconds = getattr(tick, "time", None)
    try:
        if seconds is not None and int(seconds) > 0:
            return pd.Timestamp(int(seconds), unit="s", tz="UTC")
    except Exception:
        pass
    return None


def infer_sample(
    *,
    asset: str,
    symbol: str,
    raw_timestamp: Any,
    observed_at: Any | None = None,
    max_offset_seconds: int = DEFAULT_MAX_OFFSET_SECONDS,
) -> dict[str, Any]:
    observed = ensure_utc(observed_at if observed_at is not None else utc_now_ts())
    raw = ensure_utc(raw_timestamp)
    delta_seconds = float((raw - observed).total_seconds())
    inferred = int(round(delta_seconds / 3600.0) * 3600)
    residual = abs(delta_seconds - float(inferred))
    valid = abs(inferred) <= int(max_offset_seconds) and math.isfinite(residual)
    return {
        "asset": str(asset).upper(),
        "symbol": str(symbol),
        "raw_timestamp": raw.isoformat(),
        "observed_at": observed.isoformat(),
        "inferred_offset_seconds": inferred,
        "residual_seconds": residual,
        "within_offset_range": bool(valid),
    }


def resolve_consensus(
    samples: Iterable[Mapping[str, Any]],
    *,
    max_residual_seconds: float,
    previous_offset_seconds: int | None = None,
) -> dict[str, Any]:
    """Resolve one account clock while excluding stale/closed-symbol outliers.

    A two-or-more-symbol mode is authoritative.  When only one active symbol is
    available (for example BTC over a market weekend), a fresh single-symbol
    sample is allowed and explicitly labelled.  Tied conflicting modes fail.
    """
    candidates = [
        dict(sample)
        for sample in samples
        if bool(sample.get("within_offset_range", True))
        and float(sample.get("residual_seconds", float("inf")))
        <= float(max_residual_seconds)
    ]
    if not candidates:
        raise BrokerClockContractError("clock_contract_no_fresh_symbol_samples")
    grouped: dict[int, list[dict[str, Any]]] = {}
    for sample in candidates:
        grouped.setdefault(int(sample["inferred_offset_seconds"]), []).append(sample)
    ranking = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    best_count = len(ranking[0][1])
    tied = [item for item in ranking if len(item[1]) == best_count]
    if len(tied) > 1:
        raise BrokerClockContractError(
            "clock_contract_contradictory_symbol_samples:"
            + json.dumps({offset: len(rows) for offset, rows in ranking}, sort_keys=True)
        )
    offset, accepted = tied[0]
    excluded = [sample for sample in candidates if int(sample["inferred_offset_seconds"]) != offset]
    status = "PASS" if len(accepted) >= 2 else "SINGLE_ACTIVE_SYMBOL"
    return {
        "status": status,
        "offset_seconds": int(offset),
        "accepted_samples": accepted,
        "excluded_samples": excluded,
        "fresh_sample_count": len(candidates),
        "consensus_sample_count": len(accepted),
        "previous_offset_seconds": previous_offset_seconds,
        "offset_transition_detected": bool(
            previous_offset_seconds is not None
            and int(previous_offset_seconds) != int(offset)
        ),
    }


def _generation_id(payload: Mapping[str, Any]) -> str:
    identity = {
        "schema_version": payload.get("schema_version"),
        "offset_seconds": payload.get("offset_seconds"),
        "locked": payload.get("locked"),
        "published_at": payload.get("published_at"),
        "measurement_time": payload.get("measurement_time"),
        "source": payload.get("source"),
        "selected_symbols": sorted(payload.get("selected_symbols") or []),
        "sample_symbols": sorted(
            str(row.get("symbol")) for row in (payload.get("accepted_samples") or [])
        ),
        "accepted_samples": payload.get("accepted_samples") or [],
        "excluded_samples": payload.get("excluded_samples") or [],
        "max_sample_residual_seconds": payload.get("max_sample_residual_seconds"),
        "terminal_session_identity": payload.get("terminal_session_identity"),
        "broker_server_day": payload.get("broker_server_day"),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def build_contract(
    *,
    offset_seconds: int,
    source: str,
    selected_symbols: Iterable[str],
    accepted_samples: Iterable[Mapping[str, Any]],
    excluded_samples: Iterable[Mapping[str, Any]] = (),
    consensus_status: str = "PASS",
    locked: bool = True,
    published_at: Any | None = None,
    measurement_time: Any | None = None,
    max_sample_residual_seconds: float = DEFAULT_MAX_SAMPLE_RESIDUAL_SECONDS,
    terminal_session_identity: str | None = None,
    broker_server_day: str | None = None,
) -> dict[str, Any]:
    published = ensure_utc(published_at if published_at is not None else utc_now_ts())
    accepted = [dict(row) for row in accepted_samples]
    measurement = ensure_utc(
        measurement_time
        if measurement_time is not None
        else max(
            (ensure_utc(row.get("observed_at")) for row in accepted if row.get("observed_at")),
            default=published,
        )
    )
    payload: dict[str, Any] = {
        "schema_version": CLOCK_CONTRACT_SCHEMA,
        "contract_version": CLOCK_CONTRACT_VERSION,
        "published_at": published.isoformat(),
        "measurement_time": measurement.isoformat(),
        "offset_seconds": int(offset_seconds),
        "offset_hours": float(offset_seconds) / 3600.0,
        "locked": bool(locked),
        "source": str(source),
        "consensus_status": str(consensus_status),
        "selected_symbols": sorted({str(value) for value in selected_symbols if str(value)}),
        "accepted_samples": accepted,
        "excluded_samples": [dict(row) for row in excluded_samples],
        "max_sample_residual_seconds": float(max_sample_residual_seconds),
        "terminal_session_identity": terminal_session_identity,
        "broker_server_day": broker_server_day,
    }
    payload["generation_id"] = _generation_id(payload)
    return payload


def validate_contract(
    payload: Mapping[str, Any],
    *,
    now: Any | None = None,
    required_symbol: str | None = None,
    max_offset_seconds: int = DEFAULT_MAX_OFFSET_SECONDS,
    max_published_age_seconds: float = DEFAULT_MAX_PUBLISHED_AGE_SECONDS,
    max_measurement_age_seconds: float = DEFAULT_MAX_MEASUREMENT_AGE_SECONDS,
    future_tolerance_seconds: float = DEFAULT_FUTURE_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise BrokerClockContractError("clock_contract_not_mapping")
    if payload.get("schema_version") != CLOCK_CONTRACT_SCHEMA:
        raise BrokerClockContractError("clock_contract_schema_mismatch")
    if payload.get("locked") is not True:
        raise BrokerClockContractError("clock_contract_not_locked")
    if str(payload.get("consensus_status")) not in {"PASS", "SINGLE_ACTIVE_SYMBOL"}:
        raise BrokerClockContractError("clock_contract_consensus_invalid")
    try:
        offset = int(payload.get("offset_seconds"))
    except Exception as exc:
        raise BrokerClockContractError("clock_contract_offset_invalid") from exc
    if abs(offset) > int(max_offset_seconds):
        raise BrokerClockContractError("clock_contract_offset_out_of_range")
    expected_generation = _generation_id(payload)
    if str(payload.get("generation_id") or "") != expected_generation:
        raise BrokerClockContractError("clock_contract_generation_mismatch")
    current = ensure_utc(now if now is not None else utc_now_ts())
    try:
        published = ensure_utc(payload.get("published_at"))
        measured = ensure_utc(payload.get("measurement_time"))
    except Exception as exc:
        raise BrokerClockContractError("clock_contract_timestamp_invalid") from exc
    published_age = float((current - published).total_seconds())
    measurement_age = float((current - measured).total_seconds())
    if published_age < -float(future_tolerance_seconds):
        raise BrokerClockContractError("clock_contract_published_in_future")
    if measurement_age < -float(future_tolerance_seconds):
        raise BrokerClockContractError("clock_contract_measurement_in_future")
    if published_age > float(max_published_age_seconds):
        raise BrokerClockContractError("clock_contract_stale_publication")
    if measurement_age > float(max_measurement_age_seconds):
        raise BrokerClockContractError("clock_contract_stale_measurement")
    selected = {str(value) for value in (payload.get("selected_symbols") or [])}
    if required_symbol and str(required_symbol) not in selected:
        raise BrokerClockContractError(f"clock_contract_symbol_not_covered:{required_symbol}")
    accepted = list(payload.get("accepted_samples") or [])
    if not accepted:
        raise BrokerClockContractError("clock_contract_has_no_accepted_samples")
    contradictions = {
        int(row.get("inferred_offset_seconds"))
        for row in accepted
        if row.get("inferred_offset_seconds") is not None
    }
    if contradictions != {offset}:
        raise BrokerClockContractError("clock_contract_accepted_samples_disagree")
    consensus_status = str(payload.get("consensus_status"))
    if consensus_status == "PASS" and len(accepted) < 2:
        raise BrokerClockContractError("clock_contract_multisymbol_consensus_missing")
    if consensus_status == "SINGLE_ACTIVE_SYMBOL" and len(accepted) != 1:
        raise BrokerClockContractError("clock_contract_single_symbol_cardinality_invalid")
    accepted_symbols = [str(row.get("symbol") or "") for row in accepted]
    if not all(accepted_symbols) or len(set(accepted_symbols)) != len(accepted_symbols):
        raise BrokerClockContractError("clock_contract_sample_symbols_invalid")
    if not set(accepted_symbols).issubset(selected):
        raise BrokerClockContractError("clock_contract_samples_outside_selected_symbols")
    try:
        maximum_residual = float(payload.get("max_sample_residual_seconds"))
    except Exception as exc:
        raise BrokerClockContractError("clock_contract_residual_limit_invalid") from exc
    if not math.isfinite(maximum_residual) or maximum_residual <= 0:
        raise BrokerClockContractError("clock_contract_residual_limit_invalid")
    for row in accepted:
        try:
            residual = float(row.get("residual_seconds"))
            observed = ensure_utc(row.get("observed_at"))
        except Exception as exc:
            raise BrokerClockContractError("clock_contract_sample_invalid") from exc
        if not math.isfinite(residual) or residual > maximum_residual:
            raise BrokerClockContractError("clock_contract_sample_residual_exceeded")
        sample_age = float((current - observed).total_seconds())
        if sample_age < -float(future_tolerance_seconds):
            raise BrokerClockContractError("clock_contract_sample_observed_in_future")
        if sample_age > float(max_measurement_age_seconds):
            raise BrokerClockContractError("clock_contract_sample_stale")
    return {
        "status": "PASS",
        "offset_seconds": offset,
        "published_at": published.isoformat(),
        "measurement_time": measured.isoformat(),
        "published_age_seconds": max(published_age, 0.0),
        "measurement_age_seconds": max(measurement_age, 0.0),
        "generation_id": expected_generation,
        "source": payload.get("source"),
        "consensus_status": payload.get("consensus_status"),
        "terminal_session_identity": payload.get("terminal_session_identity"),
        "broker_server_day": payload.get("broker_server_day"),
    }


def classify_contract_error(error: Exception | str) -> str:
    """Separate a temporarily unavailable authority from an invalid contract."""
    message = str(error)
    retry_tokens = (
        "missing",
        "unreadable",
        "stale_publication",
        "stale_measurement",
        "sample_stale",
    )
    return (
        CLOCK_CONTRACT_RETRY
        if any(token in message for token in retry_tokens)
        else CLOCK_CONTRACT_INVALID
    )


def normalise_broker_timestamp(raw_timestamp: Any, contract: Mapping[str, Any]) -> pd.Timestamp:
    raw = ensure_utc(raw_timestamp)
    return raw - pd.Timedelta(seconds=int(contract["offset_seconds"]))


def read_contract(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BrokerClockContractError("clock_contract_missing") from exc
    except Exception as exc:
        raise BrokerClockContractError(f"clock_contract_unreadable:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise BrokerClockContractError("clock_contract_not_object")
    return payload


def atomic_write_contract(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def self_test() -> dict[str, Any]:
    now = pd.Timestamp("2026-08-12T08:00:00Z")
    samples = [
        infer_sample(asset="A", symbol="A.x", raw_timestamp=now + pd.Timedelta(hours=3), observed_at=now),
        infer_sample(asset="B", symbol="B.x", raw_timestamp=now + pd.Timedelta(hours=3, seconds=1), observed_at=now),
    ]
    consensus = resolve_consensus(samples, max_residual_seconds=5.0)
    contract = build_contract(
        offset_seconds=consensus["offset_seconds"],
        source="self_test",
        selected_symbols=["A.x", "B.x"],
        accepted_samples=consensus["accepted_samples"],
        excluded_samples=consensus["excluded_samples"],
        consensus_status=consensus["status"],
        published_at=now,
        measurement_time=now,
    )
    validated = validate_contract(contract, now=now, required_symbol="A.x")
    normalised = normalise_broker_timestamp(now + pd.Timedelta(hours=3), validated)
    tests: dict[str, bool] = {
        "utc_plus_three_consensus": consensus["offset_seconds"] == 10800,
        "normalisation_to_utc": normalised == now,
        "generation_verified": validated["generation_id"] == contract["generation_id"],
    }

    for hours in (0, 2, 3):
        rows = [
            infer_sample(
                asset=f"T{index}",
                symbol=f"T{index}.x",
                raw_timestamp=now + pd.Timedelta(hours=hours, seconds=index),
                observed_at=now,
            )
            for index in range(2)
        ]
        tests[f"utc_plus_{hours}_supported"] = (
            resolve_consensus(rows, max_residual_seconds=5.0)["offset_seconds"]
            == hours * 3600
        )

    transitioned = resolve_consensus(
        [
            infer_sample(asset="A", symbol="A.x", raw_timestamp=now + pd.Timedelta(hours=3), observed_at=now),
            infer_sample(asset="B", symbol="B.x", raw_timestamp=now + pd.Timedelta(hours=3), observed_at=now),
        ],
        max_residual_seconds=5.0,
        previous_offset_seconds=7200,
    )
    tests["offset_transition_uses_fresh_consensus"] = transitioned["offset_seconds"] == 10800

    stale = {**contract, "published_at": (now - pd.Timedelta(hours=1)).isoformat()}
    stale["generation_id"] = _generation_id(stale)
    failures: list[tuple[dict[str, Any], str]] = [
        ({**contract, "locked": False}, "clock_contract_not_locked"),
        (stale, "clock_contract_stale_publication"),
        ({**contract, "generation_id": "tampered"}, "clock_contract_generation_mismatch"),
    ]
    for candidate, expected in failures:
        try:
            validate_contract(candidate, now=now)
            tests[expected] = False
        except BrokerClockContractError as exc:
            tests[expected] = expected in str(exc)
    try:
        resolve_consensus([], max_residual_seconds=5.0)
        tests["missing_samples_fail_closed"] = False
    except BrokerClockContractError:
        tests["missing_samples_fail_closed"] = True
    contradictory = [
        infer_sample(asset="A", symbol="A.x", raw_timestamp=now + pd.Timedelta(hours=2), observed_at=now),
        infer_sample(asset="B", symbol="B.x", raw_timestamp=now + pd.Timedelta(hours=3), observed_at=now),
    ]
    try:
        resolve_consensus(contradictory, max_residual_seconds=5.0)
        tests["contradictory_samples_fail_closed"] = False
    except BrokerClockContractError:
        tests["contradictory_samples_fail_closed"] = True
    future_age = float(
        (now - normalise_broker_timestamp(now + pd.Timedelta(hours=3, seconds=60), validated)).total_seconds()
    )
    stale_age = float(
        (now - normalise_broker_timestamp(now + pd.Timedelta(hours=3, seconds=-60), validated)).total_seconds()
    )
    tests["genuine_future_tick_remains_future"] = future_age < -5.0
    tests["genuine_stale_tick_remains_stale"] = stale_age > 30.0
    # These nine residuals reproduce the captured failure shape: raw broker
    # timestamps appeared almost three hours in the future before the locked
    # UTC+3 contract was applied. Replay is deliberately guard-only; it cannot
    # submit, persist, or otherwise consume a live trading candidate.
    captured_three_hour_residuals = (
        10799.922,
        10799.801,
        10799.644,
        10799.512,
        10799.301,
        10799.094,
        10798.877,
        10798.744,
        10798.621,
    )
    replay_ages = [
        float(
            (
                now
                - normalise_broker_timestamp(
                    now + pd.Timedelta(seconds=residual),
                    validated,
                )
            ).total_seconds()
        )
        for residual in captured_three_hour_residuals
    ]
    tests["nine_captured_signals_advance_beyond_clock_guard"] = (
        len(replay_ages) == 9
        and all(-5.0 <= age <= 30.0 for age in replay_ages)
    )
    failed = [name for name, passed in tests.items() if not passed]
    return {"status": "PASS" if not failed else "FAIL", "tests": tests, "failed": failed}


__all__ = [
    "BrokerClockContractError",
    "CLOCK_CONTRACT_SCHEMA",
    "CLOCK_CONTRACT_VERSION",
    "CLOCK_CONTRACT_RETRY",
    "CLOCK_CONTRACT_INVALID",
    "GENUINE_FUTURE_TICK",
    "STALE_TICK",
    "atomic_write_contract",
    "build_contract",
    "classify_contract_error",
    "ensure_utc",
    "infer_sample",
    "normalise_broker_timestamp",
    "raw_tick_timestamp",
    "read_contract",
    "resolve_consensus",
    "self_test",
    "validate_contract",
]
