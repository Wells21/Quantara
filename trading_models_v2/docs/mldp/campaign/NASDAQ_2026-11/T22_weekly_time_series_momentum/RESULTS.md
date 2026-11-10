# T22 results: weekly time-series momentum (Gate 0 test 35, run 2026-11-09) -- FAIL

Card: PREREGISTRATION.md (written before any run). Cell H8_E100_V24. Rule: Rev 47 + recency. Pooled over all 10 assets.

| side | n | pooled drift, month-matched (se, z) | C1 with vs against trend (se, z) | C2 vs no trend | pooled |
|---|---|---|---|---|---|
| LONG | 19,474 | -0.038 (0.011, -3.29) | +0.034 (0.015, 2.26) PASS | +0.017 | FAIL (drift) |
| SHORT | 16,171 | -0.041 (0.013, -3.10) | +0.033 (0.015, 2.24) PASS | +0.016 | FAIL (drift) |

Every asset fails on both sides. Firing on 2022+: about 3-5 events a week per side (e.g. NASDAQ LONG 730 in 3.85 y).

Reading:
- With-trend entries beat against-trend entries (C1 z 2.2 on both sides), and beat random entries over the whole
  history on most assets (diagnostic hour-matched drift > 0 on 8/10 LONG, 7/10 SHORT).
- But against random entries IN THE SAME MONTH, they lose (z about -3). The advantage is the month's own trend, which
  is exactly what the D075 month-matched baseline removes (disclosed on the card). Within a month, the 1-week signal
  points the wrong way: weekly moves partly revert inside the month.
- So the weekly signal adds no timing of its own; the trend premium in this data lives at the monthly scale, which
  Gate 0 as designed cannot credit. Not adopted. USOIL is negative everywhere (its mean R is below 0 on both sides).
