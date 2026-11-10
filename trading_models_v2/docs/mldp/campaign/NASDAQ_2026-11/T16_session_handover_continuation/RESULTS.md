# T16 results: session hand-over continuation (Gate 0 test 29, run 2026-11-09) -- FAIL

Card: PREREGISTRATION.md (written before any run). Cell H1_V1. Firing rate on 2022+: about 4 a week per side on most
assets (HK about 1.2: it only has the New York boundary).

| side | n | pooled drift (se, z) | C1 mid-session (z) | C2 stale | pooled |
|---|---|---|---|---|---|
| LONG | 23,469 | -0.006 (0.007, -0.81) | -0.010 (-1.31) | +0.004 | FAIL |
| SHORT | 22,708 | +0.001 (0.008, 0.17) | +0.000 (0.01) | +0.002 | FAIL |

Every asset fails on both sides. The first hour of a new session does not continue the previous session's move, and
the hand-over boundary is no different from a mid-session time with the same look-back.
