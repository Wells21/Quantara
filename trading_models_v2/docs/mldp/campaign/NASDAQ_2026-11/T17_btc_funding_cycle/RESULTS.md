# T17 results: Bitcoin perpetual-funding cycle (Gate 0 test 30, run 2026-11-09) -- FAIL

Card: PREREGISTRATION.md (written before any run). Cell H1_V1. BTCUSD only. Dukascopy BTCUSD has no weekend bars, so
there are 15 timestamps a week, about 6 events a week per side on 2022+.

| window | side | n | drift (se, z) | C1 mid-point times (z) | C2 reversed side |
|---|---|---|---|---|---|
| full | LONG | 2,692 | -0.020 (0.019, -1.05) | -0.021 (-1.07) | +0.009 |
| full | SHORT | 2,530 | +0.025 (0.020, 1.21) | +0.046 (2.14) | +0.008 |
| 2017-21 | SHORT | 1,236 | +0.038 (1.30) | +0.059 (1.94) | +0.029 |
| 2022+ | SHORT | 1,294 | +0.003 (0.10) | +0.034 (1.12) | -0.012 |

Verdict: FAIL both sides.
- LONG has the wrong sign.
- SHORT misses the drift bar: z 1.21 < 1.28. C1 passes, C2 > 0. The effect sits in 2017-2021 and is about 0 in
  2022+, and the C2 diff turns negative there.

Reading: after a 24 h fall, shorting right after the funding timestamp beat the mid-point times. That is consistent
with shorts re-opening after settlement, but it faded after 2021 as funding markets matured. Not adopted. A
re-test with a different threshold would be post hoc.
