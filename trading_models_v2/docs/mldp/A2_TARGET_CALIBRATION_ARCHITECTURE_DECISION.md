# A2 — Target/Calibration Architecture Decision (P0.2)

## Status

`FROZEN AS DOCTRINE`. This document is the decision the blueprint's P0.2
called for. It does not itself modify any edge's Step 01-09 code — that is
Task 2C / Stage B (rebuilding the pilot edge), which this decision now makes
well-defined. Nothing here re-opens Task 2B's population rule
(`docs/mldp/TASK2B_POPULATION_RULE_DECISION.md`); this decision consumes
that rule as given.

## The problem this closes

Quantara's current (pre-migration) edges train their secondary meta-model on
the gross label `gross_meta_label = 1[gross_r > 0]`, produce
`P(gross positive R | X)`, and then Step 04 fits a sigmoid mapping that
same model's probability onto a **different** target,
`executed_meta_label = 1[executed_r > 0]`. That is not probability
calibration of the original event — calibration must recover a well-
calibrated estimate of the probability of the event the model was actually
trained to predict (AFML Ch.9's log-loss discussion; AFML Ch.10's bet-sizing
derivation assumes the input probability means exactly one fixed thing). This
is a silent target-identity change, and it is the single most material
open item in the whole transformation: Task 2A's own pilot audit (see
`docs/mldp/TASK2A_EXECUTABLE_TARGET_ENGINE.md`) measured a real 7.8%
gross-to-executable label flip rate on `BTCUSD/absorption_long`, i.e. this is
not a theoretical concern.

## The decision

**Adopted: the "executable meta-label as secondary target" architecture**
(the blueprint's preferred option over the two-model alternative, precisely
because it is simpler and closer to AFML's actual meta-labeling intent for
Quantara's purpose — the primary rule/theory supplies direction, and ML's
only job is to decide whether the resulting bet should be taken):

1. **The primary rule/theory continues to supply side** (unchanged — this is
   already how side-specialized edges work; nothing about this decision
   touches primary-side logic).
2. **Execution resolution happens before secondary-model training, not
   after.** This is already built (Task 2A's audit engine resolves every
   event's `quote_executable_r`/`quote_executable_meta_label` from the exact
   Bid/Ask replay). The change is architectural, not computational: the
   resolved target must be available and frozen *before* Step 02 trains,
   not derived from the model's own gross output at Step 03A as today.
3. **The secondary classifier trains directly on
   `quote_executable_meta_label`**, restricted to the population Task 2B
   already defines as eligible (`mldp_target_eligible == True`, i.e. genuinely
   resolved events only — `TRUE_NO_FILL` and `RIGHT_CENSORED_END_OF_HISTORY`
   rows are never labeled, never coerced, and never enter the training set;
   `DATA_DEFECT` rows must not exist at all, per Task 2B, and their presence
   blocks this stage entirely, not just the audit).
4. **OOF probabilities from Step 03 therefore already estimate exactly the
   event that matters** — no separate "Step 03A retargeting" step exists
   anymore in the corrected architecture. Step 03A's role (computing
   executable outcomes) has already moved to Task 2A/2B, ahead of training,
   not after it.
5. **Step 04 becomes genuine same-target calibration**: fit the sigmoid
   calibrator mapping raw OOF probability to calibrated probability, out of
   fold, against `quote_executable_meta_label` — the same label the model
   was trained on. There is no target switch at this step anymore, because
   there is nothing left to switch to; the model already learned the right
   thing.

**Not adopted (for now): the two-model alternative** (a separate gross alpha
model plus an explicit execution-survival/cost model, combined through a
predeclared rule). This remains available as a future refinement if a gross
diagnostic model is independently useful, but it is not the default
architecture, per the blueprint's own stated reasoning that it adds
complexity Quantara's current purpose does not need.

## Frozen target field, precisely

