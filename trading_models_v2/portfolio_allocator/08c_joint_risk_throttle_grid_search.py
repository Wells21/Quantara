"""
V2 Phase B2.08C -- Joint search across Step 08's risk-scenario candidates x
Step 09's throttle/reserve guard library.

Pipeline order: 08 -> 08b -> 08c (this file) -> 09 -> 10 -> 11. This script
runs AFTER Step 08 (needs its stage2_aggregate.csv) and BEFORE Step 09 --
its job is to figure out which Step 08 candidate to even prefer once a
throttle is layered on top, which a human then locks into Step 09's
ACTIVE_VARIANT_SPEC. It is positioned in the "08 family" (08c, not 09b)
because its subject is Step 08's candidate selection, not Step 09's own
single-variant deep-dive.

Context: the 08 -> 09 pipeline is a GREEDY two-stage search -- Step 08 picks
the fastest (risk-budget, single-trade-cap, overlap-cap) combination assuming
NO throttle, then Step 09 tunes the drawdown-buffer throttle and admission
reserve on top of that one frozen winner. This is structurally capable of
missing the true joint optimum: a combination that looks slightly worse
without a throttle could become the best WITH the right throttle layered on
top, because the throttle only acts in a different regime of the equity
curve (near the drawdown floor) than the base risk caps do.

Rather than merging 08 and 09 into one script (which would require rewriting
Step 08's simulator to carry throttle state, and would break the
contract-validation chain 09/10/11 rely on -- 09 validates it is using
Step 08's EXACT numbers via structural-equality/drift checks; 10 replays 09's
exact start grid; 11 reads 10's output), this script runs the CROSS PRODUCT
of the top Step 08 candidates x Step 09's guard-profile/reserve-mode library
directly, using Step 09's own (parallelized) simulator and ranking logic, and
reports the single best (step08_candidate, guard_profile, reserve_mode)
combination found. It does not overwrite Step 08's own best_robust/
best_fast_safe/best_fast_frontier_scenario.csv files, but it DOES write an
additional best_joint_scenario.csv into Step 08's output directory, in the
exact same schema, holding the winning combo's Step08 risk settings
(budget/single-trade-cap/overlap mode) -- Step 09's load_step08_selected()
prefers this file first (see 09_fast_manual_variant_lab.py), so the Step08
side of the discovery is picked up automatically. The GUARD/RESERVE side
(guard_profile, trailing_buffer_reserve_mode, etc.) is NOT auto-applied --
per this project's existing single-variant-lab design, a human reviews this
script's joint_grid_ranked.csv and hand-edits Step 09's ACTIVE_VARIANT_SPEC
to match the winning guard/reserve combo, then reruns Step 09 in
full_single mode to produce the canonical Step 10 inputs.

Key implementation fact (verified against 09_fast_manual_variant_lab.py's
prepare_selected_arrays): the ledger-derived arrays (arr, base_raw_pct) only
depend on the HRP weight-group key (return_mode/universe/profile_role/
linkage_method), NOT on risk budget/single-trade cap/overlap mode. Since
Step 08's grid tests exactly one HRP group, arr/base_raw_pct/starts are
identical across every (step08_candidate, guard, reserve) combination and are
built ONCE, then every combination's variant dict (which is where budget/
single-trade/overlap/guard/reserve actually differ) is simulated in a single
combined parallel run.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
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

STEP09_PATH = THIS_FILE.with_name("09_fast_manual_variant_lab.py")
STEP08_OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "prop_challenge_rolling_optimizer_outputs"
STEP08_STAGE2_AGGREGATE = STEP08_OUTPUT_DIR / "stage2_aggregate.csv"
OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "joint_risk_throttle_grid_search_outputs"

# --- scan scope (tune these between passes; see module docstring) ---------
TOP_N_STEP08_CANDIDATES = 3
SCAN_EVERY_N_ACTIVE_DAYS = 3
SCAN_MAX_ROLLING_STARTS = 40
SCAN_COST_STRESS_R_PER_TRADE = [0.00, 0.02, 0.05]
RESERVE_CONFIGS: list[dict[str, Any]] = [
    {
        "reserve_label": "off",
        "trailing_buffer_admission_enabled": False,
        "trailing_buffer_reserve_mode": "off",
        "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer": 0.0,
        "trailing_buffer_cost_allowance_r": 0.0,
    },
    {
        "reserve_label": "dynamic_projected_open_risk",
        "trailing_buffer_admission_enabled": True,
        "trailing_buffer_reserve_mode": "dynamic_projected_open_risk",
        "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer": 0.003125,
        "trailing_buffer_cost_allowance_r": 0.05,
    },
]


def _load_step09():
    spec = importlib.util.spec_from_file_location("step09_joint_lab", STEP09_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["step09_joint_lab"] = module
    spec.loader.exec_module(module)
    return module


# Loaded at IMPORT time (not inside build()/main()) so that on Windows'
# spawn-based multiprocessing, a worker process re-importing this file as a
# fresh interpreter also re-registers sys.modules["step09_joint_lab"] before
# ProcessPoolExecutor tries to unpickle _pool09_run_chunk (whose __module__
# is "step09_joint_lab") -- a function loaded only inside a __main__-guarded
# call would be missing in spawned children and fail to unpickle.
step09 = _load_step09()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def load_top_step08_candidates(n: int) -> list[dict[str, Any]]:
    if not STEP08_STAGE2_AGGREGATE.exists():
        raise FileNotFoundError(f"Missing {STEP08_STAGE2_AGGREGATE}; run Step 08 first.")
    df = pd.read_csv(STEP08_STAGE2_AGGREGATE, float_precision="round_trip")
    base = df[df["cost_stress_r"] == 0.0].copy()
    if base.empty:
        raise RuntimeError("Step 08 stage2_aggregate.csv has no cost_stress_r=0.0 rows.")
    # Rank Step 08 candidates by SPEED first (median days to target, ascending)
    # -- the goal is fastest-without-failing, and the throttle's whole job is
    # to bring down hard-fail-rate on a fast-but-riskier base config. Starting
    # the joint scan from Step 08's highest-pass-rate (safest-but-slowest)
    # candidate would bias away from ever finding a faster winner.
    base = base.sort_values("median_trading_days_to_target", ascending=True).head(int(n))
    return base.to_dict(orient="records")


def build_reserve_configs(
    reserve_pcts: list[float] | None,
    reserve_cost_allowances: list[float] | None,
    *,
    include_off: bool = True,
) -> list[dict[str, Any]]:
    """Cross product of (fixed_reserve_pct, cost_allowance_r) values for the
    dynamic_projected_open_risk admission mode, plus an "off" control unless
    disabled. Used to sweep the reserve's own tuning knobs once a promising
    (step08_candidate, guard_profile) region has already been found -- the
    fixed pct sets a floor reserve; cost_allowance_r scales with projected
    open risk and is the more direct lever for tightening behavior under
    harsher cost-stress (0.02R/0.05R) scenarios."""
    configs: list[dict[str, Any]] = []
    if include_off:
        configs.append({
            "reserve_label": "off",
            "trailing_buffer_admission_enabled": False,
            "trailing_buffer_reserve_mode": "off",
            "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer": 0.0,
            "trailing_buffer_cost_allowance_r": 0.0,
        })
    pcts = reserve_pcts or [0.003125]
    allowances = reserve_cost_allowances or [0.05]
    for pct in pcts:
        for allowance in allowances:
            configs.append({
                "reserve_label": f"dynamic_pct{pct:g}_cost{allowance:g}",
                "trailing_buffer_admission_enabled": True,
                "trailing_buffer_reserve_mode": "dynamic_projected_open_risk",
                "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer": float(pct),
                "trailing_buffer_cost_allowance_r": float(allowance),
            })
    return configs


def _prepare_common(
    candidates: list[dict[str, Any]], every_n_active_days: int, max_rolling_starts: int
) -> tuple[dict[str, Any], np.ndarray, list[int], int, int, int, dict[int, str]]:
    """arr/base_raw_pct only depend on the HRP weight-group key -- identical
    across every candidate/guard/reserve combination (see module docstring).
    Rebuilt per call since Stage A and Stage B of build_auto() use different
    rolling-start densities."""
    ledger, asset_to_code, family_to_code, edge_to_code = step09.load_ledger(step09.INPUT_LEDGER)
    weights = step09.load_hrp_weights(step09.INPUT_HRP_WEIGHTS)
    code_to_asset = {v: k for k, v in asset_to_code.items()}
    n_assets, n_families, n_edges_global = len(asset_to_code), len(family_to_code), len(edge_to_code)
    arr = step09.prepare_selected_arrays(candidates[0], ledger, weights)
    base_raw_pct = arr["hrp_weight"].astype(float) * arr["afml_bet_size"].astype(float)
    starts = step09.build_rolling_start_times(arr, every_n_active_days, max_rolling_starts)
    return arr, base_raw_pct, starts, n_assets, n_families, n_edges_global, code_to_asset


def run_combo_grid(
    combos: list[tuple[dict[str, Any], str, dict[str, Any]]],
    arr: dict[str, Any],
    base_raw_pct: np.ndarray,
    starts: list[int],
    n_assets: int,
    n_families: int,
    n_edges_global: int,
    code_to_asset: dict[int, str],
    n_workers: int,
    desc: str,
    *,
    costs: list[float] | None = None,
) -> pd.DataFrame:
    """Runs an EXPLICIT list of (step08_candidate_row, guard_profile,
    reserve_config) combos -- no cross product -- and returns the ranked
    summary with provenance columns merged in. Used both by build()'s
    cross-product CLI mode and by build_auto()'s two-stage search (where
    Stage B only re-runs the specific combos Stage A promoted)."""
    variants: list[dict[str, Any]] = []
    combo_meta: list[dict[str, Any]] = []
    daily_cfg_cache: dict[str, dict[str, Any]] = {}
    first_candidate = combos[0][0]
    for cand, guard, reserve in combos:
        cand_label = str(cand.get("foundation_candidate_name", cand.get("scenario_id")))
        if cand_label not in daily_cfg_cache:
            daily_cfg_cache[cand_label] = step09.daily_drawdown_from_selected(cand)
        daily_cfg = daily_cfg_cache[cand_label]
        spec = {
            "name": f"joint__{cand_label}__{guard}__{reserve['reserve_label']}",
            "guard_profile": guard,
            "trailing_buffer_admission_enabled": reserve["trailing_buffer_admission_enabled"],
            "trailing_buffer_reserve_mode": reserve["trailing_buffer_reserve_mode"],
            "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer": reserve["trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer"],
            "trailing_buffer_cost_allowance_r": reserve["trailing_buffer_cost_allowance_r"],
        }
        variant = step09._make_variant_from_spec(spec, cand, daily_cfg)
        step09.validate_variant(variant)
        variants.append(variant)
        combo_meta.append({
            "variant_label": step09.variant_label(variant),
            "step08_candidate": cand_label,
            "step08_scenario_id": cand.get("scenario_id"),
            "step08_overlap_mode": cand.get("overlap_mode"),
            "step08_base_portfolio_risk_budget_pct": cand.get("base_portfolio_risk_budget_pct"),
            "step08_max_single_trade_risk_pct": cand.get("max_single_trade_risk_pct"),
            "guard_profile": guard,
            "reserve_label": reserve["reserve_label"],
        })

    active_costs = costs if costs is not None else SCAN_COST_STRESS_R_PER_TRADE
    if 0.0 not in active_costs:
        raise ValueError("costs must include 0.0 -- build_ranked_summary requires a cost_stress_r==0.0 base row.")
    print(f"{desc}: {len(variants)} variants, {len(starts)} rolling starts, {len(active_costs)} costs -> {len(variants) * len(starts) * len(active_costs)} total simulations")

    run_df = step09.run_variant_set(
        variants, arr, base_raw_pct, starts, active_costs,
        n_assets, n_families, n_edges_global, code_to_asset,
        first_candidate, desc, n_workers=n_workers,
    )
    agg = step09.aggregate_run_results(run_df)
    ranked = step09.build_ranked_summary(agg)

    meta_df = pd.DataFrame(combo_meta)
    # ranked already carries guard_profile/cap_name/trailing_buffer_*/etc. from
    # build_ranked_summary's own meta_cols; drop the overlapping columns from
    # meta_df (keeping only the join key + genuinely new provenance fields) so
    # the merge can't silently rename them to _x/_y and break downstream
    # `.get(...)` lookups.
    overlap_cols = [c for c in meta_df.columns if c != "variant_label" and c in ranked.columns]
    meta_df = meta_df.drop(columns=overlap_cols)
    ranked = ranked.merge(meta_df, on="variant_label", how="left")
    # 2026-10-17: inside one selection tier, hard-fail must outrank speed. Sorting by days alone promoted a 47% hard-fail
    # no-reserve variant over a 1.7% hard-fail dynamic-reserve variant that was 1.5 days slower. Hard-fail is bucketed to
    # whole percentage points so trivial differences (1.7% vs 2.5%) do not override large speed differences.
    if "max_hard_fail_rate_selection_costs" in ranked.columns:
        ranked["_hard_fail_bucket_pct"] = np.floor(ranked["max_hard_fail_rate_selection_costs"].astype(float).fillna(1.0) * 100.0 + 1e-9)
    sort_spec = [("selection_tier_rank", True), ("_hard_fail_bucket_pct", True), ("base_median_trading_days_to_target", True), ("base_pass_rate", False)]
    sort_spec = [(c, a) for c, a in sort_spec if c in ranked.columns]
    ranked = ranked.sort_values([c for c, _ in sort_spec], ascending=[a for _, a in sort_spec]).reset_index(drop=True)
    return ranked.drop(columns=["_hard_fail_bucket_pct"], errors="ignore")


def _write_winner_artifacts(ranked: pd.DataFrame, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Writes best_joint_scenario.csv (Step08 risk-settings side, same schema
    as Step08's own best_robust/fast_safe/fast_frontier files) AND
    best_joint_variant_spec.json (guard/reserve side, same shape as Step 09's
    ACTIVE_VARIANT_SPEC) into Step 08's output directory. 09_fast_manual_variant_lab.py
    prefers both automatically -- see load_step08_selected() and
    _load_active_variant_spec() there -- so the full 08 -> 08c -> 09 chain
    runs without a hand-edit once 08c has produced a winner."""
    best = ranked.iloc[0].to_dict() if not ranked.empty else {}
    if not best:
        return best
    winning_candidate_name = str(best.get("step08_candidate"))
    by_name = {str(c.get("foundation_candidate_name")): c for c in candidates}
    winning_step08_row = by_name.get(winning_candidate_name)
    if winning_step08_row is not None:
        pd.DataFrame([winning_step08_row]).to_csv(STEP08_OUTPUT_DIR / "best_joint_scenario.csv", index=False)
    variant_spec = {
        "name": str(best.get("variant_label", "joint_search_winner")),
        "guard_profile": str(best.get("guard_profile")),
        "trailing_buffer_admission_enabled": bool(best.get("trailing_buffer_admission_enabled")),
        "trailing_buffer_reserve_mode": str(best.get("trailing_buffer_reserve_mode")),
        "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer": float(best.get("trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer")),
        "trailing_buffer_cost_allowance_r": float(best.get("trailing_buffer_cost_allowance_r")),
        "source": "08c_joint_risk_throttle_grid_search.py",
        "source_selection_tier": best.get("selection_tier"),
        "source_base_pass_rate": best.get("base_pass_rate"),
        "source_base_hard_fail_rate": best.get("base_hard_fail_rate"),
        "source_base_median_trading_days_to_target": best.get("base_median_trading_days_to_target"),
        "created_at": now_utc(),
    }
    write_json(STEP08_OUTPUT_DIR / "best_joint_variant_spec.json", variant_spec)
    return best


