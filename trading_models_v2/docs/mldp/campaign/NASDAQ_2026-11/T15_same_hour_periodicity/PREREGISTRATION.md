# T15: same-hour intraday periodicity (OUR thesis; pre-registered 2026-11-09, before any run)

Owner 2026-11-09 standing rules: "take the wheel" (1 long + 1 short edge on each of the 10 assets), and every edge must
fire at least 3 times a week (high-recall primary).

## Mechanism

Some institutional order flow recurs at the same clock time every day. Examples:
- execution algorithms that slice parent orders on fixed schedules (VWAP/TWAP across the day);
- benchmark fixes and settlement windows;
- session hand-overs (Tokyo -> London -> New York);
- hedging and rebalancing desks that act at set hours.

While a recurring flow lasts (days to weeks), it pushes the price the same way in the same clock hour. The recent sign
of an hour's return therefore carries information about that same hour today. This is the time-of-day analogue of
Heston, Korajczyk and Sadka, "Intraday patterns in the cross-section of stock returns", Journal of Finance 65(4), 2010
(page numbers NOT checked). They find that a stock's return in a given half-hour predicts its return in the same
half-hour on later days, for many days, and link it to systematic institutional trading. Their evidence is
cross-sectional, in stocks. The single-market clock-hour version here is ours.

## Rule (bars only, decided at the start of each UTC clock hour)

For each asset and each UTC clock hour h (00:00 .. 23:00):
- price at time t = close of the last bar at or before t, at most 30 min stale;
- slot return r(D, h) = log(P(D, h+1) / P(D, h)), defined when both prices are fresh;
- signal on day D: the last K = 10 earlier days with a defined r(., h) (strictly before D);
  m = their mean, s = their standard deviation, tstat = m / (s / sqrt(10));
- entry = the first bar closing at or after D h:00, at most 30 min later.

Events (side = sign of the signal):
- **edge**: |tstat(h)| >= 1, side = sign(tstat(h)); "the same hour's recent drift repeats";
- **C1 (main)**: the same entry hour, but the signal comes from the PREVIOUS clock hour's history (h - 1):
  |tstat(h-1)| >= 1, side = sign(tstat(h-1)). It has the same type of recent-return information, minus the
  slot identity. If it works as well, the effect is generic momentum, not periodicity;
- **C2**: the same hour with a weak signal, |tstat(h)| < 1, side = sign(tstat(h)). Periodic flow should concentrate
  where the hour's pattern is strong.

Each edge event holds for the 1 h vertical, i.e. exactly its own slot. Consecutive hours can both be events; their
holds do not overlap.

Expected firing rate: under no pattern |tstat| >= 1 holds about 32 % of the time. That is about 7 events a day on a
24 h market, about 15-20 a week per side, well above the 3 a week floor.

Universe (pooled, D075 rule C): all 10 assets, both sides. Gate 0 cell H1_V1 (D083): 1 h lookback Event_Vol, EWM 100,
1 h vertical, 1:1.

## Verdict

The Rev 47 Gate 0 rule (pooled drift and C1 at one-sided 90 %; C2 diff > 0; the asset's own diffs > 0), plus
recency: the asset's own drift diff must be > 0 on 2022+. The diagnostic slices are 2014-2021 and 2022+. Events per
week on 2022+ are reported per asset and side (firing-rate rule: >= 3).

Trial count: Gate 0 test 28 of the campaign.