- **Training/calibration target**: `quote_executable_meta_label` (the
  nullable Int64 column Task 2A's engine already produces).
- **Not `net_executable_meta_label` yet.** Per the blueprint's own explicit
  staging (§5 P0.2: "quote_executable_meta_label ... until a defensible
  net-cost model exists"), net-cost modeling (commission, swap/financing,
  slippage beyond what the quote-side Bid/Ask spread already embeds) is
  deferred as a P1 upgrade path, not a P0 blocker. `quote_executable_r`
  already embeds the spread cost (entry at Ask for longs / Bid for shorts,
  exit at Bid for longs / Ask for shorts) — this is genuinely more realistic
  than a gross mid-price label, even without a full net-cost model layered
  on top.
- **Training population filter**: `mldp_target_eligible == True` only. This
  is Task 2A's own flag, and Task 2B's disposition classifier exists to
  confirm *why* the excluded rows are correctly excluded (matching what live
  execution would also decline to trade), not to add a second, separate
  filter — the actual row-selection logic for training is unchanged from
  what Task 2A already computes.

## A trap Stage B's implementation must not fall into: sample-weight scope

AFML Ch.4's concurrency/average-uniqueness/return-attribution weighting
(preserved unchanged per the blueprint, §4.3) must be computed over the
**full original gross event timeline**, including events that will later be
excluded from training as `TRUE_NO_FILL` or `RIGHT_CENSORED_END_OF_HISTORY`.
Those excluded events still occupied a real time interval and still
contributed to how "crowded" (concurrent) neighboring, retained events were.
Computing concurrency only over the post-filter (resolved-only) population
would **understate** overlap for the retained events — the correct sequence
is:

1. Compute concurrency/uniqueness/return-attribution weights over every
   gross event (as today).
2. Resolve execution and apply Task 2B's population filter.
3. Carry over each *retained* event's already-computed weight; do not
   recompute weights on the filtered-down set.

This ordering is not optional — reversing it would silently corrupt the
AFML sample weights in a way that specifically favors the (already resolved)
training set having appeared less crowded than it actually was against the
full opportunity set.

## What this invalidates

Per the blueprint's dependency table (§7, "Target changes gross ->
executable"): feature importance and modeling onward are invalidated for any
edge this migration touches. **Old selected features must not be kept
automatically** — Step 01's feature surgery was run against the wrong
target historically and must be re-run (this is exactly why Stage B's plan
already couples re-running Step 01 through the new denoise/ONC pipeline
(`docs/mldp/A4_DENOISE_ONC_UTILITY.md`) with the target migration in the
same pass, rather than treating them as unrelated changes).

Concretely invalidated once an edge is migrated: selected features, the
trained model, OOF probabilities, the sigmoid calibrator, thresholds, the
edge manifest, and anything downstream that consumed any of those. This is
the same list the blueprint's invalidation table already specifies for a
target change — this decision does not add new invalidations beyond what
was already documented, it makes concrete which edges the table now applies
to (any edge not yet migrated under this architecture).

## Reaffirming the promotion freeze (P0.6)

No edge may be promoted to, or remain at, live authority while trained under
the old gross-target/executed-calibration architecture. This was already
Quantara's own stated policy (`QUANTARA_MLDP_DOCTRINE.md`'s prior "Current
Task-1 boundary" section) and this decision changes nothing about that
freeze — it only specifies exactly what the corrected architecture is, so
that migrating an edge out from under the freeze is well-defined. Any
edge manifest currently reporting `deployment_status=PASS` under the old
architecture (e.g. the previously-noted `XAUUSD/trend_long` pilot) is
prior-generation evidence only, not current certification, until it is
rebuilt under this decision and passes the full Phase 0-8 gate chain again.

## Interaction with A5 (bet sizing)

Once the model's probability unambiguously means "P(quote-executable
success)", AFML Ch.10's canonical sizing formula (`shared/sizing/
afml_ch10_reference.py`, hardened in A5) becomes literally applicable to
what it was always meant to size — under the old architecture, feeding a
gross-trained-then-executable-calibrated probability into a formula that
assumes one fixed target meaning was itself part of the semantic problem
this decision closes. No code change follows from this observation alone;
it is noted here because it is the reason A5's hardening work and A2's
architecture decision are the same underlying fix, viewed from two different
stations of the pipeline.

## Doctrine and registry updates made alongside this decision

- `docs/mldp/QUANTARA_MLDP_DOCTRINE.md`'s "Current Task-1 boundary" section
  updated to reflect that the target architecture is now decided (this
  document), pending Stage B's actual pilot rebuild (Task 2C).
- `docs/mldp/TRANSFORMATION_STATE.json`: `phase` and `next_task` updated.
- `config/mldp_deviation_register.json`: `D001` updated from `OPEN_TASK2` to
  reflect this decision; `D002` status note updated to point at this
  document as the pipeline-migration specification.
