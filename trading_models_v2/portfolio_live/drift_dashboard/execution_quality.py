"""
Execution-quality / cost-drag drill-down for the drift dashboard (v2).

Directly answers the question that motivated this module (2026-09-24):
"we already accounted for cost/spread when gating this edge and it came out
positive -- so why would live be different?"

Gate A/B/C cost_bps (RESEARCH_COST_BPS_BY_ASSET, live_settings.py) is a
single per-entry spread assumption applied once at trade entry. It has no
concept of: (a) real slippage between the planned fill price and the actual
broker fill, (b) overnight swap/financing on trades that get held longer
than the backtest's typical bar count, (c) commission. All three are real
dollars visible in the closed-trade ledger's own fields
(entry_slippage_price, swap, commission, fee, planned_risk_usd) and are
NOT modeled anywhere upstream of live -- so they are a legitimate, expected
source of live-vs-backtest divergence, distinct from the edge's statistical
edge decaying.

This module converts each of those into R units (dividing by the trade's
own planned_risk_usd, the same denominator realized_r uses) so they are
directly comparable to the KS-test drift deltas in drift_stats.py.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
if str(_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(_V2_ROOT))

from portfolio_live.live_settings import RESEARCH_COST_BPS_BY_ASSET  # noqa: E402

# Float noise floor -- MT5 broker round-trips leave ~1e-12-scale residuals on
# "zero" slippage; anything below this is not real slippage.
SLIPPAGE_NOISE_FLOOR = 1e-6


@dataclass
class EdgeExecutionQuality:
    edge_id: str
    asset: str
    n_trades: int
    n_priced: int  # trades with a usable planned_risk_usd denominator
    n_with_slippage: int
    pct_with_slippage: float
    mean_slippage_price: float | None
    mean_swap_drag_r: float
    mean_commission_drag_r: float
    mean_fee_drag_r: float
    mean_total_cost_drag_r: float
    total_cost_drag_r_sum: float
    assumed_cost_bps: float | None
    dominant_component: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "asset": self.asset,
            "n_trades": self.n_trades,
            "n_priced": self.n_priced,
            "n_with_slippage": self.n_with_slippage,
            "pct_with_slippage": round(self.pct_with_slippage, 1),
            "mean_slippage_price": self.mean_slippage_price,
            "mean_swap_drag_r": round(self.mean_swap_drag_r, 4),
            "mean_commission_drag_r": round(self.mean_commission_drag_r, 4),
            "mean_fee_drag_r": round(self.mean_fee_drag_r, 4),
            "mean_total_cost_drag_r": round(self.mean_total_cost_drag_r, 4),
            "total_cost_drag_r_sum": round(self.total_cost_drag_r_sum, 2),
            "assumed_cost_bps": self.assumed_cost_bps,
            "dominant_component": self.dominant_component,
            "note": self.note,
        }


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    b_safe = b.replace(0, np.nan)
    return a / b_safe


def build_edge_execution_quality(edge_id: str, live_trades: pd.DataFrame) -> EdgeExecutionQuality:
    asset = edge_id.split("/", 1)[0]
    n_trades = len(live_trades)

    priced = live_trades[pd.to_numeric(live_trades.get("planned_risk_usd"), errors="coerce") > 0].copy()
    n_priced = len(priced)

    if n_priced == 0:
        return EdgeExecutionQuality(
            edge_id=edge_id,
            asset=asset,
            n_trades=n_trades,
            n_priced=0,
            n_with_slippage=0,
            pct_with_slippage=0.0,
            mean_slippage_price=None,
            mean_swap_drag_r=0.0,
            mean_commission_drag_r=0.0,
            mean_fee_drag_r=0.0,
            mean_total_cost_drag_r=0.0,
            total_cost_drag_r_sum=0.0,
            assumed_cost_bps=RESEARCH_COST_BPS_BY_ASSET.get(asset),
            dominant_component="n/a",
            note="No trades with a usable planned_risk_usd -- cannot convert cost drag to R units.",
        )

    risk = pd.to_numeric(priced["planned_risk_usd"], errors="coerce")
    swap = pd.to_numeric(priced.get("swap", 0.0), errors="coerce").fillna(0.0)
    commission = pd.to_numeric(priced.get("commission", 0.0), errors="coerce").fillna(0.0)
    fee = pd.to_numeric(priced.get("fee", 0.0), errors="coerce").fillna(0.0)

    swap_drag_r = _safe_div(swap, risk)
    commission_drag_r = _safe_div(commission, risk)
    fee_drag_r = _safe_div(fee, risk)
    total_drag_r = swap_drag_r + commission_drag_r + fee_drag_r

    slip = pd.to_numeric(priced.get("entry_slippage_price", 0.0), errors="coerce").fillna(0.0)
    real_slip = slip[slip.abs() > SLIPPAGE_NOISE_FLOOR]
    n_with_slippage = len(real_slip)

    components = {
        "swap": swap_drag_r.sum(),
        "commission": commission_drag_r.sum(),
        "fee": fee_drag_r.sum(),
    }
    dominant = max(components, key=lambda k: abs(components[k])) if any(components.values()) else "none"

    note_parts = []
    mean_total = float(total_drag_r.mean())
    if abs(mean_total) < 1e-4:
        note_parts.append("Cost drag is negligible for this edge's live sample so far.")
    else:
        pct_swap = 100.0 * abs(components["swap"]) / max(sum(abs(v) for v in components.values()), 1e-9)
        note_parts.append(
            f"Swap accounts for {pct_swap:.0f}% of total realized cost drag "
            f"-- {'material' if abs(components['swap']) > abs(components['commission']) + abs(components['fee']) else 'minor'} "
            "vs commission+fee."
        )
    if n_with_slippage > 0:
        note_parts.append(
            f"{n_with_slippage}/{n_priced} trades ({100.0*n_with_slippage/n_priced:.0f}%) show a real "
            "non-zero fill slippage vs the planned entry price -- not modeled in Gate A/B/C's cost_bps."
        )

    return EdgeExecutionQuality(
        edge_id=edge_id,
        asset=asset,
        n_trades=n_trades,
        n_priced=n_priced,
        n_with_slippage=n_with_slippage,
        pct_with_slippage=100.0 * n_with_slippage / n_priced,
        mean_slippage_price=(float(real_slip.mean()) if n_with_slippage > 0 else 0.0),
        mean_swap_drag_r=float(swap_drag_r.mean()),
        mean_commission_drag_r=float(commission_drag_r.mean()),
        mean_fee_drag_r=float(fee_drag_r.mean()),
        mean_total_cost_drag_r=mean_total,
        total_cost_drag_r_sum=float(total_drag_r.sum()),
        assumed_cost_bps=RESEARCH_COST_BPS_BY_ASSET.get(asset),
        dominant_component=dominant,
        note=" ".join(note_parts),
    )


def explain_drift_vs_cost(drift_delta_r: float | None, cost_drag_r: float) -> str:
    """Root-cause framing: how much of a flagged realized_r drift could
    plausibly be explained by measured execution cost drag alone, vs. how
    much is unexplained (genuine behavioral/statistical drift)."""
    if drift_delta_r is None:
        return ""
    if abs(drift_delta_r) < 1e-6:
        return ""
    explained_frac = min(abs(cost_drag_r / drift_delta_r), 1.0) if drift_delta_r != 0 else 0.0
    unexplained = drift_delta_r - cost_drag_r if np.sign(cost_drag_r) == np.sign(drift_delta_r) else drift_delta_r
    return (
        f"Measured cost drag ({cost_drag_r:+.4f}R/trade) can explain about {explained_frac*100:.0f}% "
        f"of the observed realized_r drift ({drift_delta_r:+.4f}R). The remaining "
        f"{unexplained:+.4f}R is NOT explained by slippage/swap/commission -- it points to a real "
        "behavioral or statistical difference between live and backtest, not just execution friction."
        if explained_frac < 0.95
        else f"Measured cost drag ({cost_drag_r:+.4f}R/trade) alone can account for essentially all of the "
        f"observed realized_r drift ({drift_delta_r:+.4f}R) -- this looks like an execution-cost gap, not edge decay."
    )


if __name__ == "__main__":
    import json as _json

    from portfolio_live.drift_dashboard.data_sources import (
        load_current_live_edge_ids,
        scope_closed_trades,
    )

    edge_ids = load_current_live_edge_ids()
    scoped = scope_closed_trades(live_edge_ids=edge_ids)
    for eid in sorted(edge_ids):
        rows = scoped.in_scope[scoped.in_scope["edge_id"] == eid]
        if rows.empty:
            continue
        report = build_edge_execution_quality(eid, rows)
        print(_json.dumps(report.to_dict(), indent=2))
