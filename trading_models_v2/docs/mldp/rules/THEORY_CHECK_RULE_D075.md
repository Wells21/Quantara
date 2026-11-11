# THEORY CHECK RULE D075 -- IN FORCE since 2 November 2026 (owner); amended 4 November 2026; Gate 0 pass rule REPLACED by D090 on 9 November 2026

Owner approval 2026-11-02: "i agree and approve"; 2nd round 2026-11-02: joint-clustered
counterfactual rule "Approved", keep both raw Gate 0 and the filtered promotion check, geometry panel "yes";
3rd round 2026-11-02: leniency A + B + C "Approved" (MLAM Ch.1 walkthrough, docs/mldp/AFML_MLAM_WALKTHROUGH.md §4W).
Register D075. Runbook: Quantara_Runbook.docx Section 2 (standing rules) and Section 6A Steps 4B, 6B, 7, 12, Appendix 49.

**Amendment 2026-11-04 (owner, RECERT):** the theory check runs only at the edge level, so no edge-level gate depends
on the allocator.
- Item 4, the model-filtered drift test at the allocator's final_live_threshold, is DELETED ("Delete item 4 ...
approved").
  - An edge enters config/edge_universe.json only after every edge-level gate passes, and the allocator reads only
    deployed edges, so a check that needs the allocator's threshold cannot decide entry.
  - On 2026-11-02 it gave the same verdict as the raw event set on all 6 edges.
- Both remaining checks use one-sided 95 % ("lets do 1.645 at both").
- RECERT edges re-run both checks at their locked geometry before any gate that removes edges.
- Runbook Revision 43, Appendix 60.

## D090 -- IN FORCE since 9 November 2026 (owner). SUPERSEDES the pass rules below where they differ.

Owner 2026-11-09, after the Gate 0 review: "Make Gate 0 test the theory, not the raw trigger's average. The
counterfactual tests become the main gate, as MLAM asks. The month comparison stays as a reported diagnostic ...
i approve this"; and "all assets must not agree for an edge in a particular asset to work". Register D090.

**Why.** MLAM asks that a theory be tested for facts and counterfacts (s.1.2.2 p.4, s.1.4.2 p.9). It does not ask
the raw trigger to beat random entries, and AFML s.3.6 (meta-labeling) expects a high-recall primary whose precision
the meta-model raises. The month-matched drift test was our addition (2026-11-02), and it cannot credit any effect
that works at the monthly scale (T22). Pooling plus the every-asset agreement rule let other assets veto an asset's
own evidence.

**Gate 0 (Step 6B), per asset and per side, on that asset's own events.** PASS needs all of:
1. **Main counterfactual C1** (the stated cause removed): own diff - 1.28 SE > 0 on the full history (one-sided 90 %,
   the Rev 47 level; joint week-clustered SE, as before).
2. **Every other card counterfactual:** own diff > 0 on the full history.
3. **Recency:** own C1 diff > 0 on 2022-01-01 onward.
4. **Events:** >= 100 edge events, and >= 100 events in every counterfactual; else INCONCLUSIVE.
5. **Firing rate:** on 2022-01-01 onward, LONG >= 2 edge events per week (D092, owner 2026-11-10; was >= 3 under D089);
   SHORT >= 1 per week (D094, owner 2026-11-11).
6. **Universe:** only assets in the card's pre-registered universe get a verdict (the mechanism must exist there);
   the other assets are computed and reported as PLACEBO.

**No pooling requirement.** Another asset's result never blocks this asset. The pooled result is a diagnostic only.

**The month-matched drift baseline is a DIAGNOSTIC.** It is always computed and reported (all-months and hour-matched
too). It no longer decides anything.

