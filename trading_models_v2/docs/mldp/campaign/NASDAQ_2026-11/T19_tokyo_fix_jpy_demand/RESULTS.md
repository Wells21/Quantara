# T19 results: Tokyo fix yen demand on GBPJPY (Gate 0 test 32, run 2026-11-09) -- FAIL

Card: PREREGISTRATION.md (written before any run). Cell H1_V1. About 4.4-4.8 events a week per side on 2022+.

| window | side | n | drift (z) | hour-matched drift | C1 two hours later (z) | C2 non-gotobi |
|---|---|---|---|---|---|---|
| full | LONG into fix | 3,405 | -0.017 (-0.96) | -0.002 | -0.025 (-1.60) | -0.011 |
| full | SHORT after fix | 3,568 | +0.028 (1.68) | -0.002 | +0.020 (1.27) | +0.017 |
| 2022+ | SHORT | 1,132 | +0.021 (0.67) | +0.025 | +0.007 (0.24) | +0.026 |

Verdict: FAIL both sides.
- LONG has the wrong sign: GBPJPY does not rise into the 09:55 fix.
- SHORT misses C1 by a hair (z 1.27 < 1.28). Its drift advantage is entirely a time-of-day effect: against
  hour-matched random shorts it is -0.002, so it is not specific to the fix.

The Tokyo-fix USDJPY pressure, if present, does not come through GBPJPY at bar level.
