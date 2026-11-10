"""
Deep-dive drill-down for any edge flagged by the drift engine (v3, built
2026-09-24), answering directly: "could this just be a normal losing streak
that this edge's own backtest already knows is possible, rather than real
drift -- and how many more live trades until we can tell for sure?"

Two independent checks, both using the edge's OWN real backtest sample (not
a generic assumption):

1. Streak check: this edge's Gate C summary row (v3_execution_velocity_summary)
   already records max_loss_streak -- the worst consecutive-loss run ever
   observed in the FULL backtest history for this exact geometry. Compare
   the live book's current consecutive-loss streak against it directly. If
   backtest's own worst-ever streak dwarfs the live streak, the live streak
   alone is not unusual for this edge.

2. Small-sample noise check (bootstrap): resample n_live trades WITH
   replacement from the backtest per-trade sample B times, computing the
   mean realized R each time. This is the actual distribution of "what a
   window of only n_live trades looks like" under the null that live is
   drawn from the same process as backtest -- i.e. the honest small-sample
   noise band. Report where the live sample's real mean R falls as a
   percentile of that distribution. This is a different, complementary
   question to the KS test in drift_stats.py: KS asks "do the two full
   distributions differ"; this asks "is a sample this extreme common even
   under the backtest's own distribution, purely from having so few trades
   so far."

Also estimates, via the same bootstrap machinery, roughly how many
additional live trades (at the currently-observed effect size) would be
needed before a KS test at alpha=0.05 reliably rejects the null -- a
concrete answer to "will this resolve itself as more trades fire, or is
something already conclusively wrong."
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
if str(_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(_V2_ROOT))

N_BOOTSTRAP = 3000
RNG_SEED = 20260924  # fixed seed: report is meant to be stable between reloads at the same data


def _current_consecutive_loss_streak(realized_r: pd.Series) -> int:
    """Longest run of realized_r <= 0 ending at any point in the live
    series (time-ordered), reported as the streak active as of the most
    recent closed trade -- what the user is actually worried about right now."""
    losses = (realized_r <= 0).to_numpy()
    if len(losses) == 0:
        return 0
    streak = 0
    for is_loss in losses[::-1]:
        if not is_loss:
            break
        streak += 1
    return streak


def _max_consecutive_loss_streak_anywhere(realized_r: pd.Series) -> int:
    losses = (realized_r <= 0).to_numpy()
    best = cur = 0
    for is_loss in losses:
        cur = cur + 1 if is_loss else 0
        best = max(best, cur)
    return best


@dataclass
class EdgeDeepDive:
    edge_id: str
    n_live: int
    live_mean_r: float
    live_current_streak: int
    live_max_streak_so_far: int
    backtest_max_loss_streak: int | None
    backtest_n: int
    bootstrap_percentile: float | None
    bootstrap_band_lo: float | None
    bootstrap_band_hi: float | None
    est_trades_to_resolve: int | None
    verdict_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "n_live": self.n_live,
            "live_mean_r": round(self.live_mean_r, 4),
            "live_current_streak": self.live_current_streak,
            "live_max_streak_so_far": self.live_max_streak_so_far,
            "backtest_max_loss_streak": self.backtest_max_loss_streak,
            "backtest_n": self.backtest_n,
            "bootstrap_percentile": self.bootstrap_percentile,
            "bootstrap_band_lo": self.bootstrap_band_lo,
            "bootstrap_band_hi": self.bootstrap_band_hi,
            "est_trades_to_resolve": self.est_trades_to_resolve,
            "verdict_note": self.verdict_note,
        }


def _estimate_trades_to_resolve(
    live_mean: float,
    backtest_r: np.ndarray,
    n_live: int,
    rng: np.random.Generator,
) -> int | None:
    """Simulate growing the live sample (holding the CURRENTLY OBSERVED live
    mean effect size fixed, i.e. 'if this keeps up') and find the smallest n
    at which a KS test of a same-shaped-but-shifted synthetic live sample vs
    backtest would reliably (>=80% of simulated draws) come back significant.
    This is a rough power-analysis heuristic, not a guarantee -- labeled as
    such wherever it's shown."""
    if n_live < 3 or len(backtest_r) < 30:
        return None
    observed_shift = live_mean - float(np.mean(backtest_r))
    if abs(observed_shift) < 1e-6:
        return None
    for n_try in (n_live, 30, 50, 75, 100, 150, 200, 300):
        if n_try < n_live:
            continue
        hits = 0
        trials = 200
        for _ in range(trials):
            synthetic_live = rng.choice(backtest_r, size=n_try, replace=True) + observed_shift
            p = stats.ks_2samp(synthetic_live, backtest_r).pvalue
            if p < 0.05:
                hits += 1
        if hits / trials >= 0.8:
            return n_try
    return None