**The drift-riding guard is now the counterfactual.** C1 must be DRIFT-NEUTRAL: it removes ONLY the stated cause and
keeps the same asset, the same side, the same period and a comparable event frequency, so that both sets carry the same
market drift. An edge that only rides the drift then earns the same as its C1 and fails. This is what the 2026-11-02
audit found on 4 of 6 old cards (the inverted trigger earned as much as the edge). A card whose C1 differs from the edge
in drift exposure for reasons other than the stated cause (a different side, a different period) is not testable:
rewrite the card before any run.
- Correction, 2026-11-09: when proposing D090, Claude said drift riding would be "caught later, at Gate D and in the
  strategy-risk checks". That was not accurate. Gate D tests the meta-model's precision and calibration, and strategy
  risk tests the payout/precision economics; neither tests drift. The guard is the drift-neutral C1 above.

**Post-Gate C re-check (item 3) and RECERT (item 5).** The same D090 test (C1 at 90 %, the other counterfactuals > 0,
C1 recency on 2022+) repeated at the LOCKED cell on the edge's asset. The drift baseline is reported. Tool:
GATE0_CELL="lookback:ewm:vertical:pt:sl" GATE0_ASSETS=<ASSET> gate0_tool.py, then gate0_verdict_d090.py.

**Unchanged.** Pre-registration (card, universe, cell, counterfactuals written before any data), one fixed Gate 0
cell, gross mid R, no re-tuning after a FAIL (MLAM p.8), every run logged; Gate 0 is a theory test, not a strategy
trial, so it does not enter PBO / DSR. Gates A, B, C, PBO, stress, Gate D, DSR and the deployment gates are unchanged.

**Long and short of one contrast.** Where a card's LONG edge is the SHORT's C1 and vice versa (T18, T20, T21), the two
sides are one comparison, not two pieces of evidence. Both may go to Gate A; the record says so.

**Prospective.** D090 applies to cards from 2026-11-09. Stored Gate 0 results were re-read under D090 (read-only, no
re-run; logs d090_retro_*.log and gate0_*_verdict_d090.json beside each card). Those passes are POST-HOC: they go to
Gate A only by owner order, logged as post-hoc in CAMPAIGN.md (as T1b was).

**Tools.** gate0_tool.py writes window_tests_d090 (items 1, 2, 4, 6 inputs);
shared/research/gate0/gate0_verdict_d090.py THESIS OUT_DIR [TAG] combines the full-history and 2022+ runs into the
verdict (items 3, 5, 6). The verdict fields verdict_NASDAQ / verdict_by_asset / verdict_by_asset_rev47 are kept for the
record only.

## Why (book)

- MLAM s.1.1 p.2: an edge exists only as someone else's systematic mistake; "Without a testable theory that explains your
  edge, the odds are that you do not have an edge at all." A backtest is "a simulation of a past that never happened".
- MLAM s.1.2.1 p.3: backtesting is not a research tool. MLAM s.1.2.2 p.4: a theory explains positives (x causes y) and
  negatives (the absence of y is due to the absence of x). MLAM s.1.4.2 p.9: backtests "do not tell us why. They are
  not a controlled experiment"; theories are tested "for facts and counterfacts".
- MLAM s.1.4.2 p.8: the backtest-tweak-backtest cycle is futile -- a failed theory is not re-tuned until it passes.
- AFML s.1.3.1.3 p.7: a strategy is the experiment that tests a theory.

The book sets no threshold and calls nothing a gate. This rule is our operationalisation (runbook addition, owner
approved); the thresholds below are ours.

## What it found (why it is needed here)

2026-10-31 / 11-02 on the 6 Gate C edges (walkthrough §4W.1-4W.3): on 4 of 6 cards the inverted trigger earned as much
as the edge; all 6 raw event sets were NOT DISTINGUISHABLE from random same-side entries in the same months (random
entries alone earn +0.07..+0.10 gross mid R on every asset). Gates A-D measured the profit; none of them asked why. A
book of edges that carry the instruments' drift loses together when the drift turns (2026-10-15 drawdown).

## The rule

1. **Card (Step 4B).** Every counterfactual on the thesis card names the trigger clause, its inversion, and the
   prediction, and is testable on research data (>= 100 events when inverted). The mechanism names who loses and why.
   The card also pre-registers its Gate 0 cell (rule A below) before any test.
