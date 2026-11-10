# T30 -- 10-day trading-range breakout continuation (all 10 assets, long + short)

Written 2026-11-10, before any return of this card was computed. Gate 0 test 43. Rules: D090 (Gate 0 per asset),
D092 (firing rate >= 2/week on 2022+), D093 (grid EWM 150), D050-A (every trial paid).

## Mechanism
Trend-following funds (CTAs) enter on trading-range / channel breakouts (Donchian channels; the "trading range
break-out" rule of Brock, Lakonishok and LeBaron 1992, JF 47(5): buy signals after a break of the recent range were
followed by higher returns). When price closes beyond its recent range, systematic trend-followers and stop orders
beyond the range buy (sell), so the break continues for a while.

Who loses: range traders and mean-reversion liquidity providers who sell the top (buy the bottom) of the recent range
and are run over by the systematic flow.
Should work: trending regimes, assets with large CTA participation (index futures, gold, FX, oil).
Should fail: choppy range-bound regimes (false breaks), breaks caused by one-off news gaps.

## Event (causal)
Every hour on the hour (UTC), t:
- c = the last close at or before t (at most 30 min stale); entry = the first bar closing at or after t (<= 30 min).
- H / L = the max / min hourly close over the 10 days before the last full hour ending at or before t (that hour
  excluded). 10-day warm-up.
- LONG edge: c > H (new 10-day closing high). SHORT edge: c < L (new 10-day closing low).
- Consecutive hours of the same run are separate events (overlap handled by AFML uniqueness weights later).

## Counterfactual (drift-neutral, D090)
- C1 LONG: same checks where c <= H and c > (H + L) / 2 (upper half of the range, no break): same trend
  direction, same asset, same period, comparable frequency -- only the breakout is removed.
- C1 SHORT: c >= L and c < (H + L) / 2.
The theory says the break itself carries the continuation; C1 must do worse.

## Gate 0 cell and rule
Cell 4 h Event_Vol, EWM 150, 24 h vertical, 1:1 (D093). PASS per asset and side (D090): C1 own diff - 1.28 SE > 0
(full history), C1 diff > 0 on 2022+, >= 100 events in edge and C1, >= 2 events/week on 2022+ (D092).
Universe: all 10 assets.

## Expected firing rate (event COUNTS only, 2022+, computed before any return)
Hourly checks, 10-day channel, events/week LONG / SHORT:
NASDAQ 4.91 / 3.28; US500 3.72 / 3.03; DAX 4.14 / 2.51; JPN 4.51 / 2.85; HK 2.87 / 2.68; XAUUSD 5.54 / 2.66;
GBPJPY 4.50 / 2.45; USDMXN 2.72 / 4.35; USOIL 3.55 / 3.08; BTCUSD 4.18 / 3.18.
Design history (counts only, no returns): a 20-day channel at 4-hourly checks fired 0.3-1.2/week, 5- and 10-day at
4-hourly checks 0.5-2.0/week, 20-day hourly 1.5-4.1/week; 10-day hourly is the first choice clearing 2/week on every
asset and side.

## Overlap with earlier cards
T13 (round level untouched 24 h) breaks a price GRID, not the asset's own range; T25 (52-week high) used nearness to
a 1-year high split at its median; T22 / T29 condition on the sign of a past return. T30's state is a new 10-day
closing extreme, which none of them used.

## Trial accounting
Test 43 of the campaign. Gate A-C on the D093 120-cell grid; all cells paid.
