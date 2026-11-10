"""Decommission / shrink rule, applied automatically by the execution bot.

AFML Ch.1 s.1.3.1.6 (p.8-9): re-allocation ("over time, performance decays, and
allocations become gradually smaller") and decommission ("perform below
expectations for a sufficiently extended period of time to conclude that the
supporting theory is no longer backed by empirical evidence"). The statistic is
the Probabilistic Sharpe Ratio, AFML s.14.7.2 (p.203), judged at the 0.95 level
(p.205-206). Quantara choices (D019, D021): no ramp-up, 5 shrink levels, 30-bet
minimum sample.

Edges come from live_manifest.json; bets count only when placed by the edge's
current certified version (edge_versions.py registry). A "bet" is a same-side run of
overlapping trades (AFML Ch.14 p.196, Snippet 14.1; D056, owner 2026-10-27): PSRs and
the minimum sample use bets, both live and in the backtest benchmark. Per edge:
  PSR[0]        <= 0.05  -> off  (95% confident the edge now loses money)
  PSR[SR_bt]    bands    -> 100/80/60/40/20% of allocator weight
where SR_bt is the per-bet Sharpe of the meta-approved backtest bets at the live
threshold (AFML p.211: strategy risk is measured on bets actually taken).

Live use (approved 2026-10-23): portfolio_execution_bot.build_order_plan calls
live_allocations() and multiplies each edge's risk by its allocation; an "off" edge
places no orders and its approved signals become shadow bets (shadow_bets.py, D022),
which count as evidence so the edge can come back. Off is never permanent.

Run from trading_models_v2:
    python ./portfolio_live/decommission_rule.py
    python ./portfolio_live/decommission_rule.py --bets-csv holdout_bets.csv --sample-label holdout
"""
from __future__ import annotations

import argparse
import json
import sys
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

V2_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import edge_versions  # noqa: E402
import shadow_bets  # noqa: E402
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))
from shared.backtest_statistics import afml_ch14_ch15_reference as ch15  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

CONFIG_PATH = V2_ROOT / "portfolio_live" / "config" / "decommission_rule.json"
OUTPUT_DIR = V2_ROOT / "portfolio_live" / "outputs" / "decommission"


def probabilistic_sharpe_ratio(returns: np.ndarray, sr_benchmark: float) -> tuple[float, float]:
    """AFML s.14.7.2 (p.203): PSR[SR*] = Z[(SR - SR*) sqrt(T-1) / sqrt(1 - g3 SR + (g4-1)/4 SR^2)].

    SR is per-bet (non-annualised, as the book prescribes); g4 is plain kurtosis (3 = Gaussian).
    Returns (psr, observed_sr).
    """
    x = np.asarray(returns, dtype="float64")
    x = x[np.isfinite(x)]
    if len(x) < 2:
        raise ValueError("PSR needs at least 2 returns")
    sd = x.std(ddof=1)
    if not sd > 0:
        raise ValueError("PSR needs non-constant returns")
    sr = float(x.mean() / sd)
    g3 = float(stats.skew(x))
    g4 = float(stats.kurtosis(x, fisher=False))
    denom = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    if not denom > 0:
        raise ValueError(f"PSR denominator not positive ({denom})")
    z = (sr - sr_benchmark) * math.sqrt(len(x) - 1) / math.sqrt(denom)
    return float(stats.norm.cdf(z)), sr


STRATEGY_RISK_CONFIG_PATH = V2_ROOT / "config" / "strategy_risk.json"


