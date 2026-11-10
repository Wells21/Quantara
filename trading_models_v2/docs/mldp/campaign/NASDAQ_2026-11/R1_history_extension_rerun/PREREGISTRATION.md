# R1: re-run of the Gate 0 cards on the extended history (pre-registered 2026-11-07, before any run)

## Why

Owner 2026-11-07: the history was extended (NASDAQ from 2014-02, the other assets from 2005-2017; USOIL from 2020-03)
and the bar parser bug was fixed (every earlier bar build dropped whole-second ticks). The owner asked whether the
theses should start afresh.

## Trials are NOT reset (book)

- AFML s.11.5 p.153, recommendation 4: "Record every backtest conducted on a dataset" so PBO and the deflated Sharpe
  use the number of trials carried out. Recommendation 6: after a failed backtest "start from scratch. Resist the
  temptation of reusing those results."
- MLAM s.8.1 p.105: presenting the best of several trials as a single trial inflates it (selection bias under
  multiple testing, SBuMT).
- The extended history CONTAINS the period on which the 14 earlier tests were judged (2022-2026), and we know their
  results. Forgetting them would understate K. Rule D050-A: trials accumulate, never reset.

So each re-run below is a NEW trial added to the campaign count. The 14 earlier tests stay counted. Gate 0 tests:
14 -> 24.

## What is re-run

The cards are unchanged: same event rule, Gate 0 cell, counterfactuals and universe (`shared/research/gate0/gate0_tool.py`
THESES).

- T1c, T1bc (the causal-entry versions of T1/T1b; T1 and T1b are not re-run because their entry bar was not causal)
- T2, T3, T4, T5, T6, T7, T10, T11

Not re-run:

- T8, T9: the economic calendar (config/economic_event_calendar.json) starts in 2022, so their event sets are
  unchanged.

## Verdict rule (fixed before the run)

Per universe asset and side, on the FULL history:

1. The Rev 47 Gate 0 rule (owner 2026-11-05, prospective):
   - the pooled month-matched drift baseline and the pooled C1 pass at one-sided 90 % (z 1.28);
   - every other pooled counterfactual diff > 0;
   - the asset's own drift and counterfactual diffs > 0.
2. **Recency**: the asset's own month-matched drift diff on the 2022-01-01 .. end slice is > 0. The economics (Gate C,
   Task 2A, Steps 04-05) run on the tick-covered recent period only (HISTORY_EXTENSION_DESIGN.md), so a thesis whose
   effect is gone there cannot pass.

Diagnostics, which do not change the verdict:

- the 2014 .. 2021 slice (years the original tests never saw = an out-of-sample replication);
- the 2022 .. end slice (the original period re-measured on the fixed bars).

A pass goes to Gate A on NASDAQ with this trial history attached (10 re-runs + 14 earlier Gate 0 tests).
