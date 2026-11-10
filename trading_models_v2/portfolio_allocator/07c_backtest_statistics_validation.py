"""
V2 Phase B2.07C -- Backtest Statistics (AFML Ch.14) and Understanding
Strategy Risk (AFML Ch.15) validation.

Applies shared/backtest_statistics/afml_ch14_ch15_reference.py's canonical
formulas to the 21 currently-deployed edges' realized daily return streams
(the same Step 06/07 artifacts 07b_dsr_psr_validation.py uses) and to the
HRP portfolio return stream:

  Returns Concentration (AFML SS14.5.1, p.199-200): HHI on positive returns,
  negative returns, and time-between-bets. A strategy whose profitability
  concentrates in a handful of trades (or a handful of active months) is a
  different, and generally worse, risk profile than one with broadly
  distributed returns, even at the same total P&L.

  Drawdown and Time under Water (AFML SS14.5.2, p.201-202): 95th-percentile
  drawdown and 95th-percentile time-under-water, computed on each series'
  own real calendar-time equity curve (drawdown/TuW are inherently
  calendar-time concepts, unlike the HHI/strategy-risk statistics below,
  which use only real trade/bet observations).

  Understanding Strategy Risk (AFML Ch.15, p.211-219): given each edge's own
  empirical win rate, average win/loss, and betting frequency, bin_hr gives
  the minimum precision needed to sustain a target Sharpe ratio, and
  prob_strategy_failure estimates the probability the true precision falls
  short of that bar -- i.e. the probability this edge's apparent
  profitability is not real. The target Sharpe used is each target's OWN
  observed annualized Sharpe ratio (a self-consistency check: would this
  edge's own precision reliably reproduce its own observed performance
  going forward), not an arbitrary external constant.

This is a diagnostic, not a gate -- consistent with every other lab in this
project (shared/independence/asset_independence_lab.py,
portfolio_allocator/07b_dsr_psr_validation.py). It does not kill, promote,
or reweight edges.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for p in (V2_ROOT, REPO_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from shared.backtest_statistics.afml_ch14_ch15_reference import (  # noqa: E402
    attribution_vs_universe,
    bet_concentration,
    bet_level_returns,
    classification_scores,
    cost_split_stats,
    regime_coverage,
    sizing_and_capacity_stats,
    target_positions_from_trades,
    correlation_to_underlying,
    drawdown_time_under_water_stats,
    general_and_performance_stats,
    implementation_shortfall_stats,
    returns_concentration,
    prob_strategy_failure,
    strategy_risk_report,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

SCHEMA_VERSION = "v1.0"
RETURN_STREAMS_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "return_streams"
HRP_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "hrp_linkage_robustness_outputs"
OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "backtest_statistics_outputs"

INPUT_EDGE_RETURNS = (
    RETURN_STREAMS_DIR / "profile_return_streams_daily_core_plus_watchlist_compound_velocity.csv"
)
# AFML Ch.14 walkthrough 2026-10-26: trade-level inputs for s.14.3 / 14.4 / 14.6 / 14.9 (bets, not trades).
INPUT_TRADE_LEDGER = V2_ROOT / "portfolio_allocator" / "outputs" / "v2_representative_trade_ledger_core_plus_watchlist.csv"
SIZING_BALANCE = 100_000.00  # 04C CURRENT_BALANCE: the edge's standalone sizing account (s.14.3 AUM / leverage / turnover)
# The deployed HRP scenario (single linkage, 07 CANONICAL_METHOD); was 'average' until the AFML Ch.16 walkthrough (2026-10-30).
DEFAULT_PORTFOLIO_RETURNS = (
    HRP_DIR / "hrp_portfolio_returns_raw_r_core_plus_watchlist_compound_velocity_single.csv"
)

ANNUALIZATION_DAYS_PER_YEAR = 365.25
# AFML Ch.15 walkthrough 2026-10-27 (D055): theta*, k, gate and bootstrap settings live in one file.
STRATEGY_RISK_CONFIG = V2_ROOT / "config" / "strategy_risk.json"


def load_strategy_risk_config() -> dict[str, Any]:
    return json.loads(STRATEGY_RISK_CONFIG.read_text(encoding="utf-8"))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def load_daily_frame(path: Path, value_cols: list[str] | None = None) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing return stream: {path}")
    df = pd.read_csv(path, float_precision="round_trip")
    if "date" not in df.columns:
        raise ValueError(f"{path} must contain a date column")
    df["date"] = to_datetime_iso(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    if value_cols is not None:
        df = df[value_cols]
    return df


def active_frequency(dates_active: pd.DatetimeIndex, n_active: int) -> float:
    """Same convention as 07b_dsr_psr_validation.py: Frequency = T/Years over
    the series' own first-to-last active span (MLAM p.112)."""
    if n_active < 2 or len(dates_active) < 2:
        return float("nan")
    years = (dates_active.max() - dates_active.min()).days / ANNUALIZATION_DAYS_PER_YEAR
    if years <= 0:
        return float("nan")
    return float(n_active / years)


