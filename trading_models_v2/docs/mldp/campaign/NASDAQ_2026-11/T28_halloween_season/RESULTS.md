# T28 results -- Halloween season (Gate 0 test 41, D090), run 2026-11-10

Card: PREREGISTRATION.md (written before the run). Cell H4_E100_V24_PT1_SL1. Files: gate0_T28*.json, verdict.log,
season_clustered_t.json (card diagnostic, read first).

| Index | D090 LONG (week-cl. z, 2022+/wk) | D090 SHORT | Season-clustered t (L / S, ~13 seasons each) | 2022+ season diff |
|---|---|---|---|---|
| NASDAQ | FAIL (0.25) | FAIL (0.18) | 0.42 / 0.34 | -0.012 |
| US500 | FAIL (0.27) | FAIL (0.26) | 0.39 / 0.39 | -0.029 |
| DAX | **PASS** (1.52, 9.7) | **PASS** (1.53, 8.9) | **1.80 / 1.82** | +0.073 |
| JPN | FAIL (-0.17) | FAIL (-0.16) | -0.09 / -0.08 | -0.004 |
| HK | **PASS** (2.17, 5.6) | **PASS** (2.15, 5.4) | **2.48 / 2.44** | +0.064 |

Placebos (no verdict): XAUUSD z 2.70 / 2.68, GBPJPY 1.58 / 1.49, USDMXN -1.25 / -1.36, USOIL 0.45 / 0.42,
BTCUSD 0.30 / 0.31.

Reading:
- DAX and HK pass on both sides, and the season-clustered t (the card's honest unit) agrees (1.8 and 2.5). The two
  sides are one contrast, so each index is one piece of evidence, not two.
- RED FLAG: the strongest season contrast is the placebo XAUUSD (z 2.70), where the card's mechanism (equity risk
  appetite) does not apply. A sample-specific winter/summer pattern common to several markets (dollar cycle, 2014-2026
  macro timing) would produce the same index result. Gates C / PBO / DSR decide; this is noted before them.
- NASDAQ / US500 (the US indices, where the paper's evidence is weakest in recent decades) show nothing.

Next: DAX/halloweenseason_long + _short, HK/halloweenseason_long + _short (triggers t28, parity IDENTICAL on all 4)
-> Gate A (logs/t28_gateA_20261110.sh).
