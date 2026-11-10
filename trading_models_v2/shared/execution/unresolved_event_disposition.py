"""Task 2B.1 -- classify Task 2A's unresolved executable-target events.

Reference: docs/mldp/TASK2B_POPULATION_RULE_DECISION.md.

Every non-"resolved" `execution_status` produced by
`shared/execution/oof_exact_execution.py` / `executable_target_dataset.py`
is assigned to exactly one of three dispositions:

TRUE_NO_FILL
    Live execution would also decline to act here (the same weekend-gap /
    session-blackout / signal-age condition the live executor itself
    enforces). Correctly excluded from the training population -- this is
    not a data gap, it is training on exactly the population the system will
    ever act on.

RIGHT_CENSORED_END_OF_HISTORY
    Collected tick/bar history simply ends before the outcome could be
    observed. A research-data-completeness artifact, not a market condition
    -- live trading never encounters this, since live always has a
    continuously arriving feed. Tracked separately; never pooled with
    TRUE_NO_FILL, and expected to shrink as more history is collected.

DATA_DEFECT
    Should be structurally impossible in a correct pipeline. A nonzero count
    means something upstream is broken (stale geometry hash, an unexplained
    hole in otherwise-continuous tick coverage, a corrupt bar). Must halt
    promotion and be investigated, never silently dropped.

Entry-side symmetry with the live executor is not asserted -- it is
guaranteed by construction: the weekend-gap and session-blackout
configuration used here is loaded directly from
`portfolio_live/live_settings.py`, the exact same file
`shared/execution/oof_exact_execution.py` already loads (via
`importlib.util.spec_from_file_location`, the same mechanism used there for
the live signal-age cap) to avoid importing the heavy, MT5-dependent
`portfolio_execution_bot.py` module for a read-only research diagnostic.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

TRUE_NO_FILL = "TRUE_NO_FILL"
RIGHT_CENSORED_END_OF_HISTORY = "RIGHT_CENSORED_END_OF_HISTORY"
DATA_DEFECT = "DATA_DEFECT"
RESOLVED = "resolved"

_ALWAYS_TRUE_NO_FILL = {"stale_entry_quote"}
_ALWAYS_RIGHT_CENSORED = {"vertical_deadline_bar_not_found"}
_ALWAYS_DATA_DEFECT = {
    "event_bar_not_found",
    "invalid_signal_mid",
    "research_entry_signal_mid_mismatch",
    "invalid_barrier_distance",
}
_AMBIGUOUS_ENTRY_SIDE = {"missing_entry_quote"}
_AMBIGUOUS_EXIT_SIDE = {"unresolved_tick_coverage"}

_KNOWN_STATUSES = (
    {RESOLVED}
    | _ALWAYS_TRUE_NO_FILL
    | _ALWAYS_RIGHT_CENSORED
    | _ALWAYS_DATA_DEFECT
    | _AMBIGUOUS_ENTRY_SIDE
    | _AMBIGUOUS_EXIT_SIDE
)


class UnresolvedDispositionError(RuntimeError):
    """Fail-closed error for the Task 2B.1 disposition classifier."""


def load_live_calendar_guard_config(v2_root: Path) -> dict[str, Any]:
    """Load the live executor's weekend-gap / session-blackout configuration
    straight from ``portfolio_live/live_settings.py``, with any
    ``local_live_settings.json`` overrides already applied (that file's
    module-level code applies them at import time), without importing the
    heavy ``portfolio_execution_bot.py`` module.
    """
    settings_path = Path(v2_root) / "portfolio_live" / "live_settings.py"
    if not settings_path.exists():
        raise UnresolvedDispositionError(
            f"Missing live settings contract: {settings_path}"
        )
    spec = importlib.util.spec_from_file_location(
        "_quantara_v3_task2b_live_settings", settings_path
    )
    if spec is None or spec.loader is None:
        raise UnresolvedDispositionError(f"Could not load live settings: {settings_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    weekend = getattr(module, "PORTFOLIO_WEEKEND_ENTRY_GUARD_BY_ASSET", None)
    blackout = getattr(module, "PORTFOLIO_ENTRY_BLACKOUT_WINDOWS_UTC_BY_ASSET", None)
    if not isinstance(weekend, dict) or not isinstance(blackout, dict):
        raise UnresolvedDispositionError(
            "live_settings.py did not expose "
            "PORTFOLIO_WEEKEND_ENTRY_GUARD_BY_ASSET / "
            "PORTFOLIO_ENTRY_BLACKOUT_WINDOWS_UTC_BY_ASSET as dicts."
        )
    return {
        "weekend_entry_guard_by_asset": weekend,
        "entry_blackout_windows_utc_by_asset": blackout,
    }


def _clock_minutes(value: Any) -> int:
    text = str(value or "").strip()
    hour_text, minute_text = (text.split(":", 1) + ["0"])[:2]
    hour = min(max(int(hour_text), 0), 23)
    minute = min(max(int(minute_text), 0), 59)
    return hour * 60 + minute


def weekend_gap_blocks_entry(
    asset: str, event_time: pd.Timestamp, weekend_entry_guard_by_asset: dict[str, Any]
) -> str | None:
    """Reimplements, standalone and read-only, the exact weekend-gap
    arithmetic `portfolio_execution_bot.market_entry_guard()` enforces live
    (that function additionally needs a live MT5 connection/state object,
    which is irrelevant for a historical classification pass -- only the
    pure calendar arithmetic is reproduced here)."""
    weekend = weekend_entry_guard_by_asset.get(asset) or {}
    if not isinstance(weekend, dict) or not weekend:
        return None
    friday = float(weekend.get("friday_block_hour_utc", 24.0))
    monday = float(weekend.get("monday_resume_hour_utc", 0.0))
    hour = float(event_time.hour) + float(event_time.minute) / 60.0
    weekday = int(event_time.weekday())
    if (
        (weekday == 4 and hour >= friday)
        or weekday in {5, 6}
        or (weekday == 0 and hour < monday)
    ):
        return f"weekend_gap_entry_guard:{asset}"
    return None


def inside_utc_blackout(
    asset: str,
    event_time: pd.Timestamp,
    entry_blackout_windows_utc_by_asset: dict[str, Any],
) -> str | None:
    """Reimplements, standalone and read-only, the exact blackout-window
    check `portfolio_execution_bot._inside_utc_blackout()` enforces live."""
    current = int(event_time.hour) * 60 + int(event_time.minute)
    for index, record in enumerate(
        entry_blackout_windows_utc_by_asset.get(asset, []) or []
    ):
        if not isinstance(record, dict):
            continue
        weekdays = {int(value) for value in record.get("weekdays", range(7))}
        if int(event_time.weekday()) not in weekdays:
            continue
        try:
            start = _clock_minutes(record.get("start"))
            end = _clock_minutes(record.get("end"))
        except Exception:
            return f"invalid_entry_blackout_window:{asset}:{index}"
        inside = start <= current < end if start <= end else (current >= start or current < end)
        if inside:
            return f"configured_utc_session_blackout:{asset}:{record.get('start')}:{record.get('end')}"
    return None


def _catalog_end(catalog: list[dict[str, Any]]) -> pd.Timestamp | None:
    if not catalog:
        return None
    return max(pd.Timestamp(rec["_end"]) for rec in catalog)


def classify_entry_side_unresolved(
    asset: str,
    event_time: pd.Timestamp,
    catalog: list[dict[str, Any]],
    *,
    weekend_entry_guard_by_asset: dict[str, Any],
    entry_blackout_windows_utc_by_asset: dict[str, Any],
) -> str:
    """Classify a `missing_entry_quote` row.

    `missing_entry_quote` means the resolver searched every remaining tick
    file from the event onward and never found a qualifying tick. The
    question this function answers is *why*: a market-closure condition
    live would also refuse to trade through (TRUE_NO_FILL), collected
    history simply ending before the event (RIGHT_CENSORED_END_OF_HISTORY),
    or an unexplained gap despite catalog coverage nominally extending past
    the event (DATA_DEFECT).
    """
    reason = weekend_gap_blocks_entry(asset, event_time, weekend_entry_guard_by_asset)
    if reason is not None:
        return TRUE_NO_FILL
    reason = inside_utc_blackout(asset, event_time, entry_blackout_windows_utc_by_asset)
    if reason is not None:
        return TRUE_NO_FILL

    catalog_end = _catalog_end(catalog)
    if catalog_end is None or event_time >= catalog_end:
        return RIGHT_CENSORED_END_OF_HISTORY
    # The catalog nominally has a file covering a period after this event,
    # yet no qualifying tick was found and it isn't a known closure window.
    return DATA_DEFECT


def classify_exit_side_unresolved(
    vertical_deadline: pd.Timestamp,
    catalog: list[dict[str, Any]],
) -> str:
    """Classify an `unresolved_tick_coverage` row. Entry already succeeded
    (a bet was actually taken), so there is no weekend/blackout "would live
    have entered" question here -- the only question is whether collected
    history covers the vertical deadline at all.
    """
    catalog_end = _catalog_end(catalog)
    if catalog_end is None or vertical_deadline >= catalog_end:
        return RIGHT_CENSORED_END_OF_HISTORY
    return DATA_DEFECT


def classify_disposition(
    execution_status: str,
    *,
    asset: str,
    event_time: pd.Timestamp,
    vertical_deadline: pd.Timestamp | None,
    catalog: list[dict[str, Any]],
    weekend_entry_guard_by_asset: dict[str, Any],
    entry_blackout_windows_utc_by_asset: dict[str, Any],
) -> str:
    status = str(execution_status)
    if status == RESOLVED:
        return RESOLVED
    if status in _ALWAYS_TRUE_NO_FILL:
        return TRUE_NO_FILL
    if status in _ALWAYS_RIGHT_CENSORED:
        return RIGHT_CENSORED_END_OF_HISTORY
    if status in _ALWAYS_DATA_DEFECT:
        return DATA_DEFECT
    if status in _AMBIGUOUS_ENTRY_SIDE:
        return classify_entry_side_unresolved(
            asset,
            event_time,
            catalog,
            weekend_entry_guard_by_asset=weekend_entry_guard_by_asset,
            entry_blackout_windows_utc_by_asset=entry_blackout_windows_utc_by_asset,
        )
    if status in _AMBIGUOUS_EXIT_SIDE:
        if vertical_deadline is None or pd.isna(vertical_deadline):
            raise UnresolvedDispositionError(
                f"unresolved_tick_coverage row missing vertical_deadline for "
                f"disposition classification (event_time={event_time})."
            )
        return classify_exit_side_unresolved(pd.Timestamp(vertical_deadline), catalog)
    raise UnresolvedDispositionError(
        f"Unknown execution_status {status!r}; not in the Task 2A engine's "
        f"documented status set {sorted(_KNOWN_STATUSES)}. Task 2B's "
        "disposition rule must be extended before this status can be "
        "classified -- refusing to guess."
    )


def annotate_dispositions(
    resolution_df: pd.DataFrame,
    *,
    asset: str,
    catalog: list[dict[str, Any]],
    calendar_guard_config: dict[str, Any],
    time_col: str = "Datetime",
    deadline_col: str = "vertical_deadline",
    status_col: str = "execution_status",
) -> pd.DataFrame:
    """Add a `task2b_disposition` column to a Task 2A resolution frame."""
    if status_col not in resolution_df.columns:
        raise UnresolvedDispositionError(f"Missing {status_col!r} column.")
    weekend_cfg = calendar_guard_config["weekend_entry_guard_by_asset"]
    blackout_cfg = calendar_guard_config["entry_blackout_windows_utc_by_asset"]
    out = resolution_df.copy()
    event_times = to_datetime_iso(out[time_col], utc=True, errors="coerce")
    deadlines = (
        to_datetime_iso(out[deadline_col], utc=True, errors="coerce")
        if deadline_col in out.columns
        else pd.Series(pd.NaT, index=out.index)
    )
    dispositions = []
    for idx, status in out[status_col].items():
        dispositions.append(
            classify_disposition(
                status,
                asset=asset,
                event_time=event_times.loc[idx],
                vertical_deadline=deadlines.loc[idx],
                catalog=catalog,
                weekend_entry_guard_by_asset=weekend_cfg,
                entry_blackout_windows_utc_by_asset=blackout_cfg,
            )
        )
    out["task2b_disposition"] = dispositions
    return out


def summarize_dispositions(annotated_df: pd.DataFrame) -> dict[str, Any]:
    counts = annotated_df["task2b_disposition"].astype(str).value_counts(dropna=False).to_dict()
    by_status = (
        annotated_df.groupby(["task2b_disposition", "execution_status"], dropna=False)
        .size()
        .to_dict()
    )
    data_defect_count = int(counts.get(DATA_DEFECT, 0))
    return {
        "total_rows": int(len(annotated_df)),
        "disposition_counts": {str(k): int(v) for k, v in counts.items()},
        "disposition_by_status": {
            f"{disp}|{status}": int(n) for (disp, status), n in by_status.items()
        },
        "data_defect_count": data_defect_count,
        "audit_status": "FAIL" if data_defect_count > 0 else "PASS",
    }
