# GATE D v4.1 (register D078) -- the meta-model must do what meta-labelling is for

> **D091 (owner 2026-11-09): GATE D IS NO LONGER A GATE.** It is run and reported for every edge, and a FAIL never
> stops an edge from entering config/edge_universe.json. Owner: "we just need to make Gate D not a Gate anymore,
> because whatever the model learns its his business ... before the edge could even reach Gate D, the primary model
> has passed other stuff, so it shows the primary model itself is good alone ... in the allocator ... it always selects
> thresholds that help compound faster, so that is why u see in some edges it selects the all trades threshold".
>
> - Book: meta-labelling is an optional layer on a primary that must stand alone ("You can always add a meta-labeling
>   layer to any primary model", AFML s.3.7 p.53; MLAM s.5.5 pp.69-70). AFML s.14.8 pp.206-207 lists statistics to
>   understand the overlay "in isolation" -- no pass mark, no kill. Killing an edge on a Gate D fail was ours.
> - The primary is judged on every event at a fixed size by Gate 0, Gates A-C, PBO, stress, the D090 re-check and
>   DSR. Those decide the edge.
> - What the meta-model does in deployment is decided by the allocator: 04b sweeps live-calibrated thresholds from the
>   lowest calibrated probability upward (the lowest takes every trade = the primary alone), 04c keeps the one with the
>   best DD-adjusted compound velocity. No separate primary-only design is needed (supersedes D087 item 3).
> - Sizing: the allocator sizes by the payoff-aware AFML s.10.3 bet size from the live calibrated probability (D007,
>   D082). With a no-skill model the calibrated probability sits near the base rate, so sizes are near-constant.
> - D1 / D2 / AUC stay in the Gate D report and the promotion note as diagnostics.

Status: IN FORCE from 2026-11-04 as v4.1 (owner: D1 decision point and D2 weighted throughout approved the same day, after the first run). Applies to every new edge and to
every re-certified edge version. Replaces Gate D v3 (AUC > 0.5, one-sided Mann-Whitney p < 0.05), which stays as a
reported diagnostic. Written before the rule was run on any edge.

Owner approvals (2026-11-04):
- "Gate D replaced by D1 + D2 ... approved". The owner asked for a statistical test, not judgement by eye.
- D2 uses calibrated probabilities: "we are calibrating the probabilities right? so are we not supposed to do this on
  a calibrated probability".
- Cross-fitted calibration is for D2 only; "it is not replacing the current calibration that live uses".
- One-sided 95 % standard as D075 ("lets do 1.645 at both").

## Why (AFML)

- s.3.6 p.50: the meta-model learns "how to use a primary exogenous model", i.e. the size of the bet, including no bet.
- s.3.7 pp.52-53: "Meta-labeling will increase your F1-score by filtering out the false positives ... Its purpose is
  to determine whether we should act or pass on the opportunity that has been presented."
- s.14.8 pp.206-207: the overlay is judged "in isolation" by accuracy, precision, recall and F1.
- p.53 and Ch.10: the meta-model's probability sizes the bet. For that use, s.9.4 p.134 prefers log loss, because
  "investment strategies profit from predicting the right label with high confidence".
- The book never uses AUC as its test. v3 checked ranking only: a model can rank slightly above chance and still lower
  F1, as all 4 RECERT edges did (Step 02 F1 0.50-0.54 against primary F1 about 0.55-0.58).

## The rule

Input: the Step 03 out-of-fold predictions (eligible rows), at the edge level -- no Step 04 threshold, no allocator
threshold. Tool: shared/edges/gate_d_calibration_monotonicity.py (exit 0 = PASS). Report:
<edge>/reports/gate_d_calibration_monotonicity_report.json.

**Cross-fitted calibration (shared by D1 and D2).**

Events sorted by time are split into 10 contiguous blocks. For each block, the live calibrator's method is fitted on
the other blocks only, then applied to the held-out block's path forecasts. The method is a Platt sigmoid on logit(raw),
fitted on all CPCV paths stacked, WITH the AFML Ch.4 return-attribution weights -- exactly as Step 04's
fit_final_live_sigmoid since D082.
- Purged: training events whose [t0, label t1] overlaps the block's span are dropped.
- Embargoed: max(100, ceil(1 % of rows)) events after the block are dropped (AFML s.7.4).
- b = the AFML-weighted positive rate of the same training rows.
- An event's P(win) = the mean of its calibrated path forecasts.

The test's calibrations are cross-fitted only to keep the test honest. The live calibrator is the same method fitted on
all rows.

**D1 -- act or pass (s.3.7, s.14.8).**

The two F1 scores, on the same events:
- Primary model: takes every signal, so precision = base rate, recall = 1, and F1_primary = 2b / (1 + b).
- Meta: acts when the cross-fitted calibrated P(win) > b (owner 2026-11-04). F1_meta = 2 TP / (2 TP + FP + FN),
  unweighted counts as in s.14.8.
- Delta = F1_meta - F1_primary.

Why not raw p > 0.5: the return weights correlate with the label on 2:1 geometry (winners carry 1.5-1.9x the weight),
so the balanced classifier's neutral point is about 0.60-0.65, and p > 0.5 acted on 74-100 % of events.

Statistical test: ISO-week block bootstrap, 10,000 draws, seed 20261104, both F1 recomputed on each draw (paired).
**PASS if the 5th percentile of Delta > 0.**

**D2 -- sizing (s.9.4, Ch.10), weighted throughout (owner 2026-11-04).**

Per-event statistic: d_i = log loss(b) - mean over the event's CPCV paths of log loss(calibrated). The weighted
calibrator and weighted base rate are as above. The loss is weighted by AFML Ch.4 return attribution (Snippet 4.10,
class balance excluded).

Basis:
- AFML s.4.6 pp.68-69: labels are weighted by absolute return.
- Snippet 7.4 cvScore: log_loss is scored with sample_weight.
- s.9.4 pp.134-135: sample-weighted cross-entropy "estimates the classifier's performance in terms of variables
  involved in a PnL ... calculation" and is "the right ML performance metric ... for financial applications".
- s.10.3: the bet is sized from the classifier's own probability.

Statistical test: **PASS if the weighted mean of d_i - 1.645 x SE > 0**, SE week-clustered.

**Gate D PASS = D1 PASS and D2 PASS.** Both are required, which is conservative, so there is no multiple-testing
adjustment.

**Reported, not gating:**
- v3 AUC and Mann-Whitney p;
- log loss with the in-sample live calibrator, and with the raw path probabilities;
- Kendall tau(raw_probability, quote_executable_r);
- F1, precision, recall and acted share.

A Gate D FAIL is a real finding: the edge does not enter config/edge_universe.json (runbook Section 6A Step 13).

## Settled at the first run (2026-11-04)

v4.0 (the first code) had two problems; both were settled by the owner the same day, before Gate D v4 gated any edge.
1. D1 acted when raw p > 0.5. It now acts when the calibrated P(win) > b.
2. D2 scored an unweighted calibrator with a weighted loss. On the 4 removed RECERT edges, all-unweighted failed all 4
   and all-weighted passed all 4. The owner chose weighted throughout (the book's answer) and also had the live
   calibrator fitted weighted (D082), so live sizes with the same kind of probability Gate D approves.
