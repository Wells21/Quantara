# T14 results: high-vol liquidity-provision reversal (Gate 0 test 27, run 2026-11-09) -- FAIL

Card: PREREGISTRATION.md (written before any run). Cell H1_V1. Rule: Rev 47 + recency.

| side | n | pooled drift (se, z) | C1 high vs low vol (se, z) | C2 | pooled |
|---|---|---|---|---|---|
| LONG (fade drops) | 9,161 | +0.007 (0.013, 0.55) | +0.014 (0.014, 0.98) | -0.002 | FAIL |
| SHORT (fade rises) | 7,869 | +0.013 (0.014, 0.92) | +0.036 (0.013, 2.71) | +0.007 | FAIL (drift) |

Every asset fails on both sides. NASDAQ: LONG drift -0.028 (se 0.036), SHORT +0.025 (se 0.038).

Reading:
- Fading a sharp 1 h move earns about what a random entry in the same month earns, in high-vol and low-vol regimes
  alike.
- SHORT C1 passes (z 2.7): fading a sharp rise does better in high vol than in low vol. But the high-vol fade itself
  does not beat random entries (drift z 0.9), so the contrast comes mostly from low-vol rises CONTINUING (C1 means
  are negative), not from a liquidity premium. That is a different thesis, seen in this data: not adopted.
- Nagel's cross-sectional premium does not show up at the index / single-market 1 h scale in these markets.
