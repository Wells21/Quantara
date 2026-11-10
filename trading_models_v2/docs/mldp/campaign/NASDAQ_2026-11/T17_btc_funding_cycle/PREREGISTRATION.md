# T17: Bitcoin perpetual-funding cycle (OUR thesis; pre-registered 2026-11-09, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.

## Mechanism

Most Bitcoin leverage sits in perpetual swaps. The large venues (Binance, Bybit, OKX) settle funding every 8 hours,
at 00:00, 08:00 and 16:00 UTC, for BTC-USDT perpetuals. A position open at the timestamp pays or receives the
funding rate:
- funding > 0 (perp above spot, the usual state after a rise): longs pay shorts. Short-horizon longs close just
  BEFORE the timestamp to avoid paying and re-open AFTER it. Price is pushed down into the timestamp and rebounds
  after it;
- funding < 0 (after a fall): shorts pay. Shorts cover before the timestamp and re-short after it. Price is pushed up
  into the timestamp and falls after it.

We have no funding data. Funding tracks the perp premium, which follows recent momentum, so the sign of the prior
24 h return is the proxy for the sign of funding. Prediction: right AFTER a funding timestamp, the price moves in
the direction of the prior 24 h return (the paying side re-opens).

The literature on crypto funding-settlement intraday patterns is mixed, and I am not citing a specific paper. The
mechanism is the venues' contract design.

## Rule (bars only)

For every day and each funding time F in {00:00, 08:00, 16:00} UTC:
- entry = the first bar closing at or after F, at most 30 min later;
- proxy q = log(P(F) / P(F - 24 h)), with prices = close of the last bar at or before t, at most 30 min stale; q != 0.

Events:
- **edge**: at F, side = sign(q). LONG after a 24 h rise (positive funding: longs re-open), SHORT after a 24 h fall;
- **C1 (main)**: the same rule at the mid-points between funding times, F + 4 h (04:00, 12:00, 20:00 UTC). It is the
  same 24 h momentum signal with no settlement. If it works as well, the effect is generic momentum;
- **C2**: at F with the side reversed. LONG after a 24 h fall, SHORT after a rise: the funding proxy then predicts the
  opposite re-opening flow.

Firing rate: 3 timestamps x 7 days = 21 a week, about 10 per side.

Universe: BTCUSD only (the mechanism is specific to crypto perpetuals). The other 9 assets are run and reported as
placebos with no verdict. Gate 0 cell H1_V1 (D083).

## Verdict

The Rev 47 Gate 0 rule on BTCUSD (pooled = BTCUSD: drift and C1 at one-sided 90 %; C2 diff > 0; own diffs > 0), plus
recency: the drift diff must be > 0 on 2022+. Diagnostic slices: BTCUSD data starts 2017-05; 2017-2021 and 2022+.
Events per week on 2022+ are reported.

Trial count: Gate 0 test 30 of the campaign.
