"""
The statistical drift-detection core.

Purpose (see docs/dashboards/LIVE_VS_BACKTEST_DRIFT_DASHBOARD_PLAN.md):
answer "is this live-vs-backtest difference inside or outside what the
system's own tested variance predicts" with a real statistical test, not a
chart someone eyeballs while emotional. Uses two-sample Kolmogorov-Smirnov
(same test family as Gate D's Mann-Whitney U -- assumption-light,
rank/permutation-based, reports a p-value instead of a magic-number cutoff)
comparing the live realized sample against the actual Gate C per-trade
backtest sample (not just a summary CI).

Honesty requirement (non-negotiable): a verdict of "on_distribution" or
"drift_flagged" may only be issued when the live sample size supports real
statistical power. Below the minimum, the verdict must be
"insufficient_sample" -- never render a comparison as if it were meaningful
when the sample doesn't support the claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

# Minimum live sample size before ANY drift verdict is issued (not an
# arbitrary "nice" number -- chosen because a two-sample KS test at n=10
# has essentially no power to detect a moderate effect size, and issuing a
# verdict below this would manufacture false confidence in either direction).
MIN_SAMPLE_FOR_VERDICT = 10
# Below this, a verdict is technically issuable but explicitly flagged as
# low-power so the user knows to treat it cautiously rather than as final.
LOW_POWER_THRESHOLD = 30
ALPHA = 0.05


@dataclass
class MetricDriftResult:
    metric_name: str
    n_live: int
    n_backtest: int
    live_mean: float | None
    backtest_mean: float | None
    ks_statistic: float | None
    p_value: float | None
    verdict: str  # "insufficient_sample" | "low_power_on_distribution" | "low_power_drift_flagged" | "on_distribution" | "drift_flagged"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric_name,
            "n_live": self.n_live,
            "n_backtest": self.n_backtest,
            "live_mean": self.live_mean,
            "backtest_mean": self.backtest_mean,
            "drift_delta": (
                None
                if self.live_mean is None or self.backtest_mean is None
                else round(self.live_mean - self.backtest_mean, 6)
            ),
            "ks_statistic": self.ks_statistic,
            "p_value": self.p_value,
            "verdict": self.verdict,
            "note": self.note,
        }


def _clean(series: pd.Series) -> np.ndarray:
    arr = pd.to_numeric(series, errors="coerce").to_numpy(dtype="float64")
    return arr[np.isfinite(arr)]


def compare_metric(
    metric_name: str,
    live_values: pd.Series | np.ndarray,
    backtest_values: pd.Series | np.ndarray,
) -> MetricDriftResult:
    live_arr = _clean(pd.Series(live_values))
    backtest_arr = _clean(pd.Series(backtest_values))

    n_live = len(live_arr)
    n_backtest = len(backtest_arr)

    if n_live < MIN_SAMPLE_FOR_VERDICT or n_backtest < MIN_SAMPLE_FOR_VERDICT:
        return MetricDriftResult(
            metric_name=metric_name,
            n_live=n_live,
            n_backtest=n_backtest,
            live_mean=float(np.mean(live_arr)) if n_live > 0 else None,
            backtest_mean=float(np.mean(backtest_arr)) if n_backtest > 0 else None,
            ks_statistic=None,
            p_value=None,
            verdict="insufficient_sample",
            note=(
                f"Live n={n_live} (need >= {MIN_SAMPLE_FOR_VERDICT}) -- "
                "cannot distinguish drift from normal variance yet. This is "
                "not a green light; it means the data hasn't accumulated "
                "enough to tell either way."
            ),
        )

    ks_result = stats.ks_2samp(live_arr, backtest_arr)
    ks_stat = float(ks_result.statistic)
    p_value = float(ks_result.pvalue)
    drifted = p_value < ALPHA
    low_power = n_live < LOW_POWER_THRESHOLD

    if low_power:
        verdict = "low_power_drift_flagged" if drifted else "low_power_on_distribution"
        note = (
            f"Live n={n_live} is below the low-power threshold "
            f"({LOW_POWER_THRESHOLD}) -- verdict is directionally informative "
            "but treat cautiously; more live trades will sharpen this."
        )
    else:
        verdict = "drift_flagged" if drifted else "on_distribution"
        note = ""

    return MetricDriftResult(
        metric_name=metric_name,
        n_live=n_live,
        n_backtest=n_backtest,
        live_mean=float(np.mean(live_arr)),
        backtest_mean=float(np.mean(backtest_arr)),
        ks_statistic=ks_stat,
        p_value=p_value,
        verdict=verdict,
        note=note,
    )


@dataclass
class EdgeDriftReport:
    edge_id: str
    metrics: dict[str, MetricDriftResult] = field(default_factory=dict)
    gap: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "gap": self.gap,
            "metrics": {name: m.to_dict() for name, m in self.metrics.items()},
        }


def build_edge_drift_report(
    edge_id: str,
    live_trades: pd.DataFrame,
    backtest_trades: pd.DataFrame | None,
    gap: str | None,
) -> EdgeDriftReport:
    """live_trades: this edge's rows from ScopedTrades.in_scope (must already
    be filtered to this edge_id and to the current live book).
    backtest_trades: the Gate C per-trade sample matching this edge's
    CURRENT geometry (from edge_reference.resolve_backtest_trade_sample),
    or None if unavailable (gap must then be non-None)."""
    if backtest_trades is None:
        return EdgeDriftReport(edge_id=edge_id, gap=gap or "no_backtest_reference_available")

    report = EdgeDriftReport(edge_id=edge_id)

    if "realized_r" in live_trades.columns and "exec_r" in backtest_trades.columns:
        report.metrics["realized_r"] = compare_metric(
            "realized_r", live_trades["realized_r"], backtest_trades["exec_r"]
        )
    if "hold_hours" in live_trades.columns and "hold_hours" in backtest_trades.columns:
        report.metrics["hold_hours"] = compare_metric(
            "hold_hours", live_trades["hold_hours"], backtest_trades["hold_hours"]
        )
    return report


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    _V2_ROOT = Path(__file__).resolve().parents[2]
    if str(_V2_ROOT) not in sys.path:
        sys.path.insert(0, str(_V2_ROOT))

    from portfolio_live.drift_dashboard.data_sources import (
        load_current_live_edge_ids,
        scope_closed_trades,
    )
    from portfolio_live.drift_dashboard.edge_reference import resolve_backtest_trade_sample

    edge_ids = load_current_live_edge_ids()
    scoped = scope_closed_trades(live_edge_ids=edge_ids)

    for eid in sorted(edge_ids):
        live_rows = scoped.in_scope[scoped.in_scope["edge_id"] == eid]
        bt = resolve_backtest_trade_sample(eid)
        report = build_edge_drift_report(eid, live_rows, bt.get("trades"), bt.get("gap"))
        print(f"=== {eid} ===")
        print(json.dumps(report.to_dict(), indent=2, default=str))
        print()