def build(
    *,
    n_workers: int = 1,
    write: bool = True,
    candidate_names: list[str] | None = None,
    guard_profiles_filter: list[str] | None = None,
    every_n_active_days: int = SCAN_EVERY_N_ACTIVE_DAYS,
    max_rolling_starts: int = SCAN_MAX_ROLLING_STARTS,
    reserve_configs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Manual/exploratory cross-product mode (preserves existing CLI
    behavior): every (candidate, guard, reserve) combination is tested. For
    an unattended, self-scoping search see build_auto()."""
    step09.configure_run_mode("quick_probe")

    if candidate_names:
        all_candidates = load_top_step08_candidates(9999)
        by_name = {str(c.get("foundation_candidate_name")): c for c in all_candidates}
        missing = [n for n in candidate_names if n not in by_name]
        if missing:
            raise ValueError(f"Unknown Step08 candidate name(s): {missing}. Available: {sorted(by_name)}")
        candidates = [by_name[n] for n in candidate_names]
    else:
        candidates = load_top_step08_candidates(TOP_N_STEP08_CANDIDATES)

    guard_profiles = sorted(guard_profiles_filter) if guard_profiles_filter else sorted(step09.GUARD_PROFILE_LIBRARY.keys())
    if guard_profiles_filter:
        unknown = [g for g in guard_profiles if g not in step09.GUARD_PROFILE_LIBRARY]
        if unknown:
            raise ValueError(f"Unknown guard profile(s): {unknown}")

    active_reserve_configs = reserve_configs if reserve_configs is not None else RESERVE_CONFIGS
    arr, base_raw_pct, starts, n_assets, n_families, n_edges_global, code_to_asset = _prepare_common(
        candidates, every_n_active_days, max_rolling_starts
    )
    combos = [(cand, guard, reserve) for cand in candidates for guard in guard_profiles for reserve in active_reserve_configs]
    ranked = run_combo_grid(combos, arr, base_raw_pct, starts, n_assets, n_families, n_edges_global, code_to_asset, n_workers, "joint 08x09 grid")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if write:
        ranked.to_csv(OUTPUT_DIR / "joint_grid_ranked.csv", index=False)

    best = _write_winner_artifacts(ranked, candidates) if write else (ranked.iloc[0].to_dict() if not ranked.empty else {})

    manifest = {
        "created_at": now_utc(),
        "n_step08_candidates": len(candidates),
        "n_guard_profiles": len(guard_profiles),
        "n_reserve_configs": len(active_reserve_configs),
        "n_variants": len(combos),
        "n_rolling_starts": len(starts),
        "costs_tested": SCAN_COST_STRESS_R_PER_TRADE,
        "best_combo": {
            "variant_label": best.get("variant_label"),
            "step08_candidate": best.get("step08_candidate"),
            "step08_overlap_mode": best.get("step08_overlap_mode"),
            "step08_base_portfolio_risk_budget_pct": best.get("step08_base_portfolio_risk_budget_pct"),
            "step08_max_single_trade_risk_pct": best.get("step08_max_single_trade_risk_pct"),
            "guard_profile": best.get("guard_profile"),
            "reserve_label": best.get("reserve_label"),
            "selection_tier": best.get("selection_tier"),
            "base_pass_rate": best.get("base_pass_rate"),
            "base_hard_fail_rate": best.get("base_hard_fail_rate"),
            "base_median_trading_days_to_target": best.get("base_median_trading_days_to_target"),
        },
        "outputs": {"ranked": str(OUTPUT_DIR / "joint_grid_ranked.csv")},
    }
    if write:
        write_json(OUTPUT_DIR / "joint_grid_manifest.json", manifest)
    return manifest


# --- unattended two-tier search (the default CLI mode) ---------------------
# Cost discipline matters here (an earlier 3-tier prefilter+StageA+StageB
# design was measured at ~90-120 minutes total -- unacceptable for a search
# meant to be re-run casually whenever account rules change) without giving
# up the robustness lesson learned the hard way: guessing a handful of
# "representative" guards/reserves to keep an early filtering pass cheap
# repeatedly excluded the eventual winner (a min-lot-inflation-guarded
# candidate that needed the specific delayed_emergency guard, and ranked only
# 5th by Step08's own throttle-blind speed metric).
#
# The fix is to remove guessing from the EXPENSIVE dimension (guard x
# candidate -- these interact strongly and unpredictably) while accepting a
# single reasonable guess only on the CHEAPER, more structural dimension
# (the reserve's exact pct/cost_allowance -- this tunes how much buffer to
# hold back, which behaves similarly regardless of which candidate/guard it's
# protecting, so one reasonably strong reserve setting is a safe proxy for
# ranking purposes).
#
# Candidate shortlist: a cheap, zero-extra-compute top-N-by-speed cut of
# Step 08's own candidates (reusing its already-reliable 400-start data). A
# strict Pareto frontier on Step08's own numbers was tried and rejected -- it
# excluded the eventual winner too, dominated on a technicality (a near-tied
# sibling candidate with marginally lower RAW, no-throttle hard-fail) that
# doesn't necessarily hold once a throttle is applied. N=10 confirmed to
# include the eventual winner (ranked 5th of 22 by raw speed) with margin.
# Survey: shortlisted candidates x EVERY guard (no guessing on this
# dimension -- guard behavior varies too much by candidate to safely narrow)
# x ONE reasonable fixed reserve, at a cheap rolling-start count -- ranks
# (candidate, guard) pairs by how well they perform under that one reserve
# setting, via a non-dominated speed-vs-safety frontier (a single-scalar sort
# was tried and rejected twice: sorting by hard-fail-rate-then-speed silently
# promoted "fast and hasn't failed yet in a tiny sample" combos, since
# hard-fail is a rare event that a small sample can't reliably measure;
# sorting by buffer margin instead silently promoted the slowest, most
# conservative candidates, since low risk mechanically produces a large
# buffer).
# Confirm: the full reserve grid is swept ONLY for the top-K (candidate,
# guard) pairs Survey promoted, at a MODERATE (not maximal) rolling-start
# count -- 120, matching the density this project used successfully before
# the 400-start experiments (per-run variance below the gates used here is
# small enough at 120 starts that the extra 3x cost of 400 buys little).
AUTO_SURVEY_EVERY_N_ACTIVE_DAYS = 3
# 60, not 20: hard-fail is a rare event (true rates elsewhere in this project
# run ~1-3%), and at 20 starts x 2 costs (40 trials) most combos show ZERO
# observed failures purely by chance -- there simply are not enough trials to
# estimate a few-percent rate. 60 starts x 2 costs (120 trials) is still far
# cheaper than Confirm's 120x3=360, but gives the ranking below enough signal
# to be more than noise.
AUTO_SURVEY_MAX_ROLLING_STARTS = 80
# 2026-09-25: raised from 10 to 30 (Step08's foundation grid grew from 16 to
# 25 candidates with the max_open_trades_per_edge and max_min_lot_risk_
# inflation probes). A top-10-by-raw-speed cut previously excluded
# pairwise_cap1p70_budget100_trade20_minlotcap6x purely because it tied for
# 5th-8th place on an unstable pandas sort -- guard/count-cap variants
# systematically rank slower on Step08's own unthrottled speed metric even
# when they may be the safer choice once a throttle/reserve is layered on
# top, so a tight cutoff structurally biases the survey against exactly the
# candidates these probes exist to test. 30 comfortably covers the full
# current grid with margin.
AUTO_CANDIDATE_SHORTLIST_SIZE = 30
AUTO_SURVEY_COSTS = [0.00, 0.02]
AUTO_SURVEY_RESERVE: dict[str, Any] = {
    "reserve_label": "survey_dynamic_pct0.01_cost0.2",
    "trailing_buffer_admission_enabled": True,
    "trailing_buffer_reserve_mode": "dynamic_projected_open_risk",
    "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer": 0.01,
    "trailing_buffer_cost_allowance_r": 0.20,
}
# Speed and safety are genuinely two separate objectives -- ranking by a
# single scalar was found empirically to systematically favor one extreme
# over the other (sorting by hard_fail_rate-then-speed silently promoted
# "fast and hasn't failed yet in a tiny sample" combos; sorting by buffer
# margin silently promoted the slowest, most conservative candidates, since
# low risk mechanically produces a large buffer). The fix is the same
# non-dominated Pareto frontier 08_rolling_start_prop_challenge_optimizer.py
# already uses for its own speed-safety tradeoff (see
# build_speed_safety_frontier there) -- promote every (candidate, guard) pair
# that isn't strictly beaten on BOTH speed and safety by another pair, capped
# at a bounded count so Confirm's cost stays predictable.
AUTO_SURVEY_FRONTIER_MAX_PAIRS = 10

AUTO_CONFIRM_EVERY_N_ACTIVE_DAYS = 3
AUTO_CONFIRM_MAX_ROLLING_STARTS = 120
AUTO_CONFIRM_COSTS = [0.00, 0.02, 0.05]
AUTO_RESERVE_PCTS = [0.00625, 0.0125]
AUTO_RESERVE_COST_ALLOWANCES = [0.15, 0.20, 0.25]


def _speed_safety_frontier(
    ranked: pd.DataFrame,
    *,
    speed_col: str = "base_median_trading_days_to_target",
    safety_col: str = "base_hard_fail_rate",
    max_rows: int = AUTO_SURVEY_FRONTIER_MAX_PAIRS,
) -> pd.DataFrame:
    """Non-dominated (Pareto) frontier over (speed, safety): a row is kept
    unless some OTHER row is at least as good on both axes and strictly
    better on at least one. Mirrors
    08_rolling_start_prop_challenge_optimizer.py's build_speed_safety_frontier
    for the same reason it exists there -- speed and safety are genuinely
    two separate objectives and a single scalar ranking systematically biases
    toward one extreme (see module comment above AUTO_SURVEY_FRONTIER_MAX_PAIRS)."""
    eps = 1e-12
    candidates = ranked.dropna(subset=[speed_col, safety_col]).copy()
    if candidates.empty:
        return candidates
    records = candidates.to_dict(orient="records")
    kept = []
    for i, row in enumerate(records):
        dominated = False
        for j, other in enumerate(records):
            if i == j:
                continue
            no_worse = (
                other[speed_col] <= row[speed_col] + eps
                and other[safety_col] <= row[safety_col] + eps
            )
            strictly_better = (
                other[speed_col] < row[speed_col] - eps
                or other[safety_col] < row[safety_col] - eps
            )
            if no_worse and strictly_better:
                dominated = True
                break
        if not dominated:
            kept.append(row)
    frontier = pd.DataFrame(kept)
    if frontier.empty:
        return frontier
    frontier = frontier.sort_values([safety_col, speed_col], ascending=[True, True]).head(max(1, int(max_rows)))
    return frontier.reset_index(drop=True)


def _survey_candidate_guard_pairs(n_workers: int, top_k_pairs: int, *, write: bool = True) -> pd.DataFrame:
    """A cheap top-N-by-speed shortlist of Step08 candidates (see module
    comment above) x EVERY guard (no guessing on this dimension -- guard
    behavior was found to vary too much by candidate to safely narrow) x ONE
    fixed, reasonable reserve setting, at cheap density. Promotes the
    non-dominated speed-vs-safety frontier (see _speed_safety_frontier),
    capped at top_k_pairs.

    IMPORTANT: at only AUTO_SURVEY_MAX_ROLLING_STARTS starts, hard-fail rate
    is a noisy statistic -- it's a rare event (true rates found elsewhere in
    this project are ~1-3%). A single-scalar ranking on it was tried and
    rejected: sorting by hard-fail-rate-then-speed silently promoted
    no_guard/least-protective combos (fast because nothing is holding them
    back, not because they're actually safe); sorting by buffer margin
    instead silently promoted the slowest, most conservative candidates. The
    non-dominated frontier over (speed, hard-fail-rate) avoids collapsing
    these two genuinely separate objectives into one biased scalar."""
    # A strict Step08-only Pareto frontier was found to exclude the eventual
    # winner too: it was dominated on Step 08's OWN throttle-blind numbers by
    # a near-tied sibling candidate (same speed, marginally lower raw hard-
    # fail rate) -- a technicality that doesn't necessarily hold once a
    # throttle is applied, since that's the whole reason this joint search
    # exists. A simple, wide top-N-by-speed cut is cheap (zero extra compute,
    # reuses Step 08's own reliable 400-start data) and was confirmed to
    # include the eventual winner with margin (it ranked 5th of 22 by raw
    # speed); N=10 keeps a comfortable buffer above that.
    all_candidates = load_top_step08_candidates(AUTO_CANDIDATE_SHORTLIST_SIZE)
    all_guards = sorted(step09.GUARD_PROFILE_LIBRARY.keys())
    arr, base_raw_pct, starts, na, nf, ne, c2a = _prepare_common(
        all_candidates, AUTO_SURVEY_EVERY_N_ACTIVE_DAYS, AUTO_SURVEY_MAX_ROLLING_STARTS
    )
    combos = [(cand, guard, AUTO_SURVEY_RESERVE) for cand in all_candidates for guard in all_guards]
    ranked = run_combo_grid(
        combos, arr, base_raw_pct, starts, na, nf, ne, c2a, n_workers,
        "survey (all candidates x all guards, fixed reserve)", costs=AUTO_SURVEY_COSTS,
    )
    if write:
        # Full raw survey grid (every candidate x every guard, before the
        # speed-safety-frontier trims it down to top_k_pairs) -- persisted so
        # candidates that the frontier's speed/hard-fail-only trade-off does
        # not promote (e.g. zero-daily-drawdown candidates, which the
        # frontier has no visibility into at all) can still be inspected
        # directly instead of being silently discarded in memory.
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ranked.to_csv(OUTPUT_DIR / "joint_grid_survey_full.csv", index=False)
    top = _speed_safety_frontier(ranked, max_rows=max(int(top_k_pairs), AUTO_SURVEY_FRONTIER_MAX_PAIRS))
    if len(top) > top_k_pairs:
        top = top.head(int(top_k_pairs))
    by_name = {str(c.get("foundation_candidate_name")): c for c in all_candidates}
    top["_candidate_row"] = top["step08_candidate"].map(lambda n: by_name.get(str(n)))
    return top[top["_candidate_row"].notna()].reset_index(drop=True)


def build_auto(
    *,
    n_workers: int = 1,
    write: bool = True,
    top_k_pairs: int = AUTO_SURVEY_FRONTIER_MAX_PAIRS,
) -> dict[str, Any]:
    step09.configure_run_mode("quick_probe")
    survey_top = _survey_candidate_guard_pairs(n_workers, top_k_pairs, write=write)
    if survey_top.empty:
        raise RuntimeError("Survey produced no results to promote to Confirm.")

    reserve_configs = build_reserve_configs(AUTO_RESERVE_PCTS, AUTO_RESERVE_COST_ALLOWANCES, include_off=True)
    combos_confirm: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    for _, row in survey_top.iterrows():
        cand = row["_candidate_row"]
        guard = str(row["guard_profile"])
        for reserve in reserve_configs:
            combos_confirm.append((cand, guard, reserve))

    candidates_for_arrays = [row["_candidate_row"] for _, row in survey_top.iterrows()]
    arr_c, braw_c, starts_c, na, nf, ne, c2c = _prepare_common(
        candidates_for_arrays, AUTO_CONFIRM_EVERY_N_ACTIVE_DAYS, AUTO_CONFIRM_MAX_ROLLING_STARTS
    )
    ranked_confirm = run_combo_grid(
        combos_confirm, arr_c, braw_c, starts_c, na, nf, ne, c2c, n_workers,
        "confirm (top candidate/guard pairs x full reserve grid)", costs=AUTO_CONFIRM_COSTS,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if write:
        survey_top.drop(columns=["_candidate_row"]).to_csv(OUTPUT_DIR / "joint_grid_survey_ranked.csv", index=False)
        ranked_confirm.to_csv(OUTPUT_DIR / "joint_grid_ranked.csv", index=False)

    best = _write_winner_artifacts(ranked_confirm, candidates_for_arrays) if write else (
        ranked_confirm.iloc[0].to_dict() if not ranked_confirm.empty else {}
    )

    manifest = {
        "created_at": now_utc(),
        "mode": "auto_survey_confirm",
        "survey": {
            "n_step08_candidates": len({str(c.get("foundation_candidate_name")) for c in load_top_step08_candidates(9999)}),
            "n_guard_profiles": len(step09.GUARD_PROFILE_LIBRARY),
            "n_rolling_starts": AUTO_SURVEY_MAX_ROLLING_STARTS,
            "top_pairs_promoted": survey_top[["step08_candidate", "guard_profile"]].to_dict(orient="records"),
        },
        "confirm": {
            "n_variants": len(combos_confirm),
            "n_reserve_configs": len(reserve_configs),
            "n_rolling_starts": len(starts_c),
            "costs_tested": AUTO_CONFIRM_COSTS,
        },
        "best_combo": {
            "variant_label": best.get("variant_label"),
            "step08_candidate": best.get("step08_candidate"),
            "step08_overlap_mode": best.get("step08_overlap_mode"),
            "step08_base_portfolio_risk_budget_pct": best.get("step08_base_portfolio_risk_budget_pct"),
            "step08_max_single_trade_risk_pct": best.get("step08_max_single_trade_risk_pct"),
            "guard_profile": best.get("guard_profile"),
            "reserve_label": best.get("reserve_label"),
            "selection_tier": best.get("selection_tier"),
            "base_pass_rate": best.get("base_pass_rate"),
            "base_hard_fail_rate": best.get("base_hard_fail_rate"),
            "base_median_trading_days_to_target": best.get("base_median_trading_days_to_target"),
        },
        "outputs": {
            "survey_full": str(OUTPUT_DIR / "joint_grid_survey_full.csv"),
            "survey_ranked": str(OUTPUT_DIR / "joint_grid_survey_ranked.csv"),
            "ranked": str(OUTPUT_DIR / "joint_grid_ranked.csv"),
            "best_joint_scenario": str(STEP08_OUTPUT_DIR / "best_joint_scenario.csv"),
            "best_joint_variant_spec": str(STEP08_OUTPUT_DIR / "best_joint_variant_spec.json"),
        },
    }
    if write:
        write_json(OUTPUT_DIR / "joint_grid_manifest.json", manifest)
    return manifest


def main() -> None:
    global TOP_N_STEP08_CANDIDATES
    parser = argparse.ArgumentParser(description="Joint Step08 x Step09 risk/throttle grid search.")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 1) - 1))
    parser.add_argument(
        "--manual", action="store_true",
        help="Use manual cross-product mode (all of --candidates/--top-n-candidates/--guards/--reserve-*/--every-n-days/--max-starts apply) instead of the default unattended two-stage search (build_auto).",
    )
    parser.add_argument("--top-n-candidates", type=int, default=TOP_N_STEP08_CANDIDATES, help="Manual mode only (or Stage A candidate count in auto mode via --auto-top-n-candidates).")
    parser.add_argument("--candidates", type=str, default=None, help="Manual mode only. Comma-separated Step08 foundation_candidate_name values to restrict the scan to (overrides --top-n-candidates).")
    parser.add_argument("--guards", type=str, default=None, help="Manual mode only. Comma-separated guard_profile names to restrict the scan to (default: all).")
    parser.add_argument("--every-n-days", type=int, default=SCAN_EVERY_N_ACTIVE_DAYS, help="Manual mode only.")
    parser.add_argument("--max-starts", type=int, default=SCAN_MAX_ROLLING_STARTS, help="Manual mode only.")
    parser.add_argument("--reserve-pcts", type=str, default=None, help="Manual mode only. Comma-separated trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer values to sweep for dynamic_projected_open_risk (default: 0.003125).")
    parser.add_argument("--reserve-cost-allowances", type=str, default=None, help="Manual mode only. Comma-separated trailing_buffer_cost_allowance_r values to sweep for dynamic_projected_open_risk (default: 0.05).")
    parser.add_argument("--no-off-control", action="store_true", help="Manual mode only. Skip the reserve='off' baseline row in the sweep.")
    parser.add_argument("--auto-top-k-pairs", type=int, default=AUTO_SURVEY_FRONTIER_MAX_PAIRS, help="Auto mode only: how many (candidate, guard) pairs the cheap survey promotes to the full-reserve-grid confirmation.")
    args = parser.parse_args()

    if not args.manual:
        manifest = build_auto(
            n_workers=int(args.workers),
            top_k_pairs=int(args.auto_top_k_pairs),
        )
        print("V2 Phase B2.08C joint risk/throttle grid search (auto survey+confirm)")
        print(f"Survey: {manifest['survey']['n_step08_candidates']} candidates x {manifest['survey']['n_guard_profiles']} guards @ {manifest['survey']['n_rolling_starts']} starts")
        print(f"Promoted pairs: {manifest['survey']['top_pairs_promoted']}")
        print(f"Confirm: {manifest['confirm']['n_variants']} variants @ {manifest['confirm']['n_rolling_starts']} starts, costs {manifest['confirm']['costs_tested']}")
        print("Best combo:")
        for k, v in manifest["best_combo"].items():
            print(f"  {k}: {v}")
        print(f"best_joint_scenario: {manifest['outputs']['best_joint_scenario']}")
        print(f"best_joint_variant_spec: {manifest['outputs']['best_joint_variant_spec']}")
        return

    TOP_N_STEP08_CANDIDATES = int(args.top_n_candidates)
    candidate_names = [x.strip() for x in args.candidates.split(",")] if args.candidates else None
    guard_profiles_filter = [x.strip() for x in args.guards.split(",")] if args.guards else None
    reserve_pcts = [float(x) for x in args.reserve_pcts.split(",")] if args.reserve_pcts else None
    reserve_cost_allowances = [float(x) for x in args.reserve_cost_allowances.split(",")] if args.reserve_cost_allowances else None
    reserve_configs = None
    if reserve_pcts or reserve_cost_allowances:
        reserve_configs = build_reserve_configs(reserve_pcts, reserve_cost_allowances, include_off=not args.no_off_control)
    manifest = build(
        n_workers=int(args.workers),
        candidate_names=candidate_names,
        guard_profiles_filter=guard_profiles_filter,
        every_n_active_days=int(args.every_n_days),
        max_rolling_starts=int(args.max_starts),
        reserve_configs=reserve_configs,
    )
    print("V2 Phase B2.08C joint risk/throttle grid search (manual)")
    print(f"Variants tested: {manifest['n_variants']}")
    print("Best combo:")
    for k, v in manifest["best_combo"].items():
        print(f"  {k}: {v}")
    print(f"Ranked output: {manifest['outputs']['ranked']}")


if __name__ == "__main__":
    main()
