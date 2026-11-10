# T33 -- market intraday momentum (5 equity indices, long + short; aimed at shorts)

Written 2026-11-10, before any return of this card was computed. Gate 0 test 46. Rules: D090, D092 (>= 2/week on
2022+), D093 (EWM 150), D083 (H1_V1 cell for an intraday horizon), D050-A.

## Mechanism
Market intraday momentum (Gao, Han, Li and Zhou 2018, JFE 129(2)): the first half-hour return of the session, measured
from the previous close, predicts the last half-hour return with the same sign. Causes: informed traders and
infrequent rebalancers trade late in the day, and dealers / leveraged-ETF / short-gamma hedgers trade in the direction
of the day's move into the close (Baltussen, Da, Lammers and Martens 2021 JFE).

Who loses: liquidity providers into the close who take the other side of predictable hedging flow.
Should work: high-volatility days, the US indices (deep hedging flow).
Should fail: quiet days; days where news reverses the morning move.

## Event (causal)
Weekday d in the index's own time zone. Opens: NASDAQ / US500 09:30 New York, DAX 09:00 Berlin, JPN 09:00 Tokyo,
HK 09:30 Hong Kong. Closes REF = the T1 table (NASDAQ / US500 16:00 NY, DAX 17:30 Berlin, JPN 15:00 Tokyo -> 15:30
from 2024-11-05, HK 16:00 HK).
- r1 = log(close at the last bar at or before open(d) + 30 min / close at the last bar at or before REF(previous
  weekday)) (30-min staleness bound).
- Entry: the first bar closing at or after REF(d) - 30 min (<= 30 min after).
- LONG edge: r1 > 0. SHORT edge: r1 < 0.

## Counterfactual (drift-neutral, D090)
- C1: the same side, same entry time, on days with the opposite-sign first half-hour (r1 of the other sign). Same
  asset, period, clock time and frequency; only the morning signal is removed.

## Gate 0 cell and rule
Cell 1 h Event_Vol, EWM 150, 1 h vertical, 1:1 (D083 intraday cell; D093 EWM). PASS per asset and side (D090): C1 own
diff - 1.28 SE > 0 (full history), C1 diff > 0 on 2022+, >= 100 events in edge and C1, >= 2 events/week on 2022+.
Universe: NASDAQ, US500, DAX, JPN, HK.

## Expected firing rate (event COUNTS only, 2022+, computed before any return)
LONG / SHORT per week: NASDAQ 2.51 / 2.05; US500 2.51 / 2.01; DAX 2.67 / 2.18; JPN 2.25 / 1.93; HK 2.22 / 2.15.
Full-history counts >= 846 per set.

## Overlap with earlier cards
T1 / T1c conditioned on the WHOLE day's move to one hour before the close (entry 1 h before the close); T33 uses only
the first half-hour (overnight + opening) and enters 30 min before the close, as in Gao et al. The two signals are
correlated; T33 is recorded as a separate test (trials paid).

## Trial accounting
Test 46. Gate A-C on the D093 120-cell grid; all cells paid.
- 2026-11-10 (before any result was read): PLACEBO-only opening times added for the 5 non-index assets (XAUUSD / GBPJPY / BTCUSD 08:00 London, USDMXN 08:00 NY, USOIL 09:00 NY) because gate0_tool computes every asset; they get no verdict.
