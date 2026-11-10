# T20 results: gold Asia vs New York session (Gate 0 test 33, run 2026-11-09) -- FAIL

Card: PREREGISTRATION.md (written before any run). Cell H4_V4. About 4.8-4.9 events a week per side on 2022+.

| window | side | n | drift (z) | hour-matched drift | C1 other session (z) |
|---|---|---|---|---|---|
| full | LONG at SGE open | 5,190 | +0.039 (2.83) | -0.033 | +0.051 (3.41) |
| full | SHORT at COMEX open | 5,455 | +0.013 (0.82) | -0.009 | +0.041 (2.74) |
| 2014-21 | LONG | 1,978 | +0.033 (1.49) | +0.009 | +0.077 (3.22) |
| 2022+ | LONG | 1,067 | **-0.000** (0.00) | -0.003 | +0.055 (1.70) |
| 2022+ | SHORT | 1,094 | +0.029 (0.79) | -0.002 | +0.039 (1.19) |

Verdict: FAIL both sides.
- LONG passes the pooled tests on the full history but **fails recency**: its 2022+ drift is -0.000. The tool's
  verdict_by_asset_rev47 field says PASS because it does not apply the card's recency condition; the card governs.
  The hour-matched drift (-0.033) also shows the advantage is not specific to the SGE-open bar.
- SHORT fails the drift test (z 0.82).
- The Asia-vs-NY contrast (C1) is real in older data, as the market commentary describes, but gold's Asian-session
  drift has not beaten random longs since 2022.

2005-2013 diagnostic (re-run after a tool fix: the NASDAQ verdict block now skips windows with no NASDAQ events):
LONG drift +0.043 (z 2.02), C1 +0.026 (z 1.11); SHORT drift -0.013 (z -0.50). The Asian-session drift is a
2005-2021 phenomenon (z 2.02, then 1.49) that has disappeared since 2022 (-0.000).
