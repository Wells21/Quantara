from __future__ import annotations

"""Closed-trade lifecycle attribution for the V2 execution bot.

The reconciler reads MT5 history only. It never opens, modifies, or closes an
order. It writes one final aggregate record when a broker position is fully
closed and can be attributed to a V2 signal/edge.
"""

import copy
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[1]))
    from shared.io.timeparse import to_datetime_iso


LEDGER_VERSION = "2026-08-20_v2_closed_trade_ledger_v4_targeted_reconcile"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except Exception:
        return default


def _safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except Exception:
        return default


def _utc_timestamp(value: Any) -> pd.Timestamp | None:
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return None
        return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    except Exception:
        return None


def _deal_time(deal: Any) -> pd.Timestamp | None:
    time_msc = _safe_int(getattr(deal, "time_msc", None), None)
    if time_msc and time_msc > 0:
        return pd.Timestamp(time_msc, unit="ms", tz="UTC")
    seconds = _safe_int(getattr(deal, "time", None), None)
    if seconds and seconds > 0:
        return pd.Timestamp(seconds, unit="s", tz="UTC")
    return None


def _deal_dict(deal: Any) -> dict[str, Any]:
    fields = (
        "ticket", "order", "position_id", "time", "time_msc", "type", "entry",
        "magic", "reason", "volume", "price", "commission", "swap", "profit",
        "fee", "symbol", "comment", "external_id",
    )
    return {name: getattr(deal, name, None) for name in fields}


def _weighted_price(deals: Iterable[Any]) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for deal in deals:
        volume = abs(float(_safe_float(getattr(deal, "volume", None), 0.0) or 0.0))
        price = _safe_float(getattr(deal, "price", None), None)
        if volume <= 0 or price is None:
            continue
        numerator += volume * float(price)
        denominator += volume
    return numerator / denominator if denominator > 0 else None


def _metadata_lookup(state: Mapping[str, Any], position_id: str, deals: list[Any]) -> tuple[dict[str, Any], str]:
    maps = [
        ("position_ticket", state.get("open_position_metadata_by_ticket") or {}),
        ("order", state.get("open_position_metadata_by_order") or {}),
        ("deal", state.get("open_position_metadata_by_deal") or {}),
        ("comment", state.get("open_position_metadata_by_comment") or {}),
    ]
    for source, mapping in maps:
        if source == "position_ticket":
            value = mapping.get(str(position_id))
            if isinstance(value, dict):
                return copy.deepcopy(value), source
        for deal in deals:
            if source == "order":
                key = str(getattr(deal, "order", "") or "")
            elif source == "deal":
                key = str(getattr(deal, "ticket", "") or "")
            elif source == "comment":
                key = str(getattr(deal, "comment", "") or "")
            else:
                key = ""
            value = mapping.get(key) if key else None
            if isinstance(value, dict):
                return copy.deepcopy(value), source
    return {}, "unresolved"


def _entry_exit_values(mt5: Any) -> tuple[set[int], set[int]]:
    entries = {
        int(value)
        for value in [
            getattr(mt5, "DEAL_ENTRY_IN", None),
            getattr(mt5, "DEAL_ENTRY_INOUT", None),
        ]
        if value is not None
    }
    exits = {
        int(value)
        for value in [
            getattr(mt5, "DEAL_ENTRY_OUT", None),
            getattr(mt5, "DEAL_ENTRY_OUT_BY", None),
            getattr(mt5, "DEAL_ENTRY_INOUT", None),
        ]
        if value is not None
    }
    return entries, exits


def _position_still_open(mt5: Any, position_id: str) -> bool:
    try:
        positions = mt5.positions_get() or []
    except Exception:
        positions = []
    for position in positions:
        keys = {
            str(getattr(position, "ticket", "") or ""),
            str(getattr(position, "identifier", "") or ""),
        }
        if str(position_id) in keys:
            return True
    return False


