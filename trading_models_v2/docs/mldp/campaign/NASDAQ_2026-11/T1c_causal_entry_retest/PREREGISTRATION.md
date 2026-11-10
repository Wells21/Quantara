# T1c / T1bc -- Session-close hedging flow with the causal (live-tradable) entry: POST-HOC Gate 0 re-test

Written 2026-11-04, before running, after the NASDAQ/closehedgingflow_short Gate A diagnostic showed that the T1b pass
depended on the entry bar (../T1b_session_close_hedging_flow_US/PREREGISTRATION.md, Gate A section).

Owner 2026-11-04: "yes rerun T1's Gate 0 with the live tradable entry". The owner wrote "T1" while answering the T1b
question, so both are run:
- **T1c:** T1's universe (all 10 assets).
- **T1bc:** T1b's universe (NASDAQ + US500).

**The one change from the T1 / T1b cards.** The entry bar, both for the edge set and for the C2 mid-session set, is
the FIRST bar closing at or after the entry time, at most 30 minutes after it. T1 / T1b used the last bar at or before
the entry time, which needs the next bar's timestamp, so it cannot be traded live.

**Unchanged:**
- the reference closes (the last bar at or before, which is in the past);
- the cell H1_V1, the labeler, the seeds and the counterfactuals C1 / C2;
- the joint week-clustered SE and one-sided 95 % for the counterfactuals;
- the tiered drift bar: 90 % when every counterfactual passes at 95 %, else 95 %;
- the NASDAQ verdict rule (pooled tests pass and NASDAQ's own diffs > 0).

**Status.** Post-hoc, owner-ordered; each run is a logged trial (T1, T1b, T1c, T1bc). A FAIL means the
session-close thesis has no live-tradable Gate 0 support on NASDAQ, and NASDAQ/closehedgingflow_short stops. A PASS
does not lift the edge's pre-registered Gate C lock rule (1 h vertical cells only).

Tool: shared/research/gate0/gate0_tool.py T1c|T1bc OUT_DIR.

## RESULT (2026-11-04) -- FAIL, both sides, both universes

The owner then clarified "i meant T1b's": **T1bc is the answer**. T1c had already run and is logged as a trial.

T1bc, pooled NASDAQ + US500 (one-sided 95 %, lower bound in brackets; the tiered bar does not apply because the
counterfactuals fail):

| side | events | drift diff | C1 sign | C2 timing |
|---|---|---|---|---|
| LONG | 1,100 | +0.018 (-0.047) | +0.040 (-0.037) | +0.073 (-0.001) |
| SHORT | 927 | +0.009 (-0.058) | +0.035 (-0.041) | +0.058 (-0.018) |

- NASDAQ SHORT alone: 462 events, mean -0.001 R; drift +0.006, C1 +0.027, C2 +0.078.
- T1c (all 10 assets): every pooled test is near zero (|diff| <= 0.011).

Compared with T1b (entry one bar earlier), the counterfactual diffs drop from about +0.12 to about +0.04 and lose
significance, and the drift diff drops from +0.06 to about +0.01. The session-close hedging flow has no live-tradable
Gate 0 support on NASDAQ or US500. **NASDAQ/closehedgingflow_short stops** (Gate C halted; the edge is not registered).
Trials on this thesis: T1, T1b, T1c, T1bc.