def sr_nonannualized(r: np.ndarray) -> float:
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return float("nan")
    std = float(np.std(r, ddof=1))
    return float(np.mean(r) / std) if std > 0 else float("nan")


def evaluate_target(name: str, daily_series: pd.Series) -> dict[str, Any]:
    active = daily_series[daily_series != 0.0]
    active_r = active.to_numpy(dtype="float64")
    active_dates = active.index

    concentration = returns_concentration(active_r, active_dates)
    equity = daily_series.cumsum()
    dd_tuw = drawdown_time_under_water_stats(equity)

    freq = active_frequency(active_dates, len(active_r))
    # AFML Ch.15 strategy risk moved to evaluate_edge_general (on BETS, s.15.4.1 bootstrap) -- 2026-10-27, D055.
    row: dict[str, Any] = {"name": name, "n_active_observations": len(active_r), "active_days_per_year": freq}
    row.update({f"{k}_active_days_proxy": v for k, v in concentration.items()})  # s.14.5.1 on bets: general report
    row.update(dd_tuw)
    return row


def strategy_risk_rows(trades: pd.DataFrame, bets: pd.DataFrame, cfg: dict[str, Any]) -> dict[str, Any]:
    """AFML s.15.4.1 on BETS (primary unit, D055) and on trades (alongside), for the configured theta* / k and the
    report-alongside values."""
    years = (trades["exit_time"].max() - trades["entry_time"].min()).total_seconds() / (365.25 * 86400.0)
    boot = cfg["bootstrap"]
    out: dict[str, Any] = {"sr_theta_star": cfg["theta_star"], "sr_k_years": cfg["k_years"],
                           "sr_theta_k_status": cfg.get("theta_k_status", "")}
    combos = [(cfg["theta_star"], cfg["k_years"], "primary")]
    combos += [(t, cfg["k_years"], f"theta{t:g}") for t in cfg["report_alongside"]["theta_star"]]
    combos += [(cfg["theta_star"], k, f"k{k:g}") for k in cfg["report_alongside"]["k_years"]]
    for unit, x in (("bets", bets["r"].to_numpy(float)), ("trades", trades["executed_r"].to_numpy(float))):
        for theta, k, tag in combos:
            full = tag == "primary" or tag.startswith("theta")  # exercise 15.1-15.2 sensitivities for each target theta
            rep = (strategy_risk_report if full else prob_strategy_failure)(
                x, years, theta, k, n_boot=int(boot["n_boot"]), seed=int(boot["seed"]))
            keep = rep if tag == "primary" else {kk: vv for kk, vv in rep.items()
                                                 if kk in ("p_theta_star", "prob_failure", "passes_0p05") or kk.startswith("sens_")}
            out.update({f"sr_{unit}_{tag}_{kk}": vv for kk, vv in keep.items()})
    out["strategy_risk_gate_pass_bets"] = bool(out.get("sr_bets_primary_prob_failure", 1.0) <= cfg["gate"]["max_prob_failure"])
    return out


