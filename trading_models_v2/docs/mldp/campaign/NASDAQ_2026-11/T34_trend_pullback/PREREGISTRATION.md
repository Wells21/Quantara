# T34 -- trend pullback (all 10 assets, long + short; aimed at shorts)

Written 2026-11-11, before any return of this card was computed. Gate 0 test 47. Rules: D090, D092 (LONG >= 2/week on
2022+), D094 (SHORT >= 1/week on 2022+), D093 (EWM 150), D050-A.

## Mechanism
Time-series momentum at the 1-3 month horizon (Moskowitz, Ooi and Pedersen 2012 JFE 104(2)): an asset below its
recent trend level tends to keep falling. Inside such a downtrend, short-horizon rallies are mostly liquidity-driven
(short covering, dip-buyers), not a change in the slow trend, so they are a better-priced point to join the trend. The
trend-follower sells the rally; the long side mirrors it (buy a 1-sigma dip while above the 50-day mean).

Who loses: counter-trend dip-buyers (in a downtrend) and short-covering flow that pays up into a temporary rally.
Should work: persistent trends (bear markets for shorts, bull markets for longs), trending FX and commodities.
Should fail: trend reversals near the 50-day mean; range-bound regimes.

## Event (causal)
Hourly closes hc (last close in each hour, right-labelled). z = (hc - mean5) / std5 over the hourly closes of the 5
days BEFORE that hour (closed='left'); m50 = mean of the hourly closes of the 50 days BEFORE that hour (closed='left');
50-day warm-up.
- SHORT edge: hc < m50 (downtrend) and z first crosses up through +1.0 (z >= 1.0, previous hour < 1.0).
- LONG edge: hc > m50 (uptrend) and z first crosses down through -1.0.
Entry = the first bar closing at or after that hour end (<= 30 min); the signal close is <= 30 min stale.

## Counterfactual (drift-neutral, D090)
- C1 SHORT: the same +1.0 cross while hc > m50 (the same rally and short, in an UPtrend) -- same asset, side, period
  and rally size; only the trend state is removed.
- C1 LONG: the same -1.0 cross while hc < m50.
The theory says the short after a rally pays only when the slow trend is down: the edge must beat C1.

## Gate 0 cell and rule
Cell 4 h Event_Vol, EWM 150, 24 h vertical, 1:1. PASS per asset and side (D090): C1 own diff - 1.28 SE > 0 (full
history), C1 diff > 0 on 2022+, >= 100 events in edge and C1, firing rate on 2022+ LONG >= 2/week, SHORT >= 1/week.
Universe: all 10 assets.

## Expected firing rate (event COUNTS only, 2022+, computed before any return)
SHORT / LONG edge events per week: NASDAQ 0.81 / 1.58; US500 0.85 / 1.72; DAX 0.89 / 1.52; JPN 0.84 / 1.65;
HK 1.01 / 0.97; XAUUSD 0.82 / 1.58; GBPJPY 0.90 / 1.58; USDMXN 1.51 / 0.84; USOIL 1.23 / 1.12; BTCUSD 1.41 / 1.45.
So only SHORT on HK, USDMXN, USOIL, BTCUSD can meet the firing rule; no LONG can. Full-history counts >= 345 per set.
Design history (counts only, no returns): first designed 2026-11-10, dropped before Gate 0 under D092 (shorts
0.81-1.51/week); re-opened under D094. Thresholds 0.75 / 0.5 sigma were counted and barely change the short rate (the
binding limit is time spent below the 50-day mean), so the original 1.0 sigma is kept.

## Overlap with earlier cards
T32 trades a 1.5-sigma stretch AGAINST the move in every regime; T34 trades a 1-sigma move against the slow trend only
and enters WITH the slow trend. T22 (weekly time-series momentum) conditions on the past week's return, not on a
pullback inside a 50-day trend. Earlier (pre-campaign, since disabled) NASDAQ trend-pullback-continuation edges used a
different trigger; this card is a new test (trials paid).

## Trial accounting
Test 47. Gate A-C on the D093 120-cell grid; all cells paid.
