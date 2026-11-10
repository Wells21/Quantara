# T25 results (Gate 0 test 38, D090) -- 2026-11-09

| index | side | n | C1 diff (z) | 2022+ C1 | events/week 2022+ | verdict |
|---|---|---|---|---|---|---|
| HK | LONG (near the 52-week high) | 2,841 | +0.068 (2.16) | +0.074 | 5.2 | **PASS** |
| HK | SHORT (far from it) | 3,408 | +0.068 (2.15) | +0.074 | 5.8 | **PASS** |
| NASDAQ | L / S | 5,104 / 5,510 | +0.017 (0.70) / +0.018 (0.72) | +0.035 | 10.0 / 11.7 | FAIL |
| US500 | L / S | 4,899 / 3,941 | -0.003 (-0.12) / -0.004 (-0.14) | +0.020 | 8.6 / 8.7 | FAIL |
| DAX | L / S | 4,642 / 5,433 | +0.019 (0.80) / +0.020 (0.83) | +0.009 | 8.5 / 10.1 | FAIL |
| JPN | L / S | 5,513 / 5,257 | -0.009 (-0.36) / -0.009 (-0.34) | +0.028 | 11.5 / 10.5 | FAIL |

HK's long and short are ONE contrast. Note: under T24, HK's volatility contrast had the OPPOSITE sign (z -1.56), so the
HK result is about the anchor, not volatility. Placebos: BTCUSD z 1.6, XAUUSD 1.2 (no verdict).

Next: HK/highanchor_long and _short -> Gate A (logs/t25_gateA_20261109.sh; HK broker mean 2.5175 bps). Trigger
shared/edges/thesis_triggers.t25, Gate 0 parity IDENTICAL (2,841 / 3,408). Family named "highanchor" (a folder name
starting with a digit is unsafe as a Python module path).

## Gate A (2026-11-09)

| edge | rows | best gross | cells net > 0 | verdict |
|---|---|---|---|---|
| HK/highanchor_long | 2,841 | +0.141 | 255 / 405 | -> Gate C |
| HK/highanchor_short | 3,408 | +0.085 | 98 / 405 | -> Gate C |
