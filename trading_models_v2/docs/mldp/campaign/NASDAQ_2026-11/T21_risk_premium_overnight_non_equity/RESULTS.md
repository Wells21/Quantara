# T21 results: overnight risk premium in non-equity risk assets (Gate 0 test 34, run 2026-11-09) -- FAIL

Card: PREREGISTRATION.md (written before any run). Cell H4_V16. About 4-5 events a week per side on 2022+.

| pooled side | window | n | drift (z) | C1 (z) |
|---|---|---|---|---|
| LONG | full | 8,545 | +0.011 (0.78) | +0.007 (0.47) |
| LONG | 2022+ | 3,164 | -0.006 (-0.24) | -0.026 (-1.11) |
| SHORT | full | 10,297 | -0.002 (-0.18) | +0.007 (0.52) |
| SHORT | 2022+ | 4,339 | -0.014 (-0.66) | -0.025 (-1.09) |

Every asset fails on both sides. The closest is USDMXN LONG in the US session (drift +0.034, se 0.022), but the
pooled tests fail. The equity overnight premium (T18) does not extend to GBPJPY, USOIL, BTCUSD or USDMXN. The
cross-asset coverage of T18 is complete for these four (gold: T20).