2. **Gate 0 (Step 6B), before Gate A, every new thesis -- new or ported, every asset, both sides.**
   - Pre-register first: logs/theory_check/<date>_<thesis>_<side>/PREREGISTRATION.md, written before any result.
   - **A -- Gate 0 cell, pre-registered on the card (owner approved 2026-11-02).** One cell from the panel: barriers 1:1
     (AFML s.3.5 p.48: learning the side requires symmetric barriers), EWM 100 bars (AFML Snippet 3.1 span0), event-vol
     lookback 1, 4 or 8 h x vertical 4, 24 or 96 h, chosen to match the thesis's stated horizon and written in edge.yaml
     before any Gate 0 result. Card silent -> H4_V24. Never chosen or changed after seeing a result. D083 (owner
     2026-11-04: "approved"): H1_V1 (1 h lookback, 1 h vertical) is also allowed for a thesis whose stated horizon is
     about 1 hour. Basis: the
     2026-11-02 panel (logs/gate0_geometry_panel_20261102/, walkthrough §4W.5): verdicts identical in all 9 cells on the 6
     Gate C edges; edge-minus-random flat across cells; short cells have the tightest CIs.
   - The edge's own trigger, extraction, primary side and labeler; gross mid R (no costs).
   - **B -- one-sided 95 % (owner approved 2026-11-02).** Every Gate 0 claim is directional (edge beats random; removing
     the clause lowers R), so Gate 0 uses the one-sided 95 % bound: PASS = diff - 1.645 SE > 0.
   - (a) Drift baseline: the event set vs 20,000 random bars of the same side (same period, same cell, same labeler).
     Primary comparison: month-matched (each event replaced by a random bar from the same calendar month), 10,000
     draws. diff = edge mean R - month-matched mean R, SE = sqrt(SE_edge^2 + SE_draws^2), SE_edge week-clustered.
     PASS = diff - 1.645 SE > 0. All-months and hour-matched comparisons reported as diagnostics.
   - (b) Counterfactuals: every card counterfactual, one clause inverted at a time. diff = edge mean R - inverted mean
     R, with ONE week-clustered SE over both sets (joint clustering, owner approved 2026-11-02): per ISO week w,
     g_w = sum_edge (r - m_E)/n_E - sum_inverted (r - m_I)/n_I, SE = sqrt(sum_w g_w^2) (run_gate0_panel.joint_diff).
     PASS = diff - 1.645 SE > 0. INCONCLUSIVE when the inverted set has < 100 events: the card must name a
     counterfactual that can be tested, else the owner decides.
   - **C -- pooled across assets (owner approved 2026-11-02).** The coverage rule tries every thesis on every asset, so
     Gate 0 runs the thesis on all assets at once, per side. Pooled test: all assets' events together, diff = pooled
     edge mean R - pooled comparison mean R (each event's comparison drawn from its own asset), SE week-clustered over
     ISO weeks shared across assets (captures cross-asset correlation); PASS = diff - 1.645 SE > 0, for (a) and for each
     counterfactual. Consistency: the per-asset point estimate diff must be > 0 on every asset with >= 100 events;
     an asset with diff <= 0 does not proceed even if the pool passes. Per-asset results are always reported. A thesis
     that fires on one asset only is tested on that asset alone.
     **Amended 2026-11-04 (owner: "approved"), for cards written from that date:** the card pre-registers its asset
     UNIVERSE -- the assets where its mechanism exists -- before any data. Gate 0 pools over that universe only, and
     the per-asset consistency check applies within it. Every asset is still computed and reported (coverage rule).
     Why: T1 (NASDAQ campaign) failed pooled over all 10 assets because assets its own card listed under "should fail"
     dragged the pool. One owner-ordered post-hoc re-test of T1 on its universe (T1b) was run and logged as a separate
     trial.
   - Gate 0 passes on an asset only when (a) and every counterfactual pass (pooled where C applies) and that asset's
     own diffs are > 0.
   - A Gate 0 FAIL kills the thesis as written. No re-tuning of the trigger thresholds to make it pass (MLAM p.8). A
     revised mechanism is a new card and a new attempt, counted in the trial counters and the cross-asset coverage
     ledger.
   - Gate 0 tests are theory tests, not strategy trials: nothing is selected on them (they do not enter PBO / DSR),
     but every run is logged.
3. **Confirmation after Gate C (Step 7).** The drift baseline is repeated at the locked geometry on the raw event set
   (single asset, Gate 0 standard: one-sided 95 %, diff - 1.645 SE > 0; amended 2026-11-04: this is the last theory
   check and the same standard as Gate 0). It must still PASS before Gate D.
4. *(Deleted 2026-11-04.)* The model-filtered drift test at the allocator's final_live_threshold is no longer part of
   this rule. The tool run_drift_filtered.py is kept only to reproduce the 2026-11-02 record.
5. **RECERT (existing edges).** Every edge in edge_universe re-runs, pre-registered, at the RECERT's locked geometry:
   the counterfactuals and the drift baseline (items 2-3), before any gate that removes edges. An edge that fails is
   presented to the owner with a recommendation; the owner decides (nothing is retired automatically). 2026-11-04: all
   4 RECERT edges failed and the owner removed them (logs/recert_20261103/RECERT_LOG.md).

**Tiered drift bar (owner 2026-11-04: "approved"; future cards):** the drift baseline (Gate 0 and the post-Gate C
re-check) needs one-sided 90 % (diff - 1.28 SE > 0) when EVERY counterfactual of the card passes at one-sided 95 %;
otherwise one-sided 95 %.
- Why: the drift test alone had about 41 % power for a 0.05 R effect at 4 years of data. Passing counterfactuals raise
  the prior odds that the mechanism is real (MLAM s.8.2 precision; s.8.8.4 p.121 on ignoring type II errors). C1-type
  sign inversions are drift-neutral.
- The counterfactuals themselves stay at 95 %.
- Applied once post-hoc, by owner order, to T1b (NASDAQ campaign): SHORT passed, LONG failed.
- The owner declined extending history (no disk space for 6 more years); the research data stays at about 4 years.

Leniency (owner 2026-11-02, price-only data): A, B and C add power or fit, not a weaker bar against drift. Both theory
checks (Gate 0 and the post-Gate C re-check) use one-sided 95 % (owner 2026-11-04).

## Tools

shared/research/counterfactual_tests/run_counterfactuals.py (gate inversion), run_drift_baseline.py (raw drift
baseline, saves the random pool); run_drift_filtered.py is RETIRED (item 4 deleted). New theses (2026-11-04):
shared/research/gate0/gate0_tool.py <THESIS> <OUT_DIR> (venv312) -- every asset per side, pooled over the card's universe
(rule C), joint-SE counterfactuals, tiered drift bar; each thesis is coded in its THESES table and points to its card. RECERT driver:
shared/research/recert_step5_theory.py (counterfactuals + drift at the locked cell, one-sided, pooled). A version that reads the card's counterfactuals and Gate 0 cell
itself stays a backlog item (D075, code_pending). run_gate0_panel.py (geometry panel, joint_diff).

## Drift checklist

- [ ] PREREGISTRATION.md exists and predates the results.
- [ ] Reproduction check passed (the test path regenerates the edge's own events / model rows).
- [ ] Short theses compared with random SHORTS; long with random longs.
- [ ] No trigger threshold changed after a Gate 0 result.
- [ ] Gate 0 cell written on the card before any result (rule A); one-sided 1.645 at Gate 0 and at the post-Gate C re-check.
- [ ] Pooled result AND every per-asset diff reported (rule C).

## Row floor (2026-11-04)

The >= 2000 model-row floor is removed (owner; register D085, runbook Revision 46). It never applied to Gate 0, but the
counterfactual minimum here (>= 100 inverted events, else INCONCLUSIVE) and the >= 100-event per-asset consistency count
are unchanged.
