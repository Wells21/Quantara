# T22: weekly time-series momentum, all 10 assets, both sides (pre-registered 2026-11-09, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.

## Mechanism

Time-series momentum (TSMOM): an asset's own past return predicts its next return, with the same sign, in equity
indices, currencies, commodities and bonds, on both the long and the short side. Sources (page numbers NOT checked):
- Moskowitz, Ooi and Pedersen, "Time series momentum", Journal of Financial Economics 104, 2012;
- Hurst, Ooi and Pedersen, "A century of evidence on trend-following investing", Journal of Portfolio Management 44, 2017;
- Baltas and Kosowski, "Momentum strategies in futures markets and trend-following funds", 2013 (daily and weekly
  rebalancing).

The explanation the literature gives: initial under-reaction to news (slow-moving capital, anchoring, the disposition
effect), followed by delayed over-reaction and hedging-pressure flows. It is one of the few documented premia that
earns on the short side, mostly in falling markets. That fits the open gaps: shorts on every asset, and both sides on
GBPJPY, USDMXN, USOIL and BTCUSD.

Disclosures:
- Gate 0 compares the edge with random entries of the same side IN THE SAME MONTH (D075 drift baseline). A
  monthly-scale trend premium is the month's drift itself, and month matching removes it by construction. Gate 0 can
  only test the WEEKLY signal, which changes sign within a month. This card is therefore the weekly version: 1-week
  lookback, 1-day hold. The literature's evidence is strongest at 1-12 month lookbacks, so this is a real risk.
- Old Quantara families (trend, rocmomentum, trendpullbackcontinuation, trendefficiencycontinuation, stcmomentumturn)
  were tried in the pre-Gate-0 system, with indicator triggers and model filters. This is the first Gate 0 test of
  the time-series premium itself.

## Rule (bars only)

- Check times: 00:00, 08:00 and 16:00 UTC, every calendar day.
- Signal at check time t: q = ln(Close[last bar at or before t] / Close[last bar at or before t - 168 h]). Both bars
  must be at most 30 min stale; otherwise there is no check.
- z = q / the standard deviation of q over the checks in the 365 days strictly before t (at least 100 checks).
- Entry: the first bar closing at or after t, at most 30 min later.

Events, per side (side +1 LONG, -1 SHORT; trend = side * z):
- **edge**: trend >= +0.5 (with the 1-week trend);
- **C1 (main)**: trend <= -0.5. The same side against the 1-week trend. TSMOM predicts edge > C1; short-term
  reversal predicts the opposite;
- **C2**: |z| < 0.5, the same side with no clear trend. TSMOM predicts edge > C2.

Events overlap (an 8 h spacing with a 24 h hold). The week-clustered SEs in the tool absorb that.

Gate 0 cell: 8 h lookback Event_Vol, EWM 100, 24 h vertical, 1:1. It is inside the Gate A grid.

## Firing rate

There are 15 checks a week (21 for BTCUSD, which also trades at weekends). About 31 % should land on each side's
edge (|z| >= 0.5 is about 62 % for a normal z). That is about 4.6 events a week per side, or 6.5 for BTCUSD. The
results report the actual events per week on 2022+.

## Verdict

The Rev 47 Gate 0 rule, pooled over all 10 assets per side (D075 rule C): pooled drift and C1 at one-sided 90 %;
C2 diff > 0; the asset's own diffs > 0. Recency: the asset's own drift diff must be > 0 on 2022+. Diagnostic slices:
2014-2021 and 2022+; hour-matched drift. Each asset gets its own verdict (cross-asset coverage).

Trial count: Gate 0 test 35 of the campaign.