def load_edge_trades(ledger_path: Path, edge_id: str) -> pd.DataFrame:
    cols = ["edge_id", "side_value", "entry_time", "exit_time", "executed_r", "afml_bet_size", "entry_price",
            "stop_distance_price", "risk_per_1_lot", "broker_min_lot", "broker_lot_step", "broker_max_lot",
            "representative_threshold"]
    df = pd.read_csv(ledger_path, usecols=lambda c: c in set(cols), float_precision="round_trip")
    df = df[df["edge_id"] == edge_id].copy()
    df["entry_time"] = to_datetime_iso(df["entry_time"], utc=True, errors="coerce")
    df["exit_time"] = to_datetime_iso(df["exit_time"], utc=True, errors="coerce")
    df["side"] = pd.to_numeric(df["side_value"], errors="coerce")
    return df.dropna(subset=["entry_time", "exit_time", "side", "executed_r"]).sort_values("entry_time").reset_index(drop=True)


UNIVERSE_COLS = {"entry_time", "label_r", "Event_Vol", "Regime_State", "live_calibrated_probability",
                 "quote_executable_meta_label", "executed_r", "executed_r_pre_carry"}


def load_universe(edge_id: str) -> pd.DataFrame:
    """Every primary event of the edge (Step 04 calibrated OOF): the 'universe' of opportunities the meta-model filters."""
    asset, edge = edge_id.split("/", 1)
    files = sorted((V2_ROOT / "assets" / asset / "edges" / edge / "artifacts" / "calibration").glob("*_calibrated_oof_predictions.csv"))
    if not files:
        return pd.DataFrame(columns=sorted(UNIVERSE_COLS))
    oof = pd.read_csv(files[0], usecols=lambda c: c in UNIVERSE_COLS, float_precision="round_trip")
    oof["entry_time"] = to_datetime_iso(oof["entry_time"], utc=True, errors="coerce")
    return oof.dropna(subset=["entry_time"]).reset_index(drop=True)


