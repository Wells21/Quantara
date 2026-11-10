"""
Portfolio-level live-vs-backtest reference for the drift dashboard.

Answers the specific emotional question that motivated this dashboard:
"is the CURRENT account drawdown/buffer-above-floor inside or outside what
the portfolio_allocator's own historical simulation already expected?"

Two backtest reference sources (see docs/dashboards/LIVE_VS_BACKTEST_DRIFT_DASHBOARD_PLAN.md):
- portfolio_allocator/outputs/backtest_statistics_outputs/backtest_statistics_report.csv
  -- the PORTFOLIO(...) row: max_drawdown_r, p95_drawdown_r (R-unit, historical
  return-stream based).
- portfolio_allocator/outputs/monte_carlo_stress_outputs/historical_bootstrap_summary.csv
  -- start-month cluster-bootstrap resampling of real historical return
  streams, with honest median/p025/p975 uncertainty bounds on
  worst_buffer_above_floor -- the best available reference distribution.

Live side: reuses portfolio_live/common.py::init_mt5() for real-time
account.balance / account.equity (best-effort, must degrade honestly, never
silently show stale numbers as if live).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_PORTFOLIO_LIVE_DIR = _THIS_FILE.parents[1]
_V2_ROOT = _THIS_FILE.parents[2]
for _path in (_PORTFOLIO_LIVE_DIR, _V2_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

import live_settings as settings  # noqa: E402
from shared.prop_account_rules import (  # noqa: E402
    drawdown_stats,
    load_active_account_rules,
    overall_drawdown_floor,
)

BACKTEST_STATS_REPORT = (
    _V2_ROOT / "portfolio_allocator" / "outputs" / "backtest_statistics_outputs" / "backtest_statistics_report.csv"
)
HISTORICAL_BOOTSTRAP_SUMMARY = (
    _V2_ROOT / "portfolio_allocator" / "outputs" / "monte_carlo_stress_outputs" / "historical_bootstrap_summary.csv"
)
EXACT_MONTE_CARLO_SUMMARY = (
    _V2_ROOT / "portfolio_allocator" / "outputs" / "monte_carlo_stress_outputs" / "exact_monte_carlo_summary.csv"
)
EXECUTOR_STATE_PATH = settings.LIVE_STATE_DIR / "portfolio_execution_bot_state.json"


class PortfolioReferenceError(RuntimeError):
    pass


@dataclass
class LiveAccountSnapshot:
    balance: float
    equity: float
    source: str  # "live_mt5" | "last_known_state_file"
    as_of: str | None = None
    warning: str | None = None


def get_live_account_snapshot() -> LiveAccountSnapshot:
    """Best-effort real-time balance/equity via a fresh MT5 connection.
    Read-only (account_info only) -- safe to run alongside the already-
    running dashboard/executor, which independently hold their own MT5
    sessions. Degrades honestly on failure rather than fabricating a number."""
    try:
        from portfolio_live.common import init_mt5

        mt5, account = init_mt5()
        snapshot = LiveAccountSnapshot(
            balance=float(account.balance),
            equity=float(account.equity),
            source="live_mt5",
        )
        try:
            mt5.shutdown()
        except Exception:
            pass
        return snapshot
    except Exception as exc:
        return _fallback_account_snapshot(warning=f"Live MT5 connection failed ({exc}); showing last known state.")


def _fallback_account_snapshot(warning: str) -> LiveAccountSnapshot:
    if not EXECUTOR_STATE_PATH.exists():
        raise PortfolioReferenceError(
            f"No live MT5 connection AND no fallback state file at {EXECUTOR_STATE_PATH}"
        )
    with open(EXECUTOR_STATE_PATH, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    risk_state = state.get("prop_risk_state", {})
    hwm = risk_state.get("overall_high_watermark")
    if hwm is None:
        raise PortfolioReferenceError("Fallback state file has no prop_risk_state.overall_high_watermark")
    updated_at = state.get("updated_at")
    # Fallback approximation only: treat last-known watermark as both
    # balance and equity proxies (this is intentionally conservative/labeled,
    # not a substitute for a real live read).
    return LiveAccountSnapshot(
        balance=float(hwm),
        equity=float(hwm),
        source="last_known_state_file",
        as_of=updated_at,
        warning=warning,
    )


@dataclass
class PortfolioDrawdownReference:
    current_balance: float
    current_equity: float
    account_source: str
    account_as_of: str | None
    account_warning: str | None
    current_floor: float
    current_buffer_above_floor: float
    high_watermark: float
    backtest_max_drawdown_r: float | None
    backtest_p95_drawdown_r: float | None
    backtest_max_time_under_water_years: float | None
    backtest_p95_time_under_water_years: float | None
    bootstrap_reference_rows: list[dict[str, Any]]
    exact_mc_reference_rows: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_balance": self.current_balance,
            "current_equity": self.current_equity,
            "account_source": self.account_source,
            "account_as_of": self.account_as_of,
            "account_warning": self.account_warning,
            "current_floor": self.current_floor,
            "current_buffer_above_floor": self.current_buffer_above_floor,
            "high_watermark": self.high_watermark,
            "backtest_max_drawdown_r": self.backtest_max_drawdown_r,
            "backtest_p95_drawdown_r": self.backtest_p95_drawdown_r,
            "backtest_max_time_under_water_years": self.backtest_max_time_under_water_years,
            "backtest_p95_time_under_water_years": self.backtest_p95_time_under_water_years,
            "bootstrap_reference_rows": self.bootstrap_reference_rows,
            "exact_mc_reference_rows": self.exact_mc_reference_rows,
        }


def _load_portfolio_backtest_stats() -> dict[str, Any] | None:
    if not BACKTEST_STATS_REPORT.exists():
        return None
    df = pd.read_csv(BACKTEST_STATS_REPORT)
    portfolio_rows = df[df["name"].astype(str).str.startswith("PORTFOLIO(")]
    if portfolio_rows.empty:
        return None
    return portfolio_rows.iloc[0].to_dict()


def _load_bootstrap_reference_rows() -> list[dict[str, Any]]:
    if not HISTORICAL_BOOTSTRAP_SUMMARY.exists():
        return []
    df = pd.read_csv(HISTORICAL_BOOTSTRAP_SUMMARY)
    return df.to_dict(orient="records")


def _load_exact_mc_reference_rows() -> list[dict[str, Any]]:
    if not EXACT_MONTE_CARLO_SUMMARY.exists():
        return []
    df = pd.read_csv(EXACT_MONTE_CARLO_SUMMARY)
    return df.to_dict(orient="records")


def build_portfolio_drawdown_reference() -> PortfolioDrawdownReference:
    account_rules = load_active_account_rules()
    snapshot = get_live_account_snapshot()

    stats = drawdown_stats(account_rules, balance=snapshot.balance, high_watermark=None)
    floor = float(stats["drawdown_floor"])
    high_watermark = float(stats["trailing_high_watermark"])
    buffer_above_floor = float(stats["remaining_dd_buffer"])

    portfolio_bt = _load_portfolio_backtest_stats()

    return PortfolioDrawdownReference(
        current_balance=snapshot.balance,
        current_equity=snapshot.equity,
        account_source=snapshot.source,
        account_as_of=snapshot.as_of,
        account_warning=snapshot.warning,
        current_floor=floor,
        current_buffer_above_floor=buffer_above_floor,
        high_watermark=high_watermark,
        backtest_max_drawdown_r=(float(portfolio_bt["max_drawdown_r"]) if portfolio_bt else None),
        backtest_p95_drawdown_r=(float(portfolio_bt["p95_drawdown_r"]) if portfolio_bt else None),
        backtest_max_time_under_water_years=(
            float(portfolio_bt["max_time_under_water_years"]) if portfolio_bt else None
        ),
        backtest_p95_time_under_water_years=(
            float(portfolio_bt["p95_time_under_water_years"]) if portfolio_bt else None
        ),
        bootstrap_reference_rows=_load_bootstrap_reference_rows(),
        exact_mc_reference_rows=_load_exact_mc_reference_rows(),
    )


if __name__ == "__main__":
    ref = build_portfolio_drawdown_reference()
    print(json.dumps(ref.to_dict(), indent=2, default=str))
