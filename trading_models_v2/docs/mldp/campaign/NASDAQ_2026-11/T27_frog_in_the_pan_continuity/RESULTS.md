# T27 results -- frog in the pan (Gate 0 test 40, D090), run 2026-11-10

Card: PREREGISTRATION.md (written before the run). Cell H4_E100_V24_PT1_SL1. Files: gate0_T27*.json, verdict.log.

| Asset | LONG (C1 z, 2022+ /week) | SHORT (C1 z, 2022+ /week) |
|---|---|---|
| NASDAQ | FAIL (-0.59) | FAIL (-1.08) |
| US500 | **PASS** (2.86; C1 diff +0.086 R, 2022+ C1 +0.047, 4.5/week) | FAIL (-1.30) |
| DAX | FAIL (0.38) | FAIL (-0.72) |
| JPN | FAIL (0.03) | FAIL (0.53) |
| HK | FAIL (0.36) | FAIL (-0.02) |
| XAUUSD | FAIL (-0.65) | **PASS** (2.45; C1 diff +0.057 R, 2022+ C1 +0.079, 4.0/week) |
| GBPJPY | FAIL (-1.02) | FAIL (0.74) |
| USDMXN | FAIL (1.18) | FAIL (-1.63) |
| USOIL | **PASS** (1.42; C1 diff +0.059 R, 2022+ C1 +0.032, 4.1/week) | FAIL (-1.33) |
| BTCUSD | FAIL (1.64 full, but 2022+ C1 -0.024: recency) | FAIL (-0.67) |

Reading:
- 3 passes in 20 asset-side contrasts at one-sided 90 %: about 2 are expected by chance alone. These are leads for
  Gates A-C / PBO / DSR, not findings.
- Month-matched drift (diagnostic): US500 LONG +0.006, XAUUSD SHORT -0.026, USOIL LONG -0.042 -- the passes beat their
  own C1 but are not better than random same-month entries on gold / oil. Gate C / PBO / DSR decide.
- The card's disclosure (T26 skew result pointing against LONG on BTCUSD / XAUUSD) did not bite: XAUUSD LONG failed
  (-0.65), BTCUSD LONG failed on recency.

Next: XAUUSD/continuousinfo_short, US500/continuousinfo_long, USOIL/continuousinfo_long scaffolded (triggers
shared/edges/thesis_triggers.py t27, event parity IDENTICAL on all 3) -> Gate A (logs/t27_gateA_20261110.sh).
