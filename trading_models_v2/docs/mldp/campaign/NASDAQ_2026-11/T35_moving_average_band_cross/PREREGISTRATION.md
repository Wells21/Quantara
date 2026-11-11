# T35 -- moving-average band cross (all 10 assets, long + short; aimed at shorts)

Written 2026-11-11, before any return of this card was computed. Gate 0 test 48. Rules: D090, D092 (LONG >= 2/week on
2022+), D094 (SHORT >= 1/week on 2022+), D093 (EWM 150), D050-A.

## Mechanism
Brock, Lakonishok and LeBaron (1992 JF 47(5)) "variable-length moving average" rule: a close through the moving average
signals a trend change, and requiring the close to clear a band around the average removes most whipsaws. Trend
followers (CTAs, systematic MA-rule traders) act on confirmed crosses, and the flow plus slow information diffusion
continue the move. (T30 tested the same paper's trading-range-breakout rule; this is the paper's other rule.)

Who loses: holders who wait for a deeper move before cutting (disposition effect) and liquidity providers leaning
against the new trend.
Should work: the start of sustained trends; trending FX and commodities.
Should fail: range-bound regimes where the price oscillates around the average.

## Event (causal)
Hourly closes hc (last close in each hour, right-labelled). m = mean of the hourly closes of the 20 days BEFORE that
hour (closed='left'); 20-day warm-up. Band b = 0.5 %.
- SHORT edge: hc < m (1 - b) and the previous hourly close >= its m (1 - b) (first close below the lower band).
- LONG edge: hc > m (1 + b) and the previous hourly close <= its m (1 + b).
Entry = the first bar closing at or after that hour end (<= 30 min); the signal close is <= 30 min stale.

## Counterfactual (drift-neutral, D090)
- C1 SHORT: an UNCONFIRMED cross -- hc < m, previous hc >= previous m, and hc >= m (1 - b) (crossed the average but
  stayed inside the band). Same asset, side, period and trend-change neighbourhood; only the confirmation is removed.
- C1 LONG mirrors (hc > m, previous hc <= previous m, hc <= m (1 + b)).
The theory says confirmed crosses continue and whipsaw crosses do not: the edge must beat C1.

## Gate 0 cell and rule
Cell 4 h Event_Vol, EWM 150, 24 h vertical, 1:1. PASS per asset and side (D090): C1 own diff - 1.28 SE > 0 (full
history), C1 diff > 0 on 2022+, >= 100 events in edge and C1, firing rate on 2022+ LONG >= 2/week, SHORT >= 1/week.
Universe: all 10 assets.

## Expected firing rate (event COUNTS only, 2022+, computed before any return)
SHORT / LONG edge events per week: NASDAQ 1.21 / 1.36; US500 1.03 / 1.32; DAX 1.28 / 1.52; JPN 1.28 / 1.61;
HK 1.22 / 1.17; XAUUSD 1.25 / 1.39; GBPJPY 1.17 / 1.82; USDMXN 1.64 / 1.18; USOIL 1.54 / 1.40; BTCUSD 1.36 / 1.52.
C1 0.85-1.75/week. So every SHORT can meet the firing rule; no LONG can.
Design history (counts only, no returns): BLL's 50-day average with a 1 % band gives 0.59-1.23 shorts/week; 20-day
with 1 % gives 0.78-1.62; 20-day with 0.5 % is the first setting with every asset's SHORT >= 1/week. BLL used 50 / 150
/ 200-day averages; the 20-day average and the 0.5 % band are chosen here for the firing rate only.

## Overlap with earlier cards
T30 (10-day closing high / low breakout) and T34 (pullback against the 50-day mean) are related trend rules. T35's state
(a first close through a band around the 20-day mean, against an unconfirmed cross) is new. Trials paid.

## Trial accounting
Test 48. Gate A-C on the D093 120-cell grid; all cells paid.
