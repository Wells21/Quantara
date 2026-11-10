# R1 results (2026-11-07): 10 Gate 0 re-runs on the extended history, 0 passes

Rule as pre-registered: the Rev 47 Gate 0 rule on the full history, plus recency (the asset's own month-matched drift
diff > 0 on 2022-01-01 .. end). Files: gate0_<T>.json (full), gate0_<T>_2014_2021.json, gate0_<T>_2022_on.json.

| card | side | pooled drift (full) | pooled C1 (full) | pooled drift 2014-21 / 2022+ | verdict |
|---|---|---|---|---|---|
| T1c | L / S | -0.000 / -0.006 | +0.002 / +0.004 | +0.002 -0.001 / -0.012 -0.004 | FAIL / FAIL |
| T1bc | L / S | +0.015 / -0.003 | +0.028 / +0.029 | +0.015 +0.019 / -0.015 +0.018 | FAIL / FAIL |
| T2 | L / S | **+0.152** (se 0.049) / +0.004 | -0.006 / -0.008 | +0.150 +0.118 / -0.021 +0.037 | FAIL (C1 about 0) / FAIL |
| T3 | L / S | +0.004 / +0.009 | +0.004 / +0.004 | +0.011 -0.004 / +0.008 +0.007 | FAIL / FAIL |
| T4 | L / S | +0.012 / -0.023 | +0.014 / -0.021 | +0.018 -0.000 / -0.019 -0.012 | FAIL / FAIL |
| T5 | L / S | **+0.045** (se 0.023) / +0.016 | **+0.079** (se 0.026) / +0.078 | **+0.087 -0.010** / +0.041 -0.021 | **FAIL on recency** / FAIL |
| T6 | L / S | -0.020 / -0.001 | -0.019 / -0.019 | -0.020 -0.020 / -0.010 +0.004 | FAIL / FAIL |
| T7 | L / S | +0.003 / -0.046 | -0.015 / -0.038 | +0.001 +0.003 / -0.026 -0.068 | FAIL / FAIL |
| T10 | L / S | +0.053 / -0.066 | -0.029 / -0.028 | +0.071 +0.020 / -0.111 -0.000 | FAIL / FAIL |
| T11 | L / S | +0.016 / -0.019 | -0.002 / -0.004 | +0.005 +0.007 / -0.023 +0.012 | FAIL / FAIL |

## Read

- **T5 LONG** (vol-target rebalancing flow into the close) is the only card that clears the Rev 47 rule on the full
  history, on both NASDAQ and US500:
  - NASDAQ drift +0.055 R (se 0.027), C1 +0.069, C2 +0.097.
  - The effect sits entirely in 2014-2021: NASDAQ drift +0.102 (se 0.035), C1 +0.132.
  - It is gone in 2022+: NASDAQ drift -0.004 (se 0.044), C1 -0.045.
  - It fails the pre-registered recency condition. Gate C economics run on 2022+ only, where it is about 0 R, so it
    could not pass there either. It is recorded as a DECAYED effect, not a lead.
- **T2 LONG**: a large month-end long drift (+0.15 R full; +0.15 in 2014-21, +0.12 in 2022+). But C1 (the
  opposite month-to-date sign) earns the same, so it is not the card's rebalancing mechanism. It is the generic
  turn-of-month drift, already tested as T4 with a settlement-locked window (T4 LONG +0.012, FAIL). This is a
  contaminated lead (found in the data, not pre-registered), not adopted per the T8 precedent.
- The extra years did not rescue any other card. Most effects stay near 0 with SEs about half the earlier ones.

Gate 0 tests in the NASDAQ campaign: 24 (14 earlier + 10 here), 0 passes.
