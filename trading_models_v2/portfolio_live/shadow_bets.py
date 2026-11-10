"""Shadow bets for decommissioned ("off") edges.

AFML Ch.1 s.1.3.1.6 (p.8-9) evaluates a strategy on evidence. An edge the
decommission rule has switched off places no orders, so without shadow bets it
could never produce the evidence to come back (D022). While an edge is off, the
execution bot hands each approved signal it refuses to `record_shadow_signal`;
`resolve_pending_shadow_bets` later resolves it on live closed bars with the
signal's own barriers:

- long: SL hit if bar Low <= SL, TP hit if bar High >= TP; short mirrored;
  both in the same bar -> SL first (the edges' tie_policy, sl_first);
- vertical barrier: first bar closing at or after event_time + vertical_horizon_hours,
  exit at its Close;
- R = side * (exit - entry) / SL distance; a TP hit is TP distance / SL distance.

Costs: each resolved shadow bet is charged the active broker's modelled costs, as
the allocator charges backtest trades (portfolio_allocator/00d): spread in R =
modelled spread / SL distance (FIXED_POINTS model, or the hour-of-week mean of a
TABLE model -- its volatility-state multiplier is not applied), plus swap and
commission from shared.research.v3_horizon_lab._xef_apply_carry_costs. Firm from
portfolio_allocator/config/broker_repricing.json. If no cost model exists the bet
stays gross and is marked cost_basis="gross_no_cost_model".
Resolved shadow bets carry the signal's live_manifest_hash and are judged
together with real bets of the same edge version by decommission_rule.py.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[1]))
    from shared.io.timeparse import to_datetime_iso

V2_ROOT = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))
SHADOW_DIR = V2_ROOT / "portfolio_live" / "outputs" / "decommission"
PENDING_PATH = SHADOW_DIR / "shadow_signals.jsonl"
RESOLVED_PATH = SHADOW_DIR / "shadow_bets_resolved.jsonl"
LIVE_BARS_DIR = V2_ROOT / "portfolio_live" / "live_bars"
BROKER_REPRICING = V2_ROOT / "portfolio_allocator" / "config" / "broker_repricing.json"
COST_MODELS_DIR = V2_ROOT / "portfolio_allocator" / "config" / "broker_profiles"

SIGNAL_FIELDS = (
    "signal_id", "edge_id", "asset", "side_value", "event_time", "signal_price",
    "planned_stop_loss", "planned_take_profit", "planned_sl_distance_price",
    "planned_tp_distance_price", "vertical_horizon_hours", "live_manifest_hash",
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str) + "\n")


def record_shadow_signal(signal: dict[str, Any], reason: str) -> bool:
    """Record a refused signal of an off edge. Idempotent per signal_id."""
    sid = str(signal.get("signal_id") or "")
    if not sid:
        return False
    if any(r.get("signal_id") == sid for r in _read_jsonl(PENDING_PATH)):
        return False
    row = {k: signal.get(k) for k in SIGNAL_FIELDS}
    row["recorded_utc"] = datetime.now(timezone.utc).isoformat()
    row["reason"] = reason
    _append_jsonl(PENDING_PATH, [row])
    return True


def load_live_bars(asset: str, bars_dir: Path = LIVE_BARS_DIR) -> pd.DataFrame:
    path = bars_dir / f"{asset}_live_standard_quote_tick_bars.csv"
    if not path.exists():
        return pd.DataFrame(columns=["Datetime", "High", "Low", "Close"])
    bars = pd.read_csv(path, usecols=["Datetime", "High", "Low", "Close"])
    bars["Datetime"] = pd.to_datetime(bars["Datetime"], utc=True, errors="coerce", format="ISO8601")
    return bars.dropna(subset=["Datetime"]).sort_values("Datetime").reset_index(drop=True)


def resolve_one(row: dict[str, Any], bars: pd.DataFrame) -> dict[str, Any] | None:
    """Resolved shadow bet, or None while no barrier has been reached in the bars available."""
    side = int(row["side_value"])
    entry = float(row["signal_price"])
    sl, tp = float(row["planned_stop_loss"]), float(row["planned_take_profit"])
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    if sl_dist <= 0 or tp_dist <= 0:
        return {**row, "status": "invalid_barriers", "r": None}
    start = to_datetime_iso(row["event_time"], utc=True)
    deadline = start + pd.Timedelta(hours=float(row["vertical_horizon_hours"]))
    path = bars.loc[bars["Datetime"] > start]
    for bar in path.itertuples(index=False):
        hit_sl = bar.Low <= sl if side == 1 else bar.High >= sl
        hit_tp = bar.High >= tp if side == 1 else bar.Low <= tp
        if hit_sl:  # sl_first when both touch in one bar
            return {**row, "status": "resolved", "exit_reason": "sl", "exit_time": str(bar.Datetime), "r": -1.0}
        if hit_tp:
            return {**row, "status": "resolved", "exit_reason": "tp", "exit_time": str(bar.Datetime), "r": tp_dist / sl_dist}
        if bar.Datetime >= deadline:
            r = side * (float(bar.Close) - entry) / sl_dist
            return {**row, "status": "resolved", "exit_reason": "vertical", "exit_time": str(bar.Datetime), "r": r}
    return None


def load_cost_model(asset: str) -> tuple[dict[str, Any] | None, str]:
    if not BROKER_REPRICING.exists():
        return None, ""
    firm = str(json.loads(BROKER_REPRICING.read_text(encoding="utf-8")).get("firm") or "")
    path = COST_MODELS_DIR / f"{firm}_cost_models" / f"{asset}.json"
    return (json.loads(path.read_text(encoding="utf-8")), firm) if path.exists() else (None, firm)


def modelled_spread_price(cm: dict[str, Any], entry_price: float, entry_time: pd.Timestamp) -> float:
    ref = float(cm["ref_price"])
    if cm["model"] == "FIXED_POINTS":
        return entry_price * float(cm["fixed_points"]) / ref
    hour_of_week = int(entry_time.dayofweek) * 24 + int(entry_time.hour)
    val = float(cm.get("hour_mean", {}).get(str(hour_of_week), cm["mean_spread_bps_time_weighted"]))
    return entry_price * val / ref if cm.get("unit") == "pts" else entry_price * val / 1e4


def apply_broker_costs(row: dict[str, Any]) -> dict[str, Any]:
    """Net R of a resolved shadow bet under the active broker's cost model."""
    cm, firm = load_cost_model(str(row["asset"]))
    gross = float(row["r"])
    if cm is None:
        return {**row, "r_gross": gross, "cost_basis": "gross_no_cost_model"}
    from shared.research.v3_horizon_lab import _xef_apply_carry_costs

    entry = float(row["signal_price"])
    sl_dist = abs(entry - float(row["planned_stop_loss"]))
    entry_ts = to_datetime_iso(row["event_time"], utc=True)
    spread_r = modelled_spread_price(cm, entry, entry_ts) / sl_dist
    carry = _xef_apply_carry_costs(pd.DataFrame([{
        "status": "resolved", "entry_price": entry, "sl_dist": sl_dist, "side": int(row["side_value"]),
        "entry_ts": entry_ts, "exit_ts": to_datetime_iso(row["exit_time"], utc=True), "exec_r": gross - spread_r,
    }]), cm).iloc[0]
    return {**row, "r_gross": gross, "spread_R": spread_r, "swap_R": float(carry["swap_R"]),
            "commission_R": float(carry["commission_R"]), "swap_nights": int(carry["swap_nights"]),
            "r": float(carry["exec_r"]), "cost_basis": f"broker_cost_model_{firm}"}


