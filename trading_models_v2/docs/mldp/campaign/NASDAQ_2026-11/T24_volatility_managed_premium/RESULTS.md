# T24 results (Gate 0 test 37, D090) -- 2026-11-09

| index | side | n | C1 diff (z) | 2022+ C1 | events/week 2022+ | verdict |
|---|---|---|---|---|---|---|
| NASDAQ | LONG (low vol) | 5,687 | +0.033 (1.39) | +0.034 | 10.8 | **PASS** |
| NASDAQ | SHORT (high vol) | 5,711 | +0.033 (1.40) | +0.034 | 10.8 | **PASS** |
| US500 | L / S | 4,945 / 4,605 | +0.003 (0.10) / +0.002 (0.07) | +0.027 / +0.024 | 8.5 / 8.9 | FAIL |
| DAX | L / S | 5,499 / 5,393 | +0.004 (0.19) / +0.005 (0.20) | +0.005 / +0.006 | 8.7 / 9.9 | FAIL |
| JPN | L / S | 5,321 / 6,245 | -0.044 (-1.88) / -0.045 (-1.92) | -0.036 | 9.1 / 12.9 | FAIL (opposite sign) |
| HK | L / S | 3,162 / 3,335 | -0.047 (-1.56) / -0.047 (-1.58) | -0.090 | 5.1 / 6.0 | FAIL (opposite sign) |

Reading, honestly: NASDAQ passes at a marginal z (1.39; the week-clustered SE is understated for persistent vol
states, see the card) and its long and short are ONE contrast. 4 of 5 indices fail, JPN and HK with the opposite sign.
Under D090 other assets do not veto, so NASDAQ long + short go to Gate A; the 405-cell search is paid in PBO / DSR.
Placebos: no |z| > 1.1.

Next: NASDAQ/volmanagedpremium_long and _short -> Gate A (logs/t24_gateA_20261109.sh). Triggers:
shared/edges/thesis_triggers.t24, Gate 0 parity IDENTICAL (5,687 / 5,711).

## Gate A (2026-11-10)

| edge | rows | best gross | cells net > 0 | verdict |
|---|---|---|---|---|
| NASDAQ/volmanagedpremium_long | 5,574 | +0.140 | 404 / 405 | -> Gate C |
| NASDAQ/volmanagedpremium_short | 5,609 | +0.001 | 0 | **KILLED Gate A** (archived) |
