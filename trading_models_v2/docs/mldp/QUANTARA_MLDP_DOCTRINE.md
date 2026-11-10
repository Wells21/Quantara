# Quantara MLDP Doctrine — Foundation v1

## Authority

Quantara's methodological references are Marcos López de Prado's *Advances in
Financial Machine Learning* (2018) and *Machine Learning for Asset Managers*
(2020). The books are a research methodology, not one fixed trading algorithm.

## Non-negotiable research principles

1. **Theory precedes promotion.** A profitable historical path is not a theory.
   Every promoted edge must state the mechanism, agent/constraint that can fund
   the edge, regimes, counterfactuals, and falsifiers.
2. **One probability, one estimand.** Model training, OOF prediction,
   calibration, thresholding, and sizing may not silently switch outcomes.
3. **Keep economic layers separate.** Gross research, quote-executable, net
   simulated executable, and live realized R are different objects.
4. **Dynamic labels are causal.** Event target volatility and barrier geometry
   must be known and frozen at event t0.
5. **Non-IID observations are treated as non-IID.** Use interval-aware purging,
   justified embargo, concurrency/uniqueness and AFML return-attribution
   weighting where applicable.
6. **Backtests do not manufacture discoveries.** Every outcome-informed
   experiment is a trial. Geometry, model, threshold and allocator searches
   must be recorded and selection risk quantified.
7. **Canonical MLDP methods remain visible.** Quantara extensions are named as
   extensions and benchmarked against the canonical reference.
8. **Production realism is preserved.** Exact Bid/Ask replay, broker lots,
   currency conversion, latency controls, MT5 state and prop constraints are
   legitimate production extensions.
9. **Failure is a valid result.** The process must be capable of rejecting every
   candidate. It must never tune an edge until profitability appears.
10. **Artifact invalidation is dependency-aware.** Upstream semantic changes
    force regeneration from the earliest affected station.

## Geometry research rule

Event_Vol lookback, EWM span, PT/SL multipliers and vertical horizon may be
researched again after executable-target migration. They must not be selected
by simply maximizing executable R over a large grid. Candidate ranges must be
justified by the mechanism/holding horizon, every evaluated point must be
recorded as a trial, development evidence must include stability across time,
regimes and assets where applicable, and one specification must be frozen
before a locked final evaluation.

## Current transformation boundary

Task 1 installed the semantic/CV/sizing/trial-accounting foundation. Task 2A
installed an audit-only executable-target engine. Task 2B froze the
population rule for unresolved/no-fill events
(`docs/mldp/TASK2B_POPULATION_RULE_DECISION.md`). The target/calibration
architecture is now frozen as doctrine
(`docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md`): the secondary
model must train directly on `quote_executable_meta_label` (execution
resolved before training, not retargeted after), and Step 04 becomes
same-target calibration with no target switch. This decision does not by
itself change any edge's Step 01-09 code -- that migration (Task 2C) is
scoped to the pilot-edge rebuild. Until an edge is actually rebuilt under
this architecture and re-passes the full Phase 0-8 gate chain, its existing
artifacts (including any edge manifest reporting `deployment_status=PASS`
under the old gross-target/executed-calibration architecture) are
prior-generation evidence and are not newly MLDP-certified.