def resolve_pending_shadow_bets(bars_dir: Path = LIVE_BARS_DIR) -> int:
    done = {r["signal_id"] for r in _read_jsonl(RESOLVED_PATH)}
    pending = [r for r in _read_jsonl(PENDING_PATH) if r.get("signal_id") not in done]
    resolved, bars_cache = [], {}
    for row in pending:
        asset = str(row.get("asset"))
        if asset not in bars_cache:
            bars_cache[asset] = load_live_bars(asset, bars_dir)
        out = resolve_one(row, bars_cache[asset])
        if out is not None:
            if out.get("status") == "resolved":
                out = apply_broker_costs(out)
            out["resolved_utc"] = datetime.now(timezone.utc).isoformat()
            resolved.append(out)
    _append_jsonl(RESOLVED_PATH, resolved)
    return len(resolved)


def load_resolved_shadow_bets() -> pd.DataFrame:
    rows = [r for r in _read_jsonl(RESOLVED_PATH) if r.get("status") == "resolved" and r.get("r") is not None]
    if not rows:
        return pd.DataFrame(columns=["edge_id", "live_manifest_hash", "r", "source"])
    df = pd.DataFrame(rows)
    # entry / exit time and side let the decommission rule group overlapping bets (AFML Ch.14 p.196, D056)
    df = df.rename(columns={"event_time": "entry_time", "side_value": "side"})
    df = df[[c for c in ("edge_id", "live_manifest_hash", "r", "entry_time", "exit_time", "side") if c in df.columns]]
    df["source"] = "shadow"
    return df