def build_edge_deep_dive(
    edge_id: str,
    live_trades: pd.DataFrame,
    backtest_trades: pd.DataFrame | None,
    gate_c_reference: dict[str, Any] | None,
    current_geometry_label: str | None,
) -> EdgeDeepDive | None:
    if live_trades.empty or "realized_r" not in live_trades.columns:
        return None
    live_r = pd.to_numeric(live_trades["realized_r"], errors="coerce").dropna()
    if live_r.empty:
        return None

    backtest_max_streak = None
    if gate_c_reference and gate_c_reference.get("rows"):
        rows = gate_c_reference["rows"]
        matched = [r for r in rows if current_geometry_label and r.get("label") == current_geometry_label]
        chosen = matched[0] if matched else rows[0]
        val = chosen.get("max_loss_streak")
        backtest_max_streak = int(val) if val is not None and not pd.isna(val) else None

    bootstrap_pct = None
    band_lo = band_hi = None
    est_trades = None
    if backtest_trades is not None and "exec_r" in backtest_trades.columns:
        backtest_r = pd.to_numeric(backtest_trades["exec_r"], errors="coerce").dropna().to_numpy()
        if len(backtest_r) >= 30:
            rng = np.random.default_rng(RNG_SEED)
            n_live = len(live_r)
            boot_means = np.array(
                [rng.choice(backtest_r, size=n_live, replace=True).mean() for _ in range(N_BOOTSTRAP)]
            )
            live_mean = float(live_r.mean())
            bootstrap_pct = float((boot_means < live_mean).mean() * 100.0)
            band_lo = float(np.percentile(boot_means, 2.5))
            band_hi = float(np.percentile(boot_means, 97.5))
            est_trades = _estimate_trades_to_resolve(live_mean, backtest_r, n_live, rng)

    live_mean_r = float(live_r.mean())
    current_streak = _current_consecutive_loss_streak(live_r)
    max_streak_so_far = _max_consecutive_loss_streak_anywhere(live_r)

    note_parts = []
    if backtest_max_streak is not None:
        if current_streak == 0:
            note_parts.append("No live losing streak in progress right now.")
        elif current_streak <= backtest_max_streak * 0.15:
            note_parts.append(
                f"Current {current_streak}-trade losing streak is small next to this edge's own "
                f"worst-ever backtest streak ({backtest_max_streak} trades) -- not unusual for this edge."
            )
        else:
            note_parts.append(
                f"Current {current_streak}-trade losing streak is a meaningful fraction of this edge's "
                f"worst-ever backtest streak ({backtest_max_streak} trades) -- worth continued watching."
            )
    if bootstrap_pct is not None:
        if bootstrap_pct < 5 or bootstrap_pct > 95:
            note_parts.append(
                f"Live's realized mean R sits at the {bootstrap_pct:.0f}th percentile of what a "
                f"random {len(live_r)}-trade window from the backtest's OWN distribution would produce "
                "-- unusual even accounting for small-sample noise."
            )
        else:
            note_parts.append(
                f"Live's realized mean R sits at the {bootstrap_pct:.0f}th percentile of what a random "
                f"{len(live_r)}-trade window from the backtest's OWN distribution would produce -- "
                "well within normal small-sample noise for a window this size. This alone could still just "
                "be a losing streak the backtest already knows how to produce."
            )
    if est_trades is not None:
        note_parts.append(
            f"If the current effect size persists, roughly {est_trades} live trades total would be enough "
            "for the KS drift test to reliably confirm it either way (rough power estimate, not a guarantee)."
        )

    return EdgeDeepDive(
        edge_id=edge_id,
        n_live=len(live_r),
        live_mean_r=live_mean_r,
        live_current_streak=current_streak,
        live_max_streak_so_far=max_streak_so_far,
        backtest_max_loss_streak=backtest_max_streak,
        backtest_n=len(backtest_trades) if backtest_trades is not None else 0,
        bootstrap_percentile=bootstrap_pct,
        bootstrap_band_lo=band_lo,
        bootstrap_band_hi=band_hi,
        est_trades_to_resolve=est_trades,
        verdict_note=" ".join(note_parts),
    )


if __name__ == "__main__":
    import json as _json

    from portfolio_live.drift_dashboard.data_sources import (
        load_current_live_edge_ids,
        load_gate_c_reference,
        scope_closed_trades,
    )
    from portfolio_live.drift_dashboard.edge_reference import (
        read_current_edge_geometry,
        resolve_backtest_trade_sample,
    )

    edge_ids = load_current_live_edge_ids()
    scoped = scope_closed_trades(live_edge_ids=edge_ids)
    for eid in sorted(edge_ids):
        rows = scoped.in_scope[scoped.in_scope["edge_id"] == eid]
        if rows.empty:
            continue
        bt = resolve_backtest_trade_sample(eid)
        geo = read_current_edge_geometry(eid)
        gate_c = load_gate_c_reference(eid)
        dd = build_edge_deep_dive(eid, rows, bt.get("trades"), gate_c, geo.config_label if geo else None)
        if dd:
            print(f"=== {eid} ===")
            print(_json.dumps(dd.to_dict(), indent=2))