def to_bets(trades: pd.DataFrame) -> np.ndarray:
    """AFML Ch.14 p.196 / Snippet 14.1: a same-side run of overlapping positions is ONE bet (outcome = summed R). Rows
    without entry / exit times or side count as one bet each (e.g. resolved shadow bets without timestamps)."""
    need = {"entry_time", "exit_time", "side", "r"}
    if trades.empty:
        return np.array([], dtype=float)
    if not need.issubset(trades.columns):
        return trades["r"].to_numpy(float)
    t = trades.copy()
    t["entry_time"] = to_datetime_iso(t["entry_time"], utc=True, errors="coerce")
    t["exit_time"] = to_datetime_iso(t["exit_time"], utc=True, errors="coerce")
    t["side"] = pd.to_numeric(t["side"], errors="coerce")
    timed = t.dropna(subset=["entry_time", "exit_time", "side", "r"])
    untimed = t.drop(timed.index)["r"].to_numpy(float)
    if timed.empty:
        return untimed
    timed = timed.rename(columns={"r": "executed_r"})
    bet_r = ch15.bet_level_returns(timed, ch15.target_positions_from_trades(timed))["r"].to_numpy(float)
    return np.concatenate([bet_r, untimed])


def strategy_risk_diagnostic(live_bets: np.ndarray, backtest_bets: np.ndarray, backtest_years: float,
                             cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """AFML s.15.4 (pp.216-217), live precision against p_theta* (owner-approved 2026-10-27, diagnostic first; D055).
    Design side: pi-, pi+, n from the meta-approved BACKTEST bets, p_theta* = binHR(pi-, pi+, n, theta*) (Snippet 15.3).
    Live side: precision of the live bets and P[p < p_theta*] by the s.15.4.1 bootstrap of the live bets (floor(n k) =
    the live bet count: the assessment window is the live sample itself) with a Gaussian KDE. Allocation is NOT driven
    by it."""
    cfg = cfg or json.loads(STRATEGY_RISK_CONFIG_PATH.read_text(encoding="utf-8"))
    live = np.asarray(live_bets, dtype=float)
    live = live[np.isfinite(live)]
    design = ch15.bet_outcome_parameters(backtest_bets, backtest_years)
    if live.size < 2 or not (np.isfinite(design["pi_plus"]) and np.isfinite(design["pi_minus"]) and design["n"] > 0):
        return None
    theta = float(cfg["theta_star"])
    p_star = ch15.bin_hr(design["pi_minus"], design["pi_plus"], design["n"], theta)
    boot = cfg["bootstrap"]
    risk = ch15.prob_precision_below(live, p_star, live.size, n_boot=int(boot["n_boot"]), seed=int(boot["seed"]))
    return {"theta_star": theta, "backtest_bet_precision": design["p"], "p_theta_star": p_star,
            "live_bets": int(live.size), "live_bet_precision": float((live > 0).mean()),
            "live_precision_margin": float((live > 0).mean() - p_star), "prob_live_precision_below_p_theta_star": risk}


def allocation_for(psr_vs_backtest: float, levels: list[dict[str, float]]) -> float:
    for level in levels:  # ordered high to low
        if psr_vs_backtest > level["psr_vs_backtest_above"]:
            return float(level["allocation"])
    raise ValueError("shrink_levels must end with a catch-all level")


@dataclass
class EdgeVerdict:
    edge_id: str
    sample_label: str
    n_bets: int
    verdict: str  # insufficient_sample | full | reduced | off | blocked
    allocation: float | None
    mean_r: float | None
    observed_sr_per_bet: float | None
    backtest_sr_per_bet: float | None
    backtest_n_bets: int | None
    psr_vs_zero: float | None
    psr_vs_backtest: float | None
    strategy_risk_diagnostic: dict[str, Any] | None  # AFML s.15.4 live precision vs p_theta* (D055), not allocation
    note: str = ""
    n_shadow_bets: int = 0
    n_trades: int = 0  # D056: n_bets counts Snippet 14.1 bets; n_trades the underlying trades


def decide(edge_id: str, bets: np.ndarray, backtest: np.ndarray, cfg: dict[str, Any], sample_label: str) -> EdgeVerdict:
    _, sr_bt = probabilistic_sharpe_ratio(backtest, 0.0)
    base = dict(edge_id=edge_id, sample_label=sample_label, n_bets=int(len(bets)),
                backtest_sr_per_bet=sr_bt, backtest_n_bets=int(len(backtest)))
    if len(bets) < int(cfg["min_bets_for_verdict"]):
        return EdgeVerdict(**base, verdict="insufficient_sample", allocation=None,
                           mean_r=float(np.mean(bets)) if len(bets) else None,
                           observed_sr_per_bet=None, psr_vs_zero=None, psr_vs_backtest=None,
                           strategy_risk_diagnostic=None,
                           note=f"needs {cfg['min_bets_for_verdict']} bets; no action")
    psr0, sr = probabilistic_sharpe_ratio(bets, 0.0)
    psr_bt, _ = probabilistic_sharpe_ratio(bets, sr_bt)
    risk = None  # filled by run() on BET-grouped live and backtest samples (AFML Ch.14 p.196 / s.15.4)
    if psr0 <= float(cfg["off_rule"]["off_if_at_or_below"]):
        verdict, alloc = "off", 0.0
    else:
        alloc = allocation_for(psr_bt, cfg["shrink_levels"])
        verdict = "full" if alloc >= 1.0 else "reduced"
    return EdgeVerdict(**base, verdict=verdict, allocation=alloc, mean_r=float(np.mean(bets)),
                       observed_sr_per_bet=sr, psr_vs_zero=psr0, psr_vs_backtest=psr_bt,
                       strategy_risk_diagnostic=risk)


_BENCHMARK_CACHE: dict[str, Any] = {}


def load_backtest_bets(cfg: dict[str, Any]) -> pd.DataFrame:
    """Benchmark bets; cached per process until the ledger file changes (allocator re-run)."""
    spec = cfg["backtest_benchmark"]
    path = V2_ROOT / spec["ledger"]
    key = (str(path), path.stat().st_mtime_ns, spec["return_column"])
    if _BENCHMARK_CACHE.get("key") != key:
        timing = {"entry_time", "exit_time", "side_value"}  # for the bet grouping of the s.15.4 diagnostic only
        df = pd.read_csv(path, usecols=lambda c: c in {"edge_id", spec["return_column"]} | timing, low_memory=False)
        df = df.rename(columns={spec["return_column"]: "r", "side_value": "side"})
        _BENCHMARK_CACHE.update(key=key, df=df)
    return _BENCHMARK_CACHE["df"]


def load_live_bets(cfg: dict[str, Any]) -> pd.DataFrame:
    """Real closed trades plus resolved shadow bets of off edges (D022)."""
    spec = cfg["live_source"]
    path = V2_ROOT / spec["ledger"]
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []
    real = pd.DataFrame(rows, columns=None if rows else ["edge_id", "live_manifest_hash", spec["return_column"]])
    real = real.rename(columns={spec["return_column"]: "r", "side_value": "side"})
    real = real[[c for c in ("edge_id", "live_manifest_hash", "r", "entry_time", "exit_time", "side") if c in real.columns]]
    real["source"] = "real"
    shadow_bets.resolve_pending_shadow_bets()
    return pd.concat([real, shadow_bets.load_resolved_shadow_bets()], ignore_index=True)


def run(cfg: dict[str, Any], bets_override: pd.DataFrame | None = None, sample_label: str = "live") -> list[EdgeVerdict]:
    edge_versions.register_current_manifest()
    backtest = load_backtest_bets(cfg)
    bets = bets_override if bets_override is not None else load_live_bets(cfg)
    out: list[EdgeVerdict] = []
    for edge_id, current in sorted(edge_versions.current_versions().items()):
        if sample_label == "live":
            edge_bets = edge_versions.filter_current_version_trades(bets, edge_id, current["version_id"])
        else:  # an explicit sample (e.g. holdout) is produced by the current version by construction
            edge_bets = bets[bets["edge_id"] == edge_id]
        # D056 (owner 2026-10-27): PSRs and the minimum sample are computed on BETS -- a same-side run of overlapping
        # trades is one bet (AFML Ch.14 p.196, Snippet 14.1); trade counts overstate independent evidence.
        bt_rows = backtest.loc[backtest["edge_id"] == edge_id]
        bt = to_bets(bt_rows)
        if len(bt) < 2:
            out.append(EdgeVerdict(edge_id, sample_label, len(edge_bets), "blocked", None, None, None, None, None, None, None, None,
                                   note="no backtest benchmark bets for this edge"))
            continue
        live_bets = to_bets(edge_bets)
        verdict = decide(edge_id, live_bets, bt, cfg, sample_label)
        verdict.n_trades = int(len(edge_bets))
        if verdict.verdict != "insufficient_sample" and {"entry_time", "exit_time"}.issubset(bt_rows.columns):
            span = to_datetime_iso(bt_rows["exit_time"], utc=True).max() - to_datetime_iso(bt_rows["entry_time"], utc=True).min()
            verdict.strategy_risk_diagnostic = strategy_risk_diagnostic(
                live_bets, bt, span.total_seconds() / (365.25 * 86400.0))
        if "source" in edge_bets.columns:
            verdict.n_shadow_bets = int((edge_bets["source"] == "shadow").sum())
        out.append(verdict)
    return out


# ---------------------------------------------------------------------------
# Live application (AFML p.9: re-assessed "frequently and automatically")
# ---------------------------------------------------------------------------
LATEST_VERDICTS_PATH = OUTPUT_DIR / "latest_verdicts.json"
_LIVE_CACHE: dict[str, Any] = {}


def live_allocations(max_age_seconds: float = 3600.0) -> dict[str, dict[str, Any]]:
    """{edge_id: {"allocation": float, "verdict": str}} for the execution bot.

    Recomputed at most every `max_age_seconds`. insufficient_sample -> 1.0 (no evidence
    either way). blocked -> 0.0 (fail closed). If recomputation fails, the last good
    verdicts on disk are used; if there are none, the error propagates (fail closed).
    """
    now = datetime.now(timezone.utc).timestamp()
    if _LIVE_CACHE and now - _LIVE_CACHE["at"] < max_age_seconds:
        return _LIVE_CACHE["allocations"]
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        verdicts = run(cfg)
        allocations = {
            v.edge_id: {
                "allocation": 1.0 if v.verdict == "insufficient_sample" else float(v.allocation or 0.0),
                "verdict": v.verdict,
                "n_bets": v.n_bets,
                "n_trades": v.n_trades,
                "n_shadow_bets": v.n_shadow_bets,
                "psr_vs_zero": v.psr_vs_zero,
                "psr_vs_backtest": v.psr_vs_backtest,
            }
            for v in verdicts
        }
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        LATEST_VERDICTS_PATH.write_text(json.dumps(
            {"computed_utc": datetime.now(timezone.utc).isoformat(), "allocations": allocations}, indent=2), encoding="utf-8")
    except Exception:
        if not LATEST_VERDICTS_PATH.exists():
            raise
        allocations = json.loads(LATEST_VERDICTS_PATH.read_text(encoding="utf-8"))["allocations"]
    _LIVE_CACHE.update(at=now, allocations=allocations)
    return allocations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bets-csv", help="judge these bets instead of live (columns edge_id, r)")
    parser.add_argument("--sample-label", default="live")
    args = parser.parse_args()
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    override = pd.read_csv(args.bets_csv) if args.bets_csv else None
    verdicts = run(cfg, override, args.sample_label)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {"generated_utc": stamp, "sample_label": args.sample_label,
              "config": str(CONFIG_PATH.relative_to(V2_ROOT)).replace("\\", "/"),
              "verdicts": [asdict(v) for v in verdicts]}
    path = OUTPUT_DIR / f"decommission_report_{args.sample_label}_{stamp}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for v in verdicts:
        alloc = "-" if v.allocation is None else f"{v.allocation:.0%}"
        psr0 = "-" if v.psr_vs_zero is None else f"{v.psr_vs_zero:.3f}"
        psrb = "-" if v.psr_vs_backtest is None else f"{v.psr_vs_backtest:.3f}"
        print(f"{v.edge_id:40s} bets={v.n_bets:4d} trades={v.n_trades:4d} (shadow trades {v.n_shadow_bets}) {v.verdict:20s} alloc={alloc:5s} PSR[0]={psr0} PSR[bt]={psrb} {v.note}")
    print(f"report: {path}")


if __name__ == "__main__":
    main()
