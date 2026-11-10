# T26 results (Gate 0 test 39, D090) -- 2026-11-10

**FAIL on all 10 assets, both sides** (gate0_T26_verdict_d090.json).

| asset | C1 z (L / S) | 2022+ C1 | note |
|---|---|---|---|
| USDMXN | +1.17 / +1.17 | +0.068 | closest, below 1.28 |
| HK | +0.10 / +0.04 | -0.008 | |
| DAX, JPN, GBPJPY, USOIL | -0.35 .. -0.62 | ~0 | |
| US500, NASDAQ | -1.0 / -1.5 | negative | opposite sign |
| XAUUSD | -2.45 / -2.42 | +0.058 | opposite sign |
| BTCUSD | -3.10 / -3.17 | -0.087 | opposite sign |

Firing 5-14 events/week everywhere. The lottery-demand reversal does not hold on single-asset time series at a 24 h
horizon. BTCUSD and XAUUSD show the OPPOSITE (positive skew is followed by higher R: jump continuation). That is a
seen, post-hoc observation; it is NOT adopted as a thesis (MLAM s.1.4.2 p.8: no backtest-tweak-backtest).

T26 CLOSED. Trial counted (test 39).
