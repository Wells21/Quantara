"""Reusable primitives for verified append-only edge-runtime continuation.

The module does not implement any trading feature or event formula.  It only
selects bounded replay windows, tracks append-only decisions, and provides
small serialisable profiles used by :mod:`shared.edges.live_stateful_runtime`.
The unchanged edge ``build_features`` and ``generate_candidate_events``
functions remain the numerical authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso


@dataclass(frozen=True)
class ReplayProfile:
    """Conservative replay policy for one edge family.

    ``feature_rows`` and ``event_rows`` are attempted from smallest to largest.
    The complete available frame is always the final fail-closed fallback in the
    caller.  ``event_boundary_guard_rows`` excludes the unstable left edge of a
    bounded event replay from parity certification.
    """

    feature_rows: tuple[int, ...]
    event_rows: tuple[int, ...]
    feature_boundary_guard_rows: int
    event_boundary_guard_rows: int
    decision_history_rows: int
    minimum_feature_overlap_rows: int
    minimum_event_decision_overlap_rows: int
    retain_full_feature_history: bool = False
    force_full_event_replay: bool = False


DEFAULT_PROFILE = ReplayProfile(
    feature_rows=(768, 1200, 1800, 3000),
    event_rows=(900, 1400, 2200, 3500),
    feature_boundary_guard_rows=250,
    event_boundary_guard_rows=300,
    decision_history_rows=700,
    minimum_feature_overlap_rows=160,
    minimum_event_decision_overlap_rows=160,
)


# These values are deliberately conservative.  They are not mathematical
# formulas; they only control how much authoritative history is replayed before
# the result is compared against a trusted checkpoint.
_FAMILY_PROFILES: dict[str, ReplayProfile] = {
    "trend": ReplayProfile(
        feature_rows=(900, 1500, 2400, 3600),
        event_rows=(1500, 2400, 3600, 5000),
        feature_boundary_guard_rows=500,
        event_boundary_guard_rows=700,
        decision_history_rows=1000,
        minimum_feature_overlap_rows=200,
        minimum_event_decision_overlap_rows=240,
    ),
    "trendpullbackcontinuation": ReplayProfile(
        feature_rows=(900, 1400, 2200, 3200),
        event_rows=(900, 1400, 2200, 3200),
        feature_boundary_guard_rows=250,
        event_boundary_guard_rows=300,
        decision_history_rows=700,
        minimum_feature_overlap_rows=180,
        minimum_event_decision_overlap_rows=180,
    ),
    "timeregime": ReplayProfile(
        feature_rows=(1200, 2000, 2800, 4200),
        event_rows=(1800, 2600, 3800, 5200),
        feature_boundary_guard_rows=500,
        event_boundary_guard_rows=1500,
        decision_history_rows=1200,
        minimum_feature_overlap_rows=220,
        minimum_event_decision_overlap_rows=240,
    ),
    "rangestructure": ReplayProfile(
        feature_rows=(800, 1200, 1800, 2800),
        event_rows=(800, 1200, 1800, 2800),
        feature_boundary_guard_rows=220,
        event_boundary_guard_rows=250,
        decision_history_rows=700,
        minimum_feature_overlap_rows=160,
        minimum_event_decision_overlap_rows=160,
    ),
    "volatilitycarry": ReplayProfile(
        feature_rows=(800, 1200, 1800, 2800),
        event_rows=(900, 1400, 2200, 3200),
        feature_boundary_guard_rows=250,
        event_boundary_guard_rows=350,
        decision_history_rows=700,
        minimum_feature_overlap_rows=160,
        minimum_event_decision_overlap_rows=160,
    ),
    "volatilityregime": ReplayProfile(
        feature_rows=(700, 1000, 1500, 2400),
        event_rows=(700, 1000, 1500, 2400),
        feature_boundary_guard_rows=200,
        event_boundary_guard_rows=220,
        decision_history_rows=600,
        minimum_feature_overlap_rows=150,
        minimum_event_decision_overlap_rows=150,
    ),
    "reversal": ReplayProfile(
        feature_rows=(900, 1400, 2200, 3200),
        event_rows=(900, 1400, 2200, 3200),
        feature_boundary_guard_rows=250,
        event_boundary_guard_rows=300,
        decision_history_rows=700,
        minimum_feature_overlap_rows=180,
        minimum_event_decision_overlap_rows=180,
    ),
    "compressionbreakoutcontinuation": ReplayProfile(
        feature_rows=(900, 1400, 2200, 3200),
        event_rows=(1000, 1600, 2400, 3600),
        feature_boundary_guard_rows=250,
        event_boundary_guard_rows=400,
        decision_history_rows=800,
        minimum_feature_overlap_rows=180,
        minimum_event_decision_overlap_rows=180,
    ),
    "downsidejumpcontinuation": ReplayProfile(
        feature_rows=(800, 1200, 1800, 2800),
        event_rows=(800, 1200, 1800, 2800),
        feature_boundary_guard_rows=220,
        event_boundary_guard_rows=250,
        decision_history_rows=700,
        minimum_feature_overlap_rows=160,
        minimum_event_decision_overlap_rows=160,
    ),
    "downsidevolatilitycascade": ReplayProfile(
        feature_rows=(800, 1200, 1800, 2800),
        event_rows=(800, 1200, 1800, 2800),
        feature_boundary_guard_rows=220,
        event_boundary_guard_rows=250,
        decision_history_rows=700,
        minimum_feature_overlap_rows=160,
        minimum_event_decision_overlap_rows=160,
    ),
    "breakdownretestfailure": ReplayProfile(
        feature_rows=(900, 1400, 2200, 3200),
        event_rows=(1000, 1600, 2400, 3600),
        feature_boundary_guard_rows=250,
        event_boundary_guard_rows=350,
        decision_history_rows=800,
        minimum_feature_overlap_rows=180,
        minimum_event_decision_overlap_rows=180,
    ),
    "signentropycollapse": ReplayProfile(
        feature_rows=(1000, 1600, 2400, 3600),
        event_rows=(1300, 2000, 3000, 4500),
        feature_boundary_guard_rows=400,
        event_boundary_guard_rows=700,
        decision_history_rows=1000,
        minimum_feature_overlap_rows=200,
        minimum_event_decision_overlap_rows=220,
    ),
    "dailyrangecompletionfade": ReplayProfile(
        feature_rows=(1600, 2600, 4000, 5500),
        event_rows=(2600, 4000, 5500),
        feature_boundary_guard_rows=500,
        event_boundary_guard_rows=2200,
        decision_history_rows=1400,
        minimum_feature_overlap_rows=240,
        minimum_event_decision_overlap_rows=260,
        retain_full_feature_history=False,
    ),
    # The hazard probability table is cumulative over resolved historical
    # observations.  Until that edge supplies a native state machine, its event
    # generator must retain the complete available feature history.  Its feature
    # builder can still use verified suffix continuation.
    "drawdownhazardcontinuation": ReplayProfile(
        feature_rows=(1200, 2000, 3200, 4800),
        event_rows=(),
        feature_boundary_guard_rows=500,
        event_boundary_guard_rows=0,
        decision_history_rows=1200,
        minimum_feature_overlap_rows=220,
        minimum_event_decision_overlap_rows=0,
        retain_full_feature_history=True,
        force_full_event_replay=True,
    ),
    "absorption": ReplayProfile(
        feature_rows=(700, 1000, 1500, 2400),
        event_rows=(700, 1000, 1500, 2400),
        feature_boundary_guard_rows=200,
        event_boundary_guard_rows=220,
        decision_history_rows=600,
        minimum_feature_overlap_rows=150,
        minimum_event_decision_overlap_rows=150,
    ),
}


def _positive_ints(values: Iterable[Any]) -> tuple[int, ...]:
    output: list[int] = []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in output:
            output.append(number)
    return tuple(sorted(output))


def runtime_replay_profile(runtime: Any, settings: Any | None = None) -> ReplayProfile:
    """Return the edge-owned/default replay profile with optional settings overrides."""

    declared = getattr(runtime, "LIVE_INCREMENTAL_REPLAY_PROFILE", None)
    family = str(getattr(runtime, "FAMILY", "")).strip().lower()
    base = _FAMILY_PROFILES.get(family, DEFAULT_PROFILE)

    if isinstance(declared, Mapping):
        base = ReplayProfile(
            feature_rows=_positive_ints(declared.get("feature_rows", base.feature_rows)) or base.feature_rows,
            event_rows=_positive_ints(declared.get("event_rows", base.event_rows)),
            feature_boundary_guard_rows=max(
                0,
                int(
                    declared.get(
                        "feature_boundary_guard_rows",
                        declared.get(
                            "event_boundary_guard_rows",
                            base.feature_boundary_guard_rows,
                        ),
                    )
                ),
            ),
            event_boundary_guard_rows=max(0, int(declared.get("event_boundary_guard_rows", base.event_boundary_guard_rows))),
            decision_history_rows=max(50, int(declared.get("decision_history_rows", base.decision_history_rows))),
            minimum_feature_overlap_rows=max(1, int(declared.get("minimum_feature_overlap_rows", base.minimum_feature_overlap_rows))),
            minimum_event_decision_overlap_rows=max(0, int(declared.get("minimum_event_decision_overlap_rows", base.minimum_event_decision_overlap_rows))),
            retain_full_feature_history=bool(declared.get("retain_full_feature_history", base.retain_full_feature_history)),
            force_full_event_replay=bool(declared.get("force_full_event_replay", base.force_full_event_replay)),
        )

    if settings is None:
        return base

    global_feature = _positive_ints(
        getattr(settings, "LIVE_DASHBOARD_NATIVE_FEATURE_REPLAY_ROWS", ()) or ()
    )
    global_event = _positive_ints(
        getattr(settings, "LIVE_DASHBOARD_NATIVE_EVENT_REPLAY_ROWS", ()) or ()
    )
    return ReplayProfile(
        feature_rows=global_feature or base.feature_rows,
        event_rows=(global_event or base.event_rows) if not base.force_full_event_replay else (),
        feature_boundary_guard_rows=base.feature_boundary_guard_rows,
        event_boundary_guard_rows=base.event_boundary_guard_rows,
        decision_history_rows=max(
            base.decision_history_rows,
            50,
            int(
                getattr(
                    settings,
                    "LIVE_DASHBOARD_NATIVE_EVENT_DECISION_HISTORY_ROWS",
                    base.decision_history_rows,
                )
            ),
        ),
        minimum_feature_overlap_rows=max(
            base.minimum_feature_overlap_rows,
            1,
            int(
                getattr(
                    settings,
                    "LIVE_DASHBOARD_NATIVE_MIN_FEATURE_OVERLAP_ROWS",
                    base.minimum_feature_overlap_rows,
                )
            ),
        ),
        minimum_event_decision_overlap_rows=max(
            base.minimum_event_decision_overlap_rows,
            0,
            int(
                getattr(
                    settings,
                    "LIVE_DASHBOARD_NATIVE_MIN_EVENT_DECISION_OVERLAP_ROWS",
                    base.minimum_event_decision_overlap_rows,
                )
            ),
        ),
        retain_full_feature_history=base.retain_full_feature_history,
        force_full_event_replay=base.force_full_event_replay,
    )


def candidate_windows(configured: Iterable[int], available_rows: int) -> tuple[int, ...]:
    """Return unique bounded windows and always include the complete frame last."""

    available = max(0, int(available_rows))
    if available <= 0:
        return ()
    output = [value for value in _positive_ints(configured) if value < available]
    output.append(available)
    return tuple(dict.fromkeys(output))


def normalize_times(values: Iterable[Any] | None) -> list[pd.Timestamp]:
    output: list[pd.Timestamp] = []
    for value in values or []:
        try:
            stamp = pd.Timestamp(value)
            stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        except Exception:
            continue
        if not pd.isna(stamp):
            output.append(stamp)
    return sorted(set(output))


def latest_timestamp(frame: pd.DataFrame | None) -> pd.Timestamp | None:
    if not isinstance(frame, pd.DataFrame) or frame.empty or "Datetime" not in frame.columns:
        return None
    values = to_datetime_iso(frame["Datetime"], utc=True, errors="coerce").dropna()
    return None if values.empty else pd.Timestamp(values.max())


def event_decision_history(
    features: pd.DataFrame,
    events: pd.DataFrame,
    *,
    keep_rows: int,
) -> list[dict[str, Any]]:
    """Encode event/no-event decisions for the last feature timestamps."""

    if features.empty or "Datetime" not in features.columns:
        return []
    times = to_datetime_iso(features["Datetime"], utc=True, errors="coerce").dropna()
    times = times.drop_duplicates().sort_values().tail(max(1, int(keep_rows)))
    event_map: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    if isinstance(events, pd.DataFrame) and not events.empty and "Datetime" in events.columns:
        work = events.copy()
        work["Datetime"] = to_datetime_iso(work["Datetime"], utc=True, errors="coerce")
        work = work.dropna(subset=["Datetime"]).sort_values("Datetime")
        columns = [
            name
            for name in ("event_id", "side", "candidate_event_generator", "cusum_direction")
            if name in work.columns
        ]
        for stamp, group in work.groupby("Datetime", sort=True):
            rows: list[dict[str, Any]] = []
            for _, row in group.iterrows():
                rows.append({name: _json_scalar(row[name]) for name in columns})
            event_map[pd.Timestamp(stamp)] = rows
    history: list[dict[str, Any]] = []
    for stamp in times:
        ts = pd.Timestamp(stamp)
        history.append(
            {
                "Datetime": ts.isoformat(),
                "events": event_map.get(ts, []),
            }
        )
    return history


def _json_scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value) if isinstance(value, (pd.Timestamp,)) else value


def compare_event_decisions(
    previous_history: Any,
    current_features: pd.DataFrame,
    current_events: pd.DataFrame,
    *,
    boundary_guard_rows: int,
    minimum_overlap_rows: int,
) -> dict[str, Any]:
    """Compare exact event/no-event decisions on a stable replay overlap."""

    prior: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    if isinstance(previous_history, list):
        for row in previous_history:
            if not isinstance(row, Mapping):
                continue
            try:
                stamp = pd.Timestamp(row.get("Datetime"))
                stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
            except Exception:
                continue
            events = row.get("events")
            prior[stamp] = list(events) if isinstance(events, list) else []

    current_history = event_decision_history(
        current_features,
        current_events,
        keep_rows=max(len(current_features), minimum_overlap_rows + boundary_guard_rows + 1),
    )
    current: dict[pd.Timestamp, list[dict[str, Any]]] = {
        pd.Timestamp(row["Datetime"]): list(row.get("events") or [])
        for row in current_history
    }
    ordered_current = sorted(current)
    stable_times = ordered_current[max(0, int(boundary_guard_rows)) :]
    common = [stamp for stamp in stable_times if stamp in prior]
    mismatches: list[dict[str, Any]] = []
    for stamp in common:
        if prior[stamp] != current[stamp]:
            mismatches.append(
                {
                    "Datetime": stamp.isoformat(),
                    "previous": prior[stamp],
                    "current": current[stamp],
                }
            )
            if len(mismatches) >= 8:
                break
    ok = len(common) >= max(0, int(minimum_overlap_rows)) and not mismatches
    return {
        "ok": bool(ok),
        "common_decision_rows": int(len(common)),
        "required_decision_rows": int(max(0, minimum_overlap_rows)),
        "boundary_guard_rows": int(max(0, boundary_guard_rows)),
        "mismatches": mismatches,
        "reason": (
            "decision_overlap_pass"
            if ok
            else "decision_mismatch"
            if mismatches
            else "insufficient_decision_overlap"
        ),
    }
