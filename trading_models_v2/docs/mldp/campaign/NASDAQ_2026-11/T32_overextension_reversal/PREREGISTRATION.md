# T32 -- short-horizon overextension reversal (all 10 assets, long + short; aimed at shorts)

Written 2026-11-10, before any return of this card was computed. Gate 0 test 45. Rules: D090, D092 (>= 2/week on
2022+), D093 (EWM 150), D050-A.

## Mechanism
Short-horizon price moves partly overshoot because liquidity demanders push price past fair value and liquidity
providers are paid to absorb the imbalance; the overshoot then reverses (Jegadeesh 1990 JF; Lehmann 1990 QJE;
Nagel 2012 RFS: short-term reversal returns are the compensation for liquidity provision). The further price is
stretched from its recent mean, the larger the expected reversal.

Who loses: late momentum / panic traders who buy (sell) after the stretch, paying the liquidity provider.
Should work: range-bound and mean-reverting regimes.
Should fail: genuine trend breaks driven by news (the stretch keeps going).

## Event (causal)
Hourly closes hc (last close in each hour, right-labelled). z = (hc - mean) / std, where mean and std use the hourly
closes of the 5 days BEFORE that hour (closed='left'); 5-day warm-up.
- SHORT edge: the hour end where z first crosses up through +1.5 (z >= 1.5, previous hour < 1.5).
- LONG edge: the hour end where z first crosses down through -1.5.
Entry = the first bar closing at or after that hour end (<= 30 min); the signal close is <= 30 min stale.

## Counterfactual (drift-neutral, D090)
- C1 SHORT: the hour end where z first crosses up through +1.0 -- the same direction of stretch from the same mean,
  same asset, side and period, comparable frequency; only the extreme part of the stretch is removed.
- C1 LONG: first cross down through -1.0.
The theory says the reversal is larger after the deeper stretch: the edge must beat C1.

## Gate 0 cell and rule
Cell 4 h Event_Vol, EWM 150, 24 h vertical, 1:1. PASS per asset and side (D090): C1 own diff - 1.28 SE > 0 (full
history), C1 diff > 0 on 2022+, >= 100 events in edge and C1, >= 2 events/week on 2022+. Universe: all 10 assets.

## Expected firing rate (event COUNTS only, 2022+, computed before any return)
SHORT / LONG edge events per week: NASDAQ 2.44 / 2.32; US500 2.03 / 2.25; DAX 2.28 / 2.01; JPN 2.24 / 2.27;
HK 1.66 / 1.80; XAUUSD 2.61 / 1.96; GBPJPY 2.73 / 2.15; USDMXN 2.07 / 2.84; USOIL 2.37 / 2.23; BTCUSD 2.47 / 2.36.
C1 runs at 1.9-3.1/week. Design history (counts only): a 20-day window at +/-2.0 gave 0.6-1.3/week; 5-day at +/-2.0
1.5-2.2; 5-day at +/-1.75 1.5-2.3; 5-day at +/-1.5 is the first that reaches 2/week on most assets and sides.

## Overlap with earlier cards
T14 (high-vol liquidity-provision reversal) conditioned on a return z-score inside a HIGH-VOLATILITY regime only; T32
uses the price's distance from its own 5-day mean in every regime. T30 trades with a new 10-day extreme, T31 against a
failed break. T32's state (a first crossing of a 1.5-sigma band of the 5-day mean) is new.

## Trial accounting
Test 45. Gate A-C on the D093 120-cell grid; all cells paid.
