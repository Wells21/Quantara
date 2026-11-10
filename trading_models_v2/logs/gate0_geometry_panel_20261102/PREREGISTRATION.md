# Gate 0 geometry sensitivity panel -- written before any result, 2026-11-02

Owner 2026-11-02: "yes" (run the 9-cell sensitivity panel before fixing the Gate 0 geometry) and "Approved" (counterfactual
PASS = edge minus inverted, 95 % CI lower bound > 0, joint week clustering). Theory tests (MLAM s.1.2.2 p.4, s.8.2
p.106), not strategy trials: nothing is selected on them. Research data only; holdout sealed.

## Question

Does the Gate 0 verdict (drift baseline and counterfactuals) depend on the labeling geometry? If not, one fixed reference
cell is enough; if it does, Gate 0 needs a panel.

## Cells

Symmetric barriers 1:1 throughout (AFML s.3.5 p.48: learning the side requires symmetric barriers) and EWM 100 bars
(AFML Snippet 3.1 span0). 9 panel cells = event-vol lookback {1, 4, 8} h x vertical {4, 24, 96} h. Plus cell L = the
edge's locked cell (its own lookback / EWM / vertical / PT:SL), used only to re-state the 2026-10-31 / 11-02 results
under the joint-clustered rule; L is not part of the panel decision.

## Method (per edge, per cell)

- Event sets: the edge's own trigger, extraction and primary side, computed ONCE with the edge's locked trigger
  configuration (the event SET does not change with the cell). Only the labeling changes: Event_Vol recomputed with
  shared/labels/calendar_time_barriers.calendar_time_event_vol (cell lookback, EWM 100, the edge's min vol), the edge's
  own apply_triple_barrier_labels with PT_MULTIPLIER = SL_MULTIPLIER = 1 and the cell's vertical (pre-label validator
  off; for rangestructure the pass-through Structure_Bias_TStat tag on random bars, as in run_drift_baseline.py).
- Reproduction (must pass): cell L reproduces the stored model-row realized_r (max diff < 1e-9).
- Random pool: the same 20,000 bars as run_drift_baseline.py (seed 20261102, same eligibility), same side.
- Drift baseline (D075 (a)): edge minus month-matched random, SE = sqrt(SE_edge^2 + SE_draws^2), SE_edge week-clustered,
  10,000 draws. PASS = CI lower > 0.
- Counterfactuals (D075 (b), approved rule): the 2026-10-31 inversions (GBPJPY C1; HK C1, C2; JPN/XAU rs C1; JPN/XAU vc
  C1, C2). diff = edge mean R - inverted mean R; SE jointly week-clustered: per ISO week w,
  g_w = sum_{edge events in w} (r - m_E)/n_E - sum_{inverted events in w} (r - m_I)/n_I, SE = sqrt(sum_w g_w^2).
  PASS = diff - 1.96 SE > 0. INCONCLUSIVE if the inverted set has < 100 labeled events.
  (The rangestructure C2 branch split is not repeated: one-branch edges, 1-3 near-low events.)
- Gross mid R, no costs.

## Decision rule (stated before the results)

For each edge x test (drift + each counterfactual with >= 100 events), count the panel cells (of 9) that PASS.
- STABLE if the same verdict holds in >= 8 of 9 cells, for every edge x test: Gate 0 keeps one reference cell
  (4 h, EWM 100, 24 h, 1:1).
- UNSTABLE otherwise: the verdict depends on geometry, and I recommend the owner adopt the 9-cell panel for Gate 0. The
  pass rule over the panel (e.g. a majority of cells) is the owner's decision after seeing these results.
Also reported: how mean R of edge events and random entries scales with lookback and vertical (drift in R vs geometry).

Driver: shared/research/counterfactual_tests/run_gate0_panel.py (stage 1 builds and caches each edge's event sets and
pool; stage 2 labels one cell per job, 8 jobs at a time at BelowNormal). Outputs here.
