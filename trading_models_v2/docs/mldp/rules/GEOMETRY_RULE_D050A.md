# GEOMETRY RULE D050-A — search the geometry grid, and pay for every trial

**Status:** IN FORCE since 2026-10-29 (owner decision: "I want us to go back and use this option 2, i prefer to search the
405 grid cells and pay for it, that is how i have been doing it for the previous edges and would want to continue doing
it that way, as long as the book says we can still follow this path"). Replaces the short-lived D050-B / D050-C drafts of
the same day (reverted). This file is the canonical text. Runbook: Section 6A STEP 7, block marked `[RULE D050-A]`,
regenerated from this file by `docs/tools/edit_runbook_rev24.py`. Register: D050.

## Book basis

- The book's two paths for the trading rule (barrier geometry): (1) avoid calibrating it on history -- its stated
  preference (AFML Ch.13 p.170: "it would be better to avoid the risk of overfitting", via theory or synthetic-data OTR);
  (2) search, then measure and deflate the search: PBO / CSCV over all N trials (AFML s.11.6 pp.155-157), the deflated
  Sharpe ratio with all trials and the third law (AFML s.14.7.3 pp.204-205; MLAM s.8.7 E[K] and V by ONC clustering).
- The owner chose path (2). It is a recorded choice of the book's second path, not of its preferred one (register D050);
  our data on the trade-off: docs/mldp/AFML_MLAM_WALKTHROUGH.md "D050 decision analysis".

## THE RULE

1. **Thesis first.** The thesis card is written (provenance pre_registered) before the first backtest (owner rule
   2026-10-26, enforced by the edge.yaml contract). The geometry is NOT fixed on the card.
2. **Search.** Gates A-C search the runbook grid exactly (405 cells: lookbacks 1,2,4,6,8 h x EWM 100,150,200 bars (register D074, 2026-10-31; was 20,50,100) x verticals
   1,2,4,8,16,24,48,96,168 h x barriers 1:1, 1.5:1, 2:1; never lab defaults). Gate C replays every cell (D051) and the
   winner is argmax compound velocity among cells with exec_mean_R > 0 and coverage >= 99 % (runbook Section 6A Step 7).
3. **Pay -- all mandatory, nothing optional:**
   - PBO / CSCV over EVERY replayed configuration of the run (D051): PBO < 0.5 AND Prob[OOS <= 0] < 0.5.
   - Level-1 DSR over every Gate C trial of the edge (ONC E[K], Lopez de Prado & Lewis 2018 cluster weights): DSR > 0.95
     AND FWER alpha_K < 0.05 (D054); Level 2 (thesis search on the asset) reported alongside.
   - Strategy risk on bets P[p < p_theta*] <= .05 (theta* 0, k 2; D055).
4. **Trials accumulate, never reset.** Every grid run on an edge -- including a re-selection after a cost-model or
   label-definition change -- adds its cells to that edge's trial record; the DSR record is computed over all of that
   edge's Gate C runs (pass every run's stamps to shared/research/dsr_all_trials.py; a cell replayed in several runs
   counts once).
5. **Report every result with all its trials** (third law): Gate C / PBO / DSR records name the run stamps and the trial
   count; a positive-only or subset trial set is a diagnostic only.

## What is NOT allowed (drift checklist)

- Using the lab's default grid or any grid other than the runbook's 405 cells.
- Running new Gate A-C searches on the old EWM 20,50,100 spans (replaced by D074 on 2026-10-31).
- Judging PBO or DSR on a subset of the trials (positive-only, top-k, one run when several were made).
- Dropping earlier runs' trials after a re-selection.
- Promoting an edge whose PBO, DSR, FWER or strategy-risk gate fails (DSR / strategy-risk gates apply from the RECERT).
