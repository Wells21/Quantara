"""
Per-edge backtest reference resolution for the drift dashboard.

CRITICAL: never hardcode a "locked geometry" or "winner label" per edge.
An edge's geometry can be re-locked at any time (Gate C rerun), so it is
re-derived from that edge's OWN current code (candidate_events.py /
model_rows.py constants, hours-based for calendar-time edges).

The per-bet comparison sample is the allocator representative trade ledger:
meta-approved bets at the live threshold with broker costs -- the same
benchmark the decommission rule uses. An edge with no rows there is a real,
visible gap; never fall back to a different sample.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
if str(_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(_V2_ROOT))

from portfolio_live.drift_dashboard.data_sources import DriftDataError  # noqa: E402


def _edge_root(edge_id: str) -> Path:
    asset, family_side = edge_id.split("/", 1)
    return _V2_ROOT / "assets" / asset / "edges" / family_side


def _import_edge_module(edge_root: Path, module_name: str) -> ModuleType:
    file_path = edge_root / f"{module_name}.py"
    if not file_path.exists():
        raise DriftDataError(f"{module_name}.py not found under {edge_root}")
    # Each edge module does its own sys.path bootstrap referencing its own
    # __file__ location -- give it a unique spec name per edge_root so two
    # different edges' same-named modules (e.g. two candidate_events.py)
    # don't collide in sys.modules.
    unique_name = f"_drift_dashboard_dynamic_{edge_root.parent.parent.name}_{edge_root.name}_{module_name}"
    spec = importlib.util.spec_from_file_location(unique_name, file_path)
    if spec is None or spec.loader is None:
        raise DriftDataError(f"Could not load module spec for {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    # Edge modules assume cwd-independent imports via their own sys.path
    # insertion at the top of the file; running exec_module triggers that.
    original_sys_path = list(sys.path)
    try:
        sys.path.insert(0, str(edge_root))
        spec.loader.exec_module(module)
    finally:
        sys.path = original_sys_path
    return module


def _fmt_num(x: float) -> str:
    """Match the Gate C config_label formatting convention: whole numbers
    with no trailing .0 (e.g. 2.0 -> '2'), fractional kept as-is (1.5 -> '1.5')."""
    xf = float(x)
    if xf == int(xf):
        return str(int(xf))
    return str(xf)


@dataclass
class EdgeGeometry:
    """Locked label geometry. Lookback and vertical are in hours for calendar-time
    edges (EVENT_VOL_LOOKBACK_HOURS / VERTICAL_HORIZON_HOURS, 2026-09-25 rollout) and in
    bars for legacy edges; Gate C labels use the same numbers either way."""

    edge_id: str
    event_vol_lookback: float
    event_vol_ewm_span: int
    vertical_horizon: float
    pt_multiplier: float
    sl_multiplier: float
    time_unit: str  # "hours" | "bars"

    @property
    def config_label(self) -> str:
        return (
            f"H{_fmt_num(self.event_vol_lookback)}_E{int(self.event_vol_ewm_span)}_"
            f"V{_fmt_num(self.vertical_horizon)}_PT{_fmt_num(self.pt_multiplier)}_"
            f"SL{_fmt_num(self.sl_multiplier)}"
        )


# Hours names first: an edge that has them is a calendar-time edge.
_LOOKBACK_NAMES_HOURS = ("EVENT_VOL_LOOKBACK_HOURS",)
_LOOKBACK_NAMES_BARS = ("EVENT_VOL_LOOKBACK_BARS", "EVENT_VOL_LOOKBACK", "VOL_LOOKBACK")
_VERTICAL_NAMES_HOURS = ("VERTICAL_HORIZON_HOURS",)
_VERTICAL_NAMES_BARS = ("VERTICAL_HORIZON",)
_EWM_NAMES = ("EVENT_VOL_EWM_SPAN", "VOL_EWM_SPAN")


def _first_attr(module: ModuleType, names: tuple[str, ...]) -> Any:
    for name in names:
        if hasattr(module, name):
            return getattr(module, name)
    return None


def read_current_edge_geometry(edge_id: str) -> EdgeGeometry:
    """Read an edge's ACTUAL current locked geometry from its own code --
    never from a research-ledger citation, which can go stale the moment
    the edge is re-locked (confirmed happens in practice this session)."""
    edge_root = _edge_root(edge_id)
    candidate_events = _import_edge_module(edge_root, "candidate_events")
    model_rows = _import_edge_module(edge_root, "model_rows")

    lookback_h = _first_attr(candidate_events, _LOOKBACK_NAMES_HOURS)
    vertical_h = _first_attr(model_rows, _VERTICAL_NAMES_HOURS)
    if lookback_h is not None or vertical_h is not None:
        time_unit, lookback, vertical = "hours", lookback_h, vertical_h
    else:
        time_unit = "bars"
        lookback = _first_attr(candidate_events, _LOOKBACK_NAMES_BARS)
        vertical = _first_attr(model_rows, _VERTICAL_NAMES_BARS)
    ewm_span = _first_attr(candidate_events, _EWM_NAMES)
    pt = getattr(model_rows, "PT_MULTIPLIER", None)
    sl = getattr(model_rows, "SL_MULTIPLIER", None)

    missing = [
        name
        for name, val in [
            ("event_vol_lookback", lookback),
            ("event_vol_ewm_span", ewm_span),
            ("vertical_horizon", vertical),
            ("pt_multiplier", pt),
            ("sl_multiplier", sl),
        ]
        if val is None
    ]
    if missing:
        raise DriftDataError(f"{edge_id}: could not read geometry constants ({time_unit}): {missing}")

    return EdgeGeometry(
        edge_id=edge_id,
        event_vol_lookback=float(lookback),
        event_vol_ewm_span=int(ewm_span),
        vertical_horizon=float(vertical),
        pt_multiplier=float(pt),
        sl_multiplier=float(sl),
        time_unit=time_unit,
    )


_BENCHMARK_CONFIG = _V2_ROOT / "portfolio_live" / "config" / "decommission_rule.json"


@lru_cache(maxsize=1)
def _load_benchmark_ledger() -> pd.DataFrame:
    """Meta-approved backtest bets at each edge's live threshold, with broker costs.

    Same source as the decommission rule (config/decommission_rule.json
    backtest_benchmark). Live trades are bets the meta-model approved, so the
    comparison sample must be approved bets too (AFML p.211: bets actually taken),
    not every Gate C candidate event.
    """
    spec = json.loads(_BENCHMARK_CONFIG.read_text(encoding="utf-8"))["backtest_benchmark"]
    cols = ["edge_id", "threshold", "entry_time", "exit_time", spec["return_column"]]
    df = pd.read_csv(_V2_ROOT / spec["ledger"], usecols=cols, low_memory=False)
    df = df.rename(columns={spec["return_column"]: "exec_r"})
    df["entry_ts"] = pd.to_datetime(df["entry_time"], utc=True, errors="coerce", format="ISO8601")
    df["exit_ts"] = pd.to_datetime(df["exit_time"], utc=True, errors="coerce", format="ISO8601")
    df["hold_hours"] = (df["exit_ts"] - df["entry_ts"]).dt.total_seconds() / 3600.0
    df.attrs["source_path"] = str(_V2_ROOT / spec["ledger"])
    return df


def resolve_backtest_trade_sample(edge_id: str) -> dict[str, Any]:
    """The backtest bets this edge's live trades are compared against.

    Returns {'trades': DataFrame(exec_r, hold_hours, ...)} or an explicit 'gap'
    reason -- never a substitute sample.
    """
    try:
        geometry = read_current_edge_geometry(edge_id)
        label = geometry.config_label
    except DriftDataError as exc:
        return {"edge_id": edge_id, "trades": None, "gap": f"geometry_read_failed: {exc}"}
    ledger = _load_benchmark_ledger()
    trades = ledger.loc[ledger["edge_id"] == edge_id].copy()
    if trades.empty:
        return {"edge_id": edge_id, "trades": None, "gap": "no_meta_approved_backtest_bets_in_representative_ledger",
                "current_geometry_label": label}
    return {
        "edge_id": edge_id,
        "trades": trades,
        "gap": None,
        "current_geometry_label": label,
        "threshold": float(trades["threshold"].iloc[0]),
        "source_path": ledger.attrs["source_path"],
    }


if __name__ == "__main__":
    import json

    from portfolio_live.drift_dashboard.data_sources import load_current_live_edge_ids

    for eid in sorted(load_current_live_edge_ids()):
        result = resolve_backtest_trade_sample(eid)
        if result["gap"]:
            print(f"{eid}: GAP -> {result['gap']}")
        else:
            n = len(result["trades"])
            print(f"{eid}: OK -> label={result['current_geometry_label']} threshold={result['threshold']} n_backtest_bets={n}")
