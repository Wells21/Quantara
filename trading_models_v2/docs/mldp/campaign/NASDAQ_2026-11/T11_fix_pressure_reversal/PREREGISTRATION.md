# T11 -- Benchmark-fix price pressure reversal (gold, GBPJPY, USDMXN): thesis card + Gate 0 pre-registration

Written 2026-11-05, before any data on these fix events was looked at. Our own thesis. Rules: D075 (rule C universe,
tiered drift bar), D083 (H1_V1). Causal entries.

**Disclosure.** The design is informed by T7-T9, where flows at clock events reversed instead of continuing. Those
results are NOT used here: the fix windows below were never examined in any earlier test (T1 used the same
reference times but measured the whole day's move into them, not the 30-minute pre-fix pressure, and traded
continuation). Owner 2026-11-05: test across the 10 assets; this card covers three non-equity assets.

## Card

- **Mechanism (ours).** Benchmark fixes concentrate non-informational orders in a short window: index rebalancers,
  hedgers and passive managers must trade at the fixing price, and banks pre-hedge them.
  - The WM/Reuters 4 pm London fix for FX.
  - The LBMA gold PM auction at 15:00 London.
  The pre-hedging and the fix flow push the price in the order-imbalance direction in the 30 minutes before the fix.
  Once the fix has passed the pressure stops, and liquidity providers who absorbed it unwind, so the pre-fix move
  reverts.
- **Who loses.** The fix-bound flow (it trades at a temporarily displaced price) and momentum traders who chase the
  pre-fix move.
- **Trigger.**
  - Each weekday, p = log(the last bar at or before the fix / the last bar at or before fix - 30 min), each at most
    30 minutes stale.
  - Entry = the first bar closing at or after the fix (at most 30 minutes after; causal).
  - p < 0: LONG. p > 0: SHORT.
  - Fix times (gate0_tool REF): XAUUSD 15:00 Europe/London; GBPJPY and USDMXN 16:00 Europe/London.
- **Universe (rule C):** XAUUSD, GBPJPY, USDMXN. The other 7 assets are computed at their REF reference times and
  reported only.
- **Cell (rule A):** H1_V1.
- **Counterfactuals (gating).**
  - **C1 sign:** the same side when the pre-fix move has the opposite sign. Prediction: edge > inverted.
  - **C2 placebo clock:** the same rule 3 hours before the fix (no fix flow). Prediction: edge > placebo.
- **Falsifiers.**
  - F1: the pre-fix sign does not matter.
  - F2: an ordinary clock time reverts as much.

## Gate 0 (pre-registered)

gate0_tool.py T11: the T1 labeler, the month-matched drift baseline, joint week-clustered SE, one-sided 95 %, pooled
over the universe, the tiered drift bar, and a verdict per side for every universe asset.

## RESULT (2026-11-05, gate0_T11.json) -- FAIL, both sides (closest pooled result so far)

Pooled over XAUUSD, GBPJPY and USDMXN, one-sided 95 % (lower bound in brackets):

| side | events | drift diff | C1 sign | C2 placebo clock |
|---|---|---|---|---|
| LONG | 1,658 | +0.013 (-0.030) | +0.031 (-0.015) | -0.006 (-0.051) |
| SHORT | 1,693 | +0.018 (-0.025) | +0.028 (-0.018) | -0.038 (-0.089) |

- XAUUSD LONG (buy after pre-fix selling, at the 15:00 London PM fix): 513 events, mean +0.058 R, drift +0.058,
  C1 +0.087, C2 +0.090. Every own diff is positive, but the pooled universe fails.
- USDMXN goes the other way (LONG -0.064 R).
- The C2 placebo fails: a reversal 3 hours before the fix is about as large. The pre-fix pressure reversal is not
  specific to the fix.
- Per-asset verdicts: FAIL for all three. Equity indices (reported only): NASDAQ LONG +0.064 R.

T11 is killed as written for both sides. Re-testing XAUUSD alone would be post-hoc (the universe chosen after the
result) and needs the owner's decision.
