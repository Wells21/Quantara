# Drift baseline ("random entry, same side") -- written before any result, 2026-11-02

Owner 2026-11-02: "Yes, run the drift baseline in parallel". Follow-up to the 12 counterfactual tests
(PREREGISTRATION.md): 4 of 6 cards' inverted gates earn about the edge's R, which suggests the edges may be
collecting the instrument's general drift. This test asks one question per edge: does the edge's event set earn more
gross mid R than entries at random bars on the same side, same locked geometry, same period?
Theory test (MLAM s.1.1 p.2 counterfactual; s.1.2.2 p.4), not a strategy trial: nothing is selected on it.
Research data only; holdout sealed.

## Method

- Edge events: the edge's own events, labeled exactly as in run_counterfactuals.py (reproduction check must pass:
  same event times as the stored candidate events, realized_r equal to the stored model rows).
- Random pool: 20,000 bars drawn uniformly (numpy seed 20261102) from the edge's own feature frame, restricted to bars
  with a finite Event_Vol, between the first and last edge event, and not themselves edge-event bars. Each pool bar is
  given the edge's side (long +1; HK short -1) and labeled with the edge's own apply_triple_barrier_labels (locked
  geometry, tick-resolved mid, pre-label validator off). Measure: realized_r (gross mid R).
- Baseline U (unconditional): mean R of the whole labeled pool, 95 % week-clustered CI.
- Baseline M (month-matched, primary): for every labeled edge event, one pool bar from the same calendar month
  (with replacement); 10,000 such draws. Baseline-M mean = mean of the draw means; its SE = sd of the draw means.
  Controls for "the edge fires more in the months the instrument rose".
- Baseline H (hour-of-day-matched, diagnostic): as M but matched on UTC hour instead of month.

## Verdict (primary = M)

difference = edge mean R - baseline-M mean R; SE = sqrt(SE_edge^2 + SE_M^2), SE_edge = the edge's week-clustered SE.
95 % CI = difference +- 1.96 SE.
- ADDS BEYOND DRIFT: CI lower bound > 0.
- NOT DISTINGUISHABLE FROM DRIFT: CI includes 0.
- WORSE THAN DRIFT: CI upper bound < 0.
The same difference/CI is reported against U and H (diagnostic), plus one-sided permutation p-values
(share of draws whose mean >= the edge mean) for U, M, H.

Result for an edge that is NOT DISTINGUISHABLE: flagged for the owner at the RECERT together with its counterfactual
result; nothing is rewritten, retired or re-selected on this test. Driver:
shared/research/counterfactual_tests/run_drift_baseline.py; outputs here as <ASSET>_<edge>_drift.json.

## Addendum 2026-11-02 -- model-filtered drift test (written before any filtered result)

Owner 2026-11-02: "yes you can so we can get the full picture." Same random pool (same seed, so the same 20,000 bars;
the rerun must reproduce the first run's pool mean exactly), now saved as <ASSET>_<edge>_drift_pool.csv.
- Taken trades = rows of the edge's calibrated OOF predictions (artifacts/calibration/*_calibrated_oof_predictions.csv,
  purged out-of-fold) with live_calibrated_probability >= the edge's final_live_threshold in
  portfolio_live/live_manifest.json (the threshold the live bot used; reference_allocator_threshold_chain). R =
  realized_r of those rows (gross mid R).
- F1 (primary): taken trades vs the month-matched random pool, same difference / week-clustered CI / verdict wording as
  above. U and H reported as diagnostics.
- F2 (diagnostic): taken minus not-taken OOF rows (does the model separate its own events?), CI from both sets'
  week-clustered SEs. Not computed if either set < 100 rows.
Caveat stated in advance: the live threshold was chosen by the allocator on these same OOF rows, so F1 is biased in the
edge's favour; a NOT DISTINGUISHABLE result is therefore the stronger statement.
Driver: shared/research/counterfactual_tests/run_drift_filtered.py; output <ASSET>_<edge>_drift_filtered.json.
