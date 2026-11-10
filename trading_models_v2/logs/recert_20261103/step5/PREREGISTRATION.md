# RECERT step 5 -- D075 theory re-check at the locked geometry (pre-registered 2026-11-04, before any result)

Rule: docs/mldp/rules/THEORY_CHECK_RULE_D075.md item 5 (RECERT): every edge_universe edge re-runs the counterfactuals
and the raw drift baseline at the RECERT's locked geometry (step 1 locks), pre-registered, with the Gate 0 standards.
The model-filtered drift test follows the allocator (step 9). Failures go to the owner with a recommendation; nothing
is retired automatically.

Edges and locked cells (step 1):
- GBPJPY/dailyrangecompletionfade_long H8_E200_V168 2:1 (counterfactual C1)
- HK/drawdownhazardcontinuation_short H8_E150_V168 2:1 (C1, C2)
- JPN/rangestructure_long H8_E100_V96 2:1 (C1)
- XAUUSD/rangestructure_long H8_E200_V16 2:1 (C1)

Data: the step 2 re-exported feature matrices (187 entries), the edge's own trigger, extraction, primary side and
labeler, gross mid R (no costs). Tool: shared/research/recert_step5_theory.py, which reuses
shared/research/counterfactual_tests/run_gate0_panel.py stage1 and stage2 at cell "L" (locked). The random pool is the
same 20,000 same-side bars (seed 20261102) as run_drift_baseline.py.

Reproduction check (must pass before any result is read): the locked-cell edge set equals the stored model rows'
realized_r (same Datetimes, max abs diff < 1e-9).

Tests (D075 B: one-sided 95 %):
- (a) Drift baseline: month-matched. diff = edge mean R - month-matched mean R (10,000 draws, seed 20261104), SE =
  sqrt(SE_edge^2 + SE_draws^2), SE_edge week-clustered. PASS = diff - 1.645 SE > 0. All-months reported as a diagnostic.
- (b) Counterfactuals: diff = edge mean R - inverted mean R, one week-clustered SE over both sets
  (run_gate0_panel.joint_diff). PASS = diff - 1.645 SE > 0. INCONCLUSIVE if the inverted set has < 100 events.
- (c) Pooled (D075 C): rangestructure_long runs on JPN and XAUUSD, so it is also tested pooled over the two assets.
  - Drift: each event minus its own asset-month's random mean; SE week-clustered over shared ISO weeks, plus the draws'
    SE.
  - C1: pooled edge vs pooled inverted, joint week clustering.
  - Pass rule: PASS = diff - 1.645 SE > 0, and each asset's own diff must be > 0.
  GBPJPY and HK edges are single-asset theses, so they are tested alone.

Edge verdict: PASS only if (a) and every counterfactual pass, pooled where (c) applies, with each asset's own diff
> 0. Reading: for comparison, on 2026-11-02 the same tests at the old locks (two-sided 1.96) failed on all 4 edges.
Theory tests, not strategy trials: nothing is selected on them; every run is logged here.
