# T29 results -- 12-month time-series momentum (Gate 0 test 42, D090), run 2026-11-10

Card: PREREGISTRATION.md (written before the run). Cell H4_E100_V24_PT1_SL1. Files: gate0_T29*.json, verdict.log,
quarter_clustered_t.json / .log (card diagnostic, read first).

| Asset | D090 LONG (z, 2022+/wk) | D090 SHORT | Quarter-clustered t (L / S) | 2022+ quarter diff |
|---|---|---|---|---|
| NASDAQ | FAIL (0.48) | FAIL (0.36) | -1.35 / -1.35 | +0.003 |
| US500 | FAIL (0.01) | FAIL (-0.04) | -1.73 / -1.76 | -0.044 |
| DAX | FAIL (-0.99) | FAIL (-0.96) | -0.12 / -0.10 | +0.165 |
| JPN | FAIL (1.00) | FAIL (0.88) | -0.29 / -0.33 | +0.030 |
| HK | **PASS** (1.56, 4.6) | **PASS** (1.59, 6.4) | 1.25 / 1.26 | +0.087 |
| XAUUSD | **PASS** (2.00, 16.7) | **PASS** (1.89, 3.2) | **-1.16 / -1.18** | -0.16 |
| GBPJPY | FAIL (0.49) | FAIL (0.59) | -1.70 / -1.67 | -0.185 |
| USDMXN | FAIL (0.27) | FAIL (0.38) | -0.30 / -0.23 | -0.028 |
| USOIL | FAIL (-0.02) | FAIL (-0.00) | -1.21 / -1.18 | -0.092 |
| BTCUSD | FAIL (0.43) | FAIL (0.44) | 0.10 / 0.11 | +0.030 |

Reading:
- HK: D090 PASS; the quarter-clustered t (1.25) is just below 1.28 but has the same sign and holds on 2022+.
- XAUUSD: D090 PASS event-weighted, but the quarter-clustered difference has the OPPOSITE sign (-1.16) and is negative
  on 2022+. The event-weighted pass rests on a few quarters with many down-state events. **Not credible**; sent to
  Gate A only because the card made the diagnostic a reading, not a gate. Gates C / PBO / DSR decide.
- Quarter-clustered t is negative on most assets: at a 24 h hold, 12-month trend states do not help here.

Next: HK/trendyear_long + _short, XAUUSD/trendyear_long + _short (triggers t29, parity IDENTICAL) -> Gate A
(logs/t29_gateA_20261110.sh), after the T28 Gate C lanes.