def join_calibrated_oof(trades: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    """Mid-path label R (label_r, the firm-neutral mid outcome) and Event_Vol from the edge's Step 04 calibrated OOF,
    matched on entry_time."""
    u = universe[["entry_time", "label_r", "Event_Vol"]].drop_duplicates("entry_time")
    return trades.merge(u, on="entry_time", how="left")


def representative_sizing_f(edge_id: str) -> float:
    path = V2_ROOT / "portfolio_allocator" / "outputs" / "v2_representative_threshold_candidates_core_plus_watchlist.csv"
    if not path.exists():
        return float("nan")
    reps = pd.read_csv(path, usecols=["edge_id", "broker_optimized_f"])
    vals = reps.loc[reps["edge_id"] == edge_id, "broker_optimized_f"]
    return float(vals.iloc[0]) if len(vals) else float("nan")


def underlying_close(asset: str) -> pd.Series:
    path = V2_ROOT / "assets" / asset / "bars" / "standard_quote_tick" / f"{asset.lower()}_standard_quote_tick_bars.csv"
    bars = pd.read_csv(path, usecols=["Datetime", "Close"], float_precision="round_trip")
    bars["Datetime"] = to_datetime_iso(bars["Datetime"], utc=True, errors="coerce")
    return bars.dropna().set_index("Datetime")["Close"].sort_index()


def session_of(times: pd.Series) -> np.ndarray:
    hour = times.dt.hour
    return np.select([hour < 8, hour < 16], ["00-08", "08-16"], default="16-24")


def evaluate_edge_general(edge_id: str, ledger_path: Path) -> tuple[dict[str, Any], pd.DataFrame]:
    """AFML Ch.14 s.14.3 / 14.4 / 14.5 / 14.6 / 14.8 / 14.9 on the edge's representative trades and its primary-event
    universe (owner-approved 2026-10-26; second pass 2026-10-27)."""
    trades = load_edge_trades(ledger_path, edge_id)
    row: dict[str, Any] = {"name": edge_id}
    if trades.empty:
        return row, pd.DataFrame()
    row.update(general_and_performance_stats(trades))
    bets = bet_level_returns(trades, target_positions_from_trades(trades))
    row.update(bet_concentration(bets))
    row.update(strategy_risk_rows(trades, bets, load_strategy_risk_config()))
    row.update(sizing_and_capacity_stats(trades, representative_sizing_f(edge_id), SIZING_BALANCE))
    daily = trades.set_index("exit_time")["executed_r"]
    try:
        row.update(correlation_to_underlying(daily, underlying_close(edge_id.split("/", 1)[0])))
    except FileNotFoundError:
        row.update({"corr_to_underlying": float("nan"), "corr_p_value": float("nan"), "corr_days": 0})

    universe = load_universe(edge_id)
    joined = join_calibrated_oof(trades, universe)
    row.update(implementation_shortfall_stats(joined["executed_r"].to_numpy(), joined["label_r"].to_numpy()))
    threshold = float(trades["representative_threshold"].iloc[0]) if "representative_threshold" in trades else float("nan")
    row["live_threshold"] = threshold
    attr = pd.DataFrame()
    if not universe.empty and np.isfinite(threshold):
        taken = universe["live_calibrated_probability"].to_numpy(float) >= threshold - 1e-12
        row["universe_taken_vs_ledger_trades"] = f"{int(taken.sum())} / {len(trades)}"
        row.update(cost_split_stats(universe.loc[taken]))
        row.update({f"cls_{k}": v for k, v in classification_scores(
            universe["quote_executable_meta_label"].to_numpy(), universe["live_calibrated_probability"].to_numpy(), threshold).items()})
        row.update(regime_coverage(universe, taken))
        universe = universe.assign(session_utc=session_of(universe["entry_time"]),
                                   event_vol_tercile=pd.qcut(universe["Event_Vol"], 3, labels=["low", "mid", "high"]).astype(str))
        attr = attribution_vs_universe(universe, taken, ["session_utc", "event_vol_tercile", "Regime_State"])
        attr.insert(0, "name", edge_id)
    return row, attr


def build(
    *,
    write: bool = True,
    edge_returns_path: Path = INPUT_EDGE_RETURNS,
    portfolio_returns_path: Path = DEFAULT_PORTFOLIO_RETURNS,
    trade_ledger_path: Path = INPUT_TRADE_LEDGER,
) -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    edge_returns = load_daily_frame(edge_returns_path)
    edge_ids = list(edge_returns.columns)

    rows = [evaluate_target(edge_id, edge_returns[edge_id]) for edge_id in edge_ids]
    general = [evaluate_edge_general(edge_id, trade_ledger_path) for edge_id in edge_ids]
    general_df = pd.DataFrame([g[0] for g in general])
    attribution_df = pd.concat([g[1] for g in general if not g[1].empty], ignore_index=True) if general else pd.DataFrame()

    portfolio_df = load_daily_frame(portfolio_returns_path, value_cols=["hrp_portfolio_r"])
    rows.append(evaluate_target(
        "PORTFOLIO(hrp_single_core_plus_watchlist_raw_r)",
        portfolio_df["hrp_portfolio_r"],
    ))

    report_df = pd.DataFrame(rows).sort_values("name")
    gate_col = "strategy_risk_gate_pass_bets"
    n_high_failure_risk = int((~general_df[gate_col].astype(bool)).sum()) if gate_col in general_df else 0
    n_concentration_flag = int(
        ((general_df.get("hhi_positive_bets", pd.Series(dtype=float)) > 0.30)
         | (general_df.get("hhi_bets_per_month", pd.Series(dtype=float)) > 0.30)).sum()
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "status": "PASS",
        "input_edge_returns": str(edge_returns_path.relative_to(V2_ROOT)),
        "input_portfolio_returns": str(portfolio_returns_path.relative_to(V2_ROOT)),
        "n_targets_evaluated": int(len(report_df)),
        "n_edges_failing_strategy_risk_0p05_on_bets": n_high_failure_risk,
        "strategy_risk_config": str(STRATEGY_RISK_CONFIG.relative_to(V2_ROOT)),
        "n_targets_hhi_concentration_flag_above_0p30": n_concentration_flag,
        "methodology": {
            "returns_concentration": "AFML SS14.5.1 (p.199-200): HHI on positive returns, negative returns, and monthly bet counts. 0=uniform, 1=fully concentrated.",
            "drawdown_time_under_water": "AFML SS14.5.2 (p.201-202): 95th-percentile drawdown (R units) and 95th-percentile time-under-water (years), computed on each series' own calendar-time equity curve.",
            "strategy_risk": "AFML Ch.15 s.15.4.1 (p.217) on BET outcomes (D055): pi- = E[pi | pi <= 0], pi+ = E[pi | pi > 0], n = bets / year, p_theta* = binHR (Snippet 15.3); P[p < p_theta*] by bootstrapping floor(n k) bets with replacement and a Gaussian KDE; s.15.4.2 rule P <= .05; theta*, k in config/strategy_risk.json; trades reported alongside; exercise 15.1-15.2 sensitivities (required p, n, PT, SL; elasticities). The Snippet 15.5 shortcut is reported only as a flagged diagnostic.",
            "general_performance": "AFML s.14.3-14.4 (p.196-199), per BET (Snippet 14.1: a same-side run of positions is one bet; Snippet 14.2 holding period): time range, bets, bets/year, holding period, ratio of longs, max position, PnL, PnL from longs, R per year, hit ratio, average hit / miss. R units replace dollars.",
            "correlation_to_underlying": "AFML p.197: Pearson correlation of daily strategy R (by exit day, 0 on days without exits) with the underlying's daily log return (last bar close per UTC day).",
            "implementation_shortfall": "AFML s.14.6 (p.202-203) in R: execution cost per trade = mid-path label_r - executed_r (Step 04 calibrated OOF, matched on entry_time); return on execution costs = net R / total cost.",
            "attribution": "AFML s.14.9 (p.207-208): portfolio (taken events) vs universe (every primary event) index return per disjoint category of UTC entry session, Event_Vol tercile and HMM regime.",
            "not_a_gate": "This lab is diagnostic only. It does not kill, promote, or reweight edges.",
        },
        "outputs": {
            "general_performance": str(OUTPUT_DIR / "backtest_general_performance_report.csv"),
            "attribution": str(OUTPUT_DIR / "backtest_attribution_report.csv"),
            "report": str(OUTPUT_DIR / "backtest_statistics_report.csv"),
            "manifest": str(OUTPUT_DIR / "backtest_statistics_manifest.json"),
        },
    }

    if write:
        report_df.to_csv(OUTPUT_DIR / "backtest_statistics_report.csv", index=False)
        general_df.to_csv(OUTPUT_DIR / "backtest_general_performance_report.csv", index=False)
        attribution_df.to_csv(OUTPUT_DIR / "backtest_attribution_report.csv", index=False)
        write_json(OUTPUT_DIR / "backtest_statistics_manifest.json", manifest)

    return manifest


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V2 Phase B2.07C AFML Ch.14/15 backtest-statistics validation.")
    parser.add_argument("--edge-returns", default=str(INPUT_EDGE_RETURNS))
    parser.add_argument("--portfolio-returns", default=str(DEFAULT_PORTFOLIO_RETURNS))
    parser.add_argument("--trade-ledger", default=str(INPUT_TRADE_LEDGER))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    manifest = build(
        write=not args.dry_run,
        edge_returns_path=Path(args.edge_returns),
        portfolio_returns_path=Path(args.portfolio_returns),
        trade_ledger_path=Path(args.trade_ledger),
    )
    print("V2 Phase B2.07C AFML Ch.14/15 backtest-statistics validation")
    print(f"Status: {manifest['status']}")
    print(f"Targets evaluated: {manifest['n_targets_evaluated']}")
    print(f"Edges failing the Ch.15 strategy-risk rule (bets, P > .05): {manifest['n_edges_failing_strategy_risk_0p05_on_bets']}")
    print(f"Targets with HHI concentration flag: {manifest['n_targets_hhi_concentration_flag_above_0p30']}")
    for name, path in manifest["outputs"].items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
