# Thesis-card counterfactual tests (MLAM s.1.1 p.2, s.1.2.2 p.4) -- written before any result

Owner 2026-10-31: "Yes, pre-register and run the 12 tests in parallel". Theory tests of the 6 owner-approved thesis
cards (edge.yaml `thesis.counterfactuals`); not strategy trials (nothing is selected on them). Research data only; the
holdout stays sealed (the cards' "research+holdout" = research now, holdout at the final one-look).

## Common method (gate-inversion tests)

- Input: the edge's stored research dataset (artifacts/research_dataset/*_feature_matrix.csv, the bars the current
  models were trained on) and the edge's research bars (runtime._load_asset_bars()).
- Trigger fields: the edge's own candidate_events.py feature function (unchanged code). The edge's gate column is then
  recomputed with ONE clause replaced as stated per test below; every other clause, the event extraction / spacing
  function, the primary-side rule and the dropna are the edge's own.
- Labels: the edge's own model_rows.apply_triple_barrier_labels (locked geometry, tick-resolved mid path, D016), its
  pre-label gate validator disabled (it would reject the inverted events by design). Measure: realized_r (gross mid R).
- Reproduction check (must pass or the edge's tests are void): the same path with NO clause replaced must reproduce the
  edge's stored model rows -- same event times, realized_r equal.
- Statistic: mean R and 95 % CI = mean +- 1.96 x week-clustered SE (ISO weeks of the event time).
- PASS = inverted-set mean R < the edge's mean R AND the inverted set's CI lower bound <= 0 (the card: "no edge" /
  "CI must include 0 / be below the edge's"). Cards that say "weaker or no expectancy" (vc C1): PASS = inverted mean
  < edge mean. FAIL otherwise. INCONCLUSIVE if the inverted set has < 100 labeled events.

## The 12 tests

| Edge | Test | Replaced clause (edge code -> test) |
|---|---|---|
| GBPJPY drcf long | C1 | Daily_Range_Completion >= 0.70 -> < 0.50 (card: "< 50% of expected range") |
| GBPJPY drcf long | C2 | not a gate test: the edge's Gate C trades of its locked cell H6_E100_V168_PT2_SL1 (full replay 20261026T185441Z, Moneta cost model) with swap added back; PASS = mean R before swap > 0 with CI lower bound > 0; mean > 0 with CI incl. 0 = INCONCLUSIVE |
| HK dhc short | C1 | Drawdown_Hazard_Probability >= 0.52 -> < 0.48 (card) |
| HK dhc short | C2 | Recovery_Fraction <= 0.40 -> > 0.40 (card: "recovery > 40%") |
| JPN rs long | C1 | abs(Structure_Bias_TStat) >= 0.5 -> < 0.5 (side rule unchanged: t > 0 kept, so 0 < t < 0.5) |
| JPN rs long | C2 | branch test: the edge's own labeled events split by Range_Structure_Event_Type (+1 near-high, -1 near-low); each branch PASS = mean R > 0 and CI lower > 0; mean > 0 with CI incl. 0 = INCONCLUSIVE; mean <= 0 = FAIL |
| JPN vc long | C1 | Is_Calm_Vol -> not Is_Calm_Vol (Volatility_Ratio <= 1.25 and Drift_TStat >= 0.5 kept) |
| JPN vc long | C2 | Drift_TStat >= 0.5 -> 0 < Drift_TStat < 0.5 (calm and ratio kept; card "|t| < 0.5", positive side as the long edge) |
| XAU rs long | C1 | as JPN rs C1 |
| XAU rs long | C2 | as JPN rs C2 |
| XAU vc long | C1 | as JPN vc C1 |
| XAU vc long | C2 | as JPN vc C2 |

JPN vc and XAU vc are out of edge_universe (Gate C / Gate E); their cards are tested anyway (6 approved cards).

A failed counterfactual means the card's stated cause is not supported on research data: the edge is flagged for the
owner at the RECERT; the card is NOT rewritten after the fact. Driver: shared/research/counterfactual_tests/run_counterfactuals.py;
outputs here (<asset>_<edge>.json + logs).
