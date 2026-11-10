# T15 results: same-hour intraday periodicity (Gate 0 test 28, run 2026-11-09) -- FAIL

Card: PREREGISTRATION.md (written before any run). Cell H1_V1. Firing rate on 2022+: about 10-15 edge events a week per
side per asset (the firing-rate rule is met). The result is not.

| side | n | pooled drift (se, z) | C1 previous-hour signal (z) | C2 weak signal | pooled |
|---|---|---|---|---|---|
| LONG | 79,553 | -0.011 (0.004, -2.75) | +0.000 (0.02) | -0.000 | FAIL |
| SHORT | 70,716 | -0.020 (0.004, -4.54) | -0.007 (-1.64) | -0.008 | FAIL |

Every asset fails on both sides.

Reading:
- **No periodicity.** C1 is about zero: an hour's own recent history predicts that hour no better than the previous
  hour's history does.
- **The drift is NEGATIVE and significant on both sides.** Part of it is an entry-clock effect. Hour-matched random
  entries close about half the gap: entering at the top of the clock hour does slightly worse than a random bar
  (e.g. :00 news / fix spikes with a lagging 1 h Event_Vol). The rest is a mild same-hour REVERSAL, the opposite
  of the thesis. It was seen in this data, so it is not adopted.
- Heston-Korajczyk-Sadka's cross-sectional half-hour persistence (stocks) does not carry to these single markets at
  the clock-hour scale.
- Diagnostic for future clock-entry cards: compare against the hour-matched baseline as well.