def _exit_reason(mt5: Any, exits: list[Any], metadata: Mapping[str, Any], exit_price: float | None) -> str:
    comments = " ".join(str(getattr(deal, "comment", "") or "").lower() for deal in exits)
    if "[sl" in comments or " sl" in comments or comments.strip() == "sl":
        return "stop_loss"
    if "[tp" in comments or " tp" in comments or comments.strip() == "tp":
        return "take_profit"
    reason_values = {int(getattr(deal, "reason", -999)) for deal in exits}
    if getattr(mt5, "DEAL_REASON_SL", None) in reason_values:
        return "stop_loss"
    if getattr(mt5, "DEAL_REASON_TP", None) in reason_values:
        return "take_profit"
    if "vertical" in comments:
        return "vertical_barrier"
    stop = _safe_float(metadata.get("stop_loss"), None)
    take = _safe_float(metadata.get("take_profit"), None)
    if exit_price is not None:
        tolerance = max(abs(float(exit_price)) * 1e-6, 1e-9)
        if stop is not None and abs(float(exit_price) - stop) <= tolerance:
            return "stop_loss"
        if take is not None and abs(float(exit_price) - take) <= tolerance:
            return "take_profit"
    return "broker_or_manual_close"


def _load_bars(live_bars_dir: Path, asset: str) -> pd.DataFrame:
    path = Path(live_bars_dir) / f"{str(asset).upper()}_live_standard_quote_tick_bars.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        frame = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
    if frame.empty or "Datetime" not in frame.columns:
        return pd.DataFrame()
    frame["Datetime"] = to_datetime_iso(frame["Datetime"], utc=True, errors="coerce")
    for column in ("Open", "High", "Low", "Close"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "Executable" in frame.columns:
        executable = frame["Executable"].astype(str).str.strip().str.lower().isin({"true", "1", "yes"})
        frame = frame.loc[executable]
    if "Gap_Healed" in frame.columns:
        gap = frame["Gap_Healed"].astype(str).str.strip().str.lower().isin({"true", "1", "yes"})
        frame = frame.loc[~gap]
    return frame.dropna(subset=["Datetime"]).sort_values("Datetime").drop_duplicates("Datetime", keep="last")


def _path_metrics(
    *,
    live_bars_dir: Path,
    asset: str,
    side_value: int,
    entry_price: float | None,
    entry_time: pd.Timestamp | None,
    exit_time: pd.Timestamp | None,
    sl_distance: float | None,
) -> dict[str, Any]:
    if entry_price is None or entry_time is None or exit_time is None:
        return {"bars_held": None, "mfe_price": None, "mae_price": None, "mfe_r": None, "mae_r": None}
    bars = _load_bars(live_bars_dir, asset)
    if bars.empty:
        return {"bars_held": None, "mfe_price": None, "mae_price": None, "mfe_r": None, "mae_r": None}
    window = bars.loc[(bars["Datetime"] >= entry_time) & (bars["Datetime"] <= exit_time)].copy()
    if window.empty:
        return {"bars_held": 0, "mfe_price": None, "mae_price": None, "mfe_r": None, "mae_r": None}
    high = _safe_float(window["High"].max() if "High" in window else None, None)
    low = _safe_float(window["Low"].min() if "Low" in window else None, None)
    if high is None or low is None:
        return {"bars_held": len(window), "mfe_price": None, "mae_price": None, "mfe_r": None, "mae_r": None}
    if int(side_value) >= 0:
        mfe = max(high - entry_price, 0.0)
        mae = max(entry_price - low, 0.0)
    else:
        mfe = max(entry_price - low, 0.0)
        mae = max(high - entry_price, 0.0)
    risk_distance = abs(float(sl_distance or 0.0))
    return {
        "bars_held": int(len(window)),
        "mfe_price": mfe,
        "mae_price": mae,
        "mfe_r": mfe / risk_distance if risk_distance > 0 else None,
        "mae_r": mae / risk_distance if risk_distance > 0 else None,
    }


def _position_key(value: Any) -> str:
    number = _safe_int(value, None)
    return str(number) if number is not None and number > 0 else ""


def _merge_deals_by_ticket(target: list[Any], additions: Iterable[Any]) -> None:
    existing = {
        _position_key(getattr(deal, "ticket", None))
        for deal in target
    }
    for deal in additions:
        ticket = _position_key(getattr(deal, "ticket", None))
        if ticket and ticket in existing:
            continue
        target.append(deal)
        if ticket:
            existing.add(ticket)


def _executed_signal_ids(state: Mapping[str, Any]) -> set[str]:
    return {
        str(signal_id)
        for signal_id, terminal in (state.get("terminal_signal_ids") or {}).items()
        if isinstance(terminal, Mapping)
        and str(terminal.get("status") or "").strip().lower() == "executed"
    }


def _expected_position_metadata(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return one expected broker position identifier per executed position."""
    executed_signals = _executed_signal_ids(state)
    expected: dict[str, dict[str, Any]] = {}
    ticket_map = state.get("open_position_metadata_by_ticket") or {}
    for ticket, raw in ticket_map.items():
        if not isinstance(raw, Mapping):
            continue
        signal_id = str(raw.get("signal_id") or "")
        if executed_signals and signal_id and signal_id not in executed_signals:
            continue
        position_id = (
            _position_key(raw.get("position_identifier"))
            or _position_key(raw.get("position_ticket"))
            or _position_key(ticket)
        )
        if position_id:
            expected[position_id] = dict(raw)

    for signal_id, raw in (state.get("open_position_metadata_by_signal_id") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        normalized_signal = str(raw.get("signal_id") or signal_id)
        if executed_signals and normalized_signal not in executed_signals:
            continue
        position_id = (
            _position_key(raw.get("position_identifier"))
            or _position_key(raw.get("position_ticket"))
        )
        if position_id:
            expected.setdefault(position_id, dict(raw))
    return expected


def _open_position_ids(mt5: Any) -> set[str]:
    try:
        positions = list(mt5.positions_get() or [])
    except Exception:
        positions = []
    ids: set[str] = set()
    for position in positions:
        for value in (getattr(position, "identifier", None), getattr(position, "ticket", None)):
            key = _position_key(value)
            if key:
                ids.add(key)
    return ids


def _documented_position_exceptions(ledger: Mapping[str, Any]) -> dict[str, str]:
    raw = ledger.get("documented_position_exceptions") or {}
    if isinstance(raw, Mapping):
        return {
            key: str(reason)
            for value, reason in raw.items()
            if (key := _position_key(value)) and str(reason or "").strip()
        }
    return {}


def _csv_position_ids(csv_path: Path) -> set[str]:
    path = Path(csv_path)
    if not path.exists():
        return set()
    try:
        frame = pd.read_csv(
            path,
            usecols=["position_id"],
            dtype={"position_id": "string"},
        )
    except Exception:
        return set()
    return {
        key
        for value in frame["position_id"].tolist()
        if (key := _position_key(value))
    }


def _targeted_position_deals(mt5: Any, position_id: str) -> list[Any]:
    try:
        return list(mt5.history_deals_get(position=int(position_id)) or [])
    except Exception:
        return []


def _resolve_executed_signal_positions(
    mt5: Any,
    state: Mapping[str, Any],
    deals: list[Any],
    expected: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[Any], list[str]]:
    """Resolve position IDs from stored deal/order tickets after a capture miss."""
    executed_signals = _executed_signal_ids(state)
    represented = {
        str(metadata.get("signal_id") or "")
        for metadata in expected.values()
        if str(metadata.get("signal_id") or "")
    }
    additions: list[Any] = []
    unresolved: list[str] = []
    signal_map = state.get("open_position_metadata_by_signal_id") or {}
    for signal_id in sorted(executed_signals - represented):
        raw = signal_map.get(signal_id)
        if not isinstance(raw, Mapping):
            unresolved.append(signal_id)
            continue
        metadata = dict(raw)
        position_id = (
            _position_key(metadata.get("position_identifier"))
            or _position_key(metadata.get("position_ticket"))
        )
        candidates = list(deals)
        deal_ticket = _position_key(metadata.get("deal"))
        order_ticket = _position_key(metadata.get("order"))
        matches = [
            deal for deal in candidates
            if (deal_ticket and _position_key(getattr(deal, "ticket", None)) == deal_ticket)
            or (order_ticket and _position_key(getattr(deal, "order", None)) == order_ticket)
        ]
        if not matches and deal_ticket:
            try:
                matches = list(mt5.history_deals_get(ticket=int(deal_ticket)) or [])
            except Exception:
                matches = []
            _merge_deals_by_ticket(additions, matches)
        if not position_id:
            position_ids = {
                key
                for deal in matches
                if (key := _position_key(getattr(deal, "position_id", None)))
            }
            if len(position_ids) == 1:
                position_id = next(iter(position_ids))
        if position_id:
            metadata["position_identifier"] = position_id
            expected[position_id] = metadata
        else:
            unresolved.append(signal_id)
    return expected, additions, unresolved


def _broker_pnl_summary(
    deals: Iterable[Any],
    *,
    account_balance: float | None,
    expected_starting_capital: float | None,
    history_start: pd.Timestamp,
) -> dict[str, Any]:
    values = list(deals)
    trading = 0.0
    non_trading = 0.0
    for deal in values:
        net = sum(
            float(_safe_float(getattr(deal, field, None), 0.0) or 0.0)
            for field in ("profit", "commission", "swap", "fee")
        )
        if _position_key(getattr(deal, "position_id", None)):
            trading += net
        else:
            non_trading += net
    expected_balance = None
    discrepancy = None
    if expected_starting_capital is not None:
        expected_balance = float(expected_starting_capital) + trading + non_trading
        if account_balance is not None:
            discrepancy = float(account_balance) - expected_balance
    return {
        "scope": "queried_history_window_informational",
        "history_start": history_start.isoformat(),
        "broker_trading_net": trading,
        "broker_non_trading_cashflows": non_trading,
        "expected_starting_capital": expected_starting_capital,
        "expected_balance_from_window": expected_balance,
        "account_balance": account_balance,
        "balance_discrepancy": discrepancy,
    }


def ensure_state(state: dict[str, Any]) -> dict[str, Any]:
    ledger = state.setdefault("closed_trade_ledger", {})
    ledger.setdefault("processed_position_ids", [])
    ledger.setdefault("last_reconcile_at", None)
    ledger.setdefault("last_history_cursor_msc", 0)
    ledger.setdefault("last_result", {})
    ledger.setdefault("documented_position_exceptions", {})
    ledger.setdefault("last_unexplained_executed_position_ids", [])
    ledger.setdefault("last_full_audit_at", None)
    ledger.setdefault("full_audit_count", 0)
    ledger.setdefault("targeted_reconcile_count", 0)
    return ledger


def reconcile_closed_trades(
    *,
    mt5: Any,
    state: dict[str, Any],
    live_bars_dir: Path,
    append_rows: Callable[[Path, list[dict[str, Any]], list[str] | None], None],
    csv_path: Path,
    logger: Any,
    minimum_interval_seconds: float = 30.0,
    history_lookback_days: int = 30,
    history_overlap_days: int = 7,
    full_audit_interval_seconds: float = 21600.0,
    force_full_audit: bool = False,
    account_balance: float | None = None,
    expected_starting_capital: float | None = None,
) -> dict[str, Any]:
    ledger = ensure_state(state)
    now = pd.Timestamp.now(tz="UTC")
    previous_run = _utc_timestamp(ledger.get("last_reconcile_at"))
    if (
        not force_full_audit
        and previous_run is not None
        and (now - previous_run).total_seconds() < max(float(minimum_interval_seconds), 1.0)
    ):
        return copy.deepcopy(ledger.get("last_result") or {"status": "interval_not_due"})

    cursor_msc = int(ledger.get("last_history_cursor_msc") or 0)
    last_full_audit = _utc_timestamp(ledger.get("last_full_audit_at"))
    full_audit_interval = max(float(full_audit_interval_seconds), 300.0)
    full_audit_due = bool(
        force_full_audit
        or last_full_audit is None
        or (now - last_full_audit).total_seconds() >= full_audit_interval
    )

    # E1: normal cycles are position-targeted. Broad date history is startup/periodic only.
    broad_history_query_used = False
    start = None
    deals: list[Any] = []
    if full_audit_due:
        if cursor_msc > 0:
            start = pd.Timestamp(cursor_msc, unit="ms", tz="UTC") - pd.Timedelta(
                days=max(int(history_overlap_days), 1)
            )
        else:
            start = now - pd.Timedelta(days=max(int(history_lookback_days), 1))
        try:
            deals = list(mt5.history_deals_get(start.to_pydatetime(), now.to_pydatetime()) or [])
            broad_history_query_used = True
        except Exception as exc:
            result = {
                "status": "history_query_failed",
                "error": f"{type(exc).__name__}:{exc}",
                "created_at": utc_now(),
                "reconcile_mode": "full_audit",
                "broad_history_query_used": True,
            }
            ledger["last_reconcile_at"] = utc_now()
            ledger["last_result"] = result
            return result

    expected_metadata = _expected_position_metadata(state)
    expected_metadata, targeted_signal_deals, unresolved_executed_signals = (
        _resolve_executed_signal_positions(mt5, state, deals, expected_metadata)
    )
    _merge_deals_by_ticket(deals, targeted_signal_deals)
    expected_ids = set(expected_metadata)
    open_ids = _open_position_ids(mt5)
    entry_values, exit_values = _entry_exit_values(mt5)
    by_position: dict[str, list[Any]] = {}
    max_msc = cursor_msc
    for deal in deals:
        position_id = str(getattr(deal, "position_id", "") or "")
        if not position_id or position_id == "0":
            continue
        by_position.setdefault(position_id, []).append(deal)
        max_msc = max(max_msc, int(getattr(deal, "time_msc", 0) or 0))

    processed = {
        key
        for value in ledger.get("processed_position_ids") or []
        if (key := _position_key(value))
    }
    processed.update(_csv_position_ids(csv_path))

    # The broad date query can omit old entry deals while still returning a
    # recent exit, or omit broker rows at the cursor boundary. Query each
    # unprocessed, closed position directly when its broad-history bucket is
    # absent or does not contain a complete entry/exit pair. Already-processed
    # positions are excluded before any targeted broker query.
    targeted_query_ids: list[str] = []
    for position_id in sorted(expected_ids - open_ids - processed):
        bucket = by_position.get(position_id) or []
        has_entry = any(
            int(getattr(deal, "entry", -999)) in entry_values
            for deal in bucket
        )
        has_exit = any(
            int(getattr(deal, "entry", -999)) in exit_values
            for deal in bucket
        )
        if has_entry and has_exit:
            continue
        targeted_query_ids.append(position_id)
        targeted = _targeted_position_deals(mt5, position_id)
        if targeted:
            bucket = by_position.setdefault(position_id, [])
            _merge_deals_by_ticket(bucket, targeted)
            _merge_deals_by_ticket(deals, targeted)
            for deal in targeted:
                max_msc = max(max_msc, int(getattr(deal, "time_msc", 0) or 0))

    rows: list[dict[str, Any]] = []
    unresolved = 0
    for position_id, position_deals in sorted(by_position.items()):
        if position_id in processed or _position_still_open(mt5, position_id):
            continue
        position_deals.sort(key=lambda deal: (int(getattr(deal, "time_msc", 0) or 0), int(getattr(deal, "ticket", 0) or 0)))
        entries = [deal for deal in position_deals if int(getattr(deal, "entry", -999)) in entry_values]
        exits = [deal for deal in position_deals if int(getattr(deal, "entry", -999)) in exit_values]
        if not entries or not exits:
            continue
        metadata, metadata_source = _metadata_lookup(state, position_id, position_deals)
        if not metadata:
            unresolved += 1

        entry_time = min(filter(None, (_deal_time(deal) for deal in entries)), default=None)
        exit_time = max(filter(None, (_deal_time(deal) for deal in exits)), default=None)
        actual_entry = _weighted_price(entries)
        actual_exit = _weighted_price(exits)
        gross_profit = sum(float(_safe_float(getattr(deal, "profit", None), 0.0) or 0.0) for deal in position_deals)
        commission = sum(float(_safe_float(getattr(deal, "commission", None), 0.0) or 0.0) for deal in position_deals)
        swap = sum(float(_safe_float(getattr(deal, "swap", None), 0.0) or 0.0) for deal in position_deals)
        fee = sum(float(_safe_float(getattr(deal, "fee", None), 0.0) or 0.0) for deal in position_deals)
        net_profit = gross_profit + commission + swap + fee
        planned_risk = _safe_float(metadata.get("actual_risk_usd_at_entry"), None)
        planned_entry = _safe_float(metadata.get("entry_price"), None)
        stop_loss = _safe_float(metadata.get("stop_loss"), None)
        take_profit = _safe_float(metadata.get("take_profit"), None)
        side_value = int(_safe_int(metadata.get("side_value"), 0) or 0)
        sl_distance = (
            abs(float(planned_entry) - float(stop_loss))
            if planned_entry is not None and stop_loss is not None
            else None
        )
        path = _path_metrics(
            live_bars_dir=live_bars_dir,
            asset=str(metadata.get("asset") or getattr(entries[0], "symbol", "")),
            side_value=side_value,
            entry_price=actual_entry,
            entry_time=entry_time,
            exit_time=exit_time,
            sl_distance=sl_distance,
        )
        row = {
            "created_at": utc_now(),
            "ledger_version": LEDGER_VERSION,
            "position_id": position_id,
            "signal_id": metadata.get("signal_id"),
            "edge_id": metadata.get("edge_id"),
            "asset": metadata.get("asset"),
            "symbol": metadata.get("symbol") or getattr(entries[0], "symbol", None),
            "family": metadata.get("family"),
            "side_name": metadata.get("side_name"),
            "side_value": side_value,
            "metadata_source": metadata_source,
            "entry_time": entry_time.isoformat() if entry_time is not None else None,
            "exit_time": exit_time.isoformat() if exit_time is not None else None,
            "event_time": metadata.get("event_time"),
            "planned_entry_price": planned_entry,
            "actual_entry_price": actual_entry,
            "entry_slippage_price": (
                actual_entry - planned_entry
                if actual_entry is not None and planned_entry is not None
                else None
            ),
            "planned_stop_loss": stop_loss,
            "planned_take_profit": take_profit,
            "actual_exit_price": actual_exit,
            "exit_reason": _exit_reason(mt5, exits, metadata, actual_exit),
            "entry_volume": sum(abs(float(_safe_float(getattr(deal, "volume", None), 0.0) or 0.0)) for deal in entries),
            "exit_volume": sum(abs(float(_safe_float(getattr(deal, "volume", None), 0.0) or 0.0)) for deal in exits),
            "gross_profit": gross_profit,
            "commission": commission,
            "swap": swap,
            "fee": fee,
            "net_profit": net_profit,
            "planned_risk_usd": planned_risk,
            "realized_r": net_profit / planned_risk if planned_risk and planned_risk > 0 else None,
            "bars_held": path.get("bars_held"),
            "mfe_price": path.get("mfe_price"),
            "mae_price": path.get("mae_price"),
            "mfe_r": path.get("mfe_r"),
            "mae_r": path.get("mae_r"),
            "entry_deal_tickets": [str(getattr(deal, "ticket", "") or "") for deal in entries],
            "exit_deal_tickets": [str(getattr(deal, "ticket", "") or "") for deal in exits],
            "entry_order_tickets": [str(getattr(deal, "order", "") or "") for deal in entries],
            "exit_order_tickets": [str(getattr(deal, "order", "") or "") for deal in exits],
            "execution_plan_hash": metadata.get("execution_plan_hash"),
            "live_manifest_hash": metadata.get("live_manifest_hash"),
            "edge_manifest_hash": metadata.get("edge_manifest_hash"),
            "bar_policy_hash": metadata.get("bar_policy_hash"),
            "release_id": (state.get("production_release_identity") or {}).get("release_id"),
            "broker_deals_json": [_deal_dict(deal) for deal in position_deals],
        }
        rows.append(row)
        processed.add(position_id)

    if rows:
        append_rows(csv_path, rows, ["position_id"])
        logger.info(
            "CLOSED_TRADE_LEDGER_RECONCILED positions=%s unresolved_metadata=%s path=%s",
            len(rows),
            unresolved,
            csv_path,
        )
    ledger["processed_position_ids"] = sorted(processed)[-10000:]
    ledger["last_history_cursor_msc"] = max_msc
    ledger["last_reconcile_at"] = utc_now()
    if broad_history_query_used:
        ledger["last_full_audit_at"] = ledger["last_reconcile_at"]
        ledger["full_audit_count"] = int(ledger.get("full_audit_count", 0) or 0) + 1
    else:
        ledger["targeted_reconcile_count"] = int(ledger.get("targeted_reconcile_count", 0) or 0) + 1
    documented_exceptions = _documented_position_exceptions(ledger)
    accounted_closed_ids = processed.copy()
    unexplained = sorted(
        expected_ids
        - open_ids
        - accounted_closed_ids
        - set(documented_exceptions)
    )
    ledger["last_unexplained_executed_position_ids"] = unexplained
    invariant_failed = bool(unexplained or unresolved_executed_signals)
    status = "PASS" if not invariant_failed else "FAIL_UNEXPLAINED_EXECUTED_POSITIONS"
    broker_pnl_reconciliation = (
        _broker_pnl_summary(
            deals,
            account_balance=account_balance,
            expected_starting_capital=expected_starting_capital,
            history_start=start,
        )
        if broad_history_query_used and start is not None
        else {
            "scope": "not_run_targeted_reconcile",
            "reason": "full_integrity_audit_not_due",
            "last_full_audit_at": ledger.get("last_full_audit_at"),
        }
    )
    result = {
        "status": status,
        "created_at": utc_now(),
        "reconcile_mode": "full_audit" if broad_history_query_used else "targeted_positions",
        "broad_history_query_used": bool(broad_history_query_used),
        "full_audit_interval_seconds": full_audit_interval,
        "last_full_audit_at": ledger.get("last_full_audit_at"),
        "full_audit_count": int(ledger.get("full_audit_count", 0) or 0),
        "targeted_reconcile_count": int(ledger.get("targeted_reconcile_count", 0) or 0),
        "positions_written": len(rows),
        "unresolved_metadata_count": unresolved,
        "history_deal_count": len(deals),
        "expected_executed_position_count": len(expected_ids),
        "open_expected_position_count": len(expected_ids & open_ids),
        "closed_expected_position_count": len(expected_ids & accounted_closed_ids),
        "documented_exception_count": len(expected_ids & set(documented_exceptions)),
        "unexplained_executed_position_count": len(unexplained),
        "unexplained_executed_position_ids": unexplained,
        "unresolved_executed_signal_count": len(unresolved_executed_signals),
        "unresolved_executed_signal_ids": unresolved_executed_signals,
        "targeted_history_query_position_ids": targeted_query_ids,
        "history_overlap_days": max(int(history_overlap_days), 1),
        "history_lookback_days": max(int(history_lookback_days), 1),
        "last_history_cursor_msc": max_msc,
        "broker_pnl_reconciliation": broker_pnl_reconciliation,
    }
    if invariant_failed:
        logger.error(
            "CLOSED_TRADE_LEDGER_INVARIANT_FAILED unexplained_count=%s position_ids=%s "
            "unresolved_signal_count=%s signal_ids=%s",
            len(unexplained),
            unexplained,
            len(unresolved_executed_signals),
            unresolved_executed_signals,
        )
    ledger["last_result"] = result
    return result


# QUANTARA_PHASE_E_EXECUTOR_CLEANUP_E1_E2_E3_PY39_V1
__all__ = ["LEDGER_VERSION", "ensure_state", "reconcile_closed_trades"]
