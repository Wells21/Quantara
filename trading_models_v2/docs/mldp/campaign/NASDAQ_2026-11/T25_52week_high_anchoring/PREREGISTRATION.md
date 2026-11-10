# T25: the 52-week-high anchor -- equity indices (pre-registered 2026-11-09, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.
Gate 0 rule: D090 (per asset, drift-neutral C1 decides).

## Mechanism

J. Li and J. Yu, "Investor attention, psychological anchors, and stock return predictability", Journal of Financial
Economics 104(2), 2012: the nearness of the Dow to its 52-week high positively predicts future aggregate market
returns (nearness to its all-time high predicts negatively). Investors anchor on the 52-week high: good news that
pushes the index toward it is under-reacted to (traders hesitate to bid through the anchor), so the price keeps
drifting up as the news is absorbed. Far below the anchor, bad news has been under-reacted to in the same way.
George and Hwang (Journal of Finance 59(5), 2004) document the same anchor in the cross-section of stocks.
I have not checked page numbers.

Who pays: investors who sell into strength near the old high (anchoring on it as "expensive") and buy dips far below
it (anchoring on it as the "fair" level).

Prediction, per equity index (our horizon is shorter than the paper's monthly one):
- **LONG** entered when the index is nearer than usual to its 52-week high earns more R than a LONG entered when it is
  farther than usual;
- **SHORT** entered when it is farther than usual earns more R than a SHORT entered when it is nearer.

Disclosure:
- The two sides are one contrast with the side flipped.
- Correlated with T24 (test 37): being near the high often coincides with low volatility. T24's NASDAQ pass and its
  JPN / HK opposite signs are known. This card tests a different state variable (price vs its anchor), not volatility.
- States persist for weeks; the week-clustered SE is understated if they persist longer.

## Rule (bars only)

- Check times: every 4 h on the UTC clock (00, 04, 08, 12, 16, 20); used only when a bar closed within 30 min before
  the check (signal) and one closes within 30 min after it (entry).
- Hourly series: the last close in each UTC hour.
- x_t = log(close at the check / maximum hourly close over the 365 days ending at the check). x_t <= 0.
- m_t = median of x at the checks of the PRIOR 365 days (at least 100 checks), strictly before t.
- Near state: x_t >= m_t. Far state: x_t < m_t.
- LONG: **edge** = LONG at near-state checks; **C1** = LONG at far-state checks (same asset, side, period and
  frequency; only the anchor distance differs: drift-neutral).
- SHORT: **edge** = SHORT at far-state checks; **C1** = SHORT at near-state checks.
- The first 365 days of each asset are warm-up (no events).

Card cell: 4 h lookback Event_Vol, EWM 100, **24 h vertical**, 1:1.

Firing rate: about half of ~25-30 usable checks a week per side.

Universe: NASDAQ, US500, DAX, JPN, HK. The other 5 assets are run and reported as placebos with no verdict.

## Verdict (D090)

Per index and side: C1 diff - 1.28 SE > 0 (full history), C1 diff > 0 on 2022+, >= 100 events in edge and C1,
>= 3 events a week on 2022+. Month-matched drift and pooled results are diagnostics.

Trial count: Gate 0 test 38 of the campaign.
