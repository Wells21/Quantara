# T28: the Halloween season (Sell in May) -- equity indices (pre-registered 2026-11-10, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.
Gate 0 rule: D090 (per asset, drift-neutral C1 decides).

## Mechanism

S. Bouman and B. Jacobsen, "The Halloween indicator, 'Sell in May and go away': another puzzle", American Economic
Review 92(5), 2002: equity returns from November to April are far higher than from May to October, in 36 of 37
markets. B. Jacobsen and C. Zhang (working paper, "The Halloween indicator: everywhere and all the time", about 2018)
report it persists out of sample over centuries of UK data. Proposed causes: summer vacations reduce risk-bearing
capacity and liquidity (Bouman-Jacobsen), seasonal risk aversion. I have not checked page numbers.

Who pays: investors who stay fully invested through May-October, earning less per unit of risk than in winter.

Prediction, per equity index:
- **LONG** entered November-April earns more R than a LONG entered May-October;
- **SHORT** entered May-October earns more R than a SHORT entered November-April.

Disclosure:
- The two sides are one contrast with the side flipped.
- The state lasts six months. The tool clusters standard errors by ISO week, so the SE is STRONGLY understated: the
  independent units are seasons (about 2 per year). A season-clustered t (one value per half-year) is reported as a
  diagnostic beside the D090 verdict and must be read first; a pass with a weak season-clustered t is not credible.
- Seen data: every earlier card ran on these indices, but none conditioned on calendar half-year. T2 / T4 (month-end)
  and T7 (options expiry) used calendar positions within the month, not the season.
- Holding the index for six months is plain beta exposure; the edge here is the R difference between seasons.

## Rule (bars only)

- Check times: every 4 h on the UTC clock (00, 04, 08, 12, 16, 20); used only when a bar closed within 30 min before
  the check (signal) and one closes within 30 min after it (entry).
- Winter: the check's UTC month is November, December, January, February, March or April. Summer: May-October.
- LONG: **edge** = LONG at winter checks; **C1** = LONG at summer checks.
- SHORT: **edge** = SHORT at summer checks; **C1** = SHORT at winter checks.

Card cell: 4 h lookback Event_Vol, EWM 100, **24 h vertical**, 1:1.

Firing rate: every usable check in season, about 25-30 a week per side, half the year. The 2022+ events-per-week
figure averages over the whole calendar, so it is about half that.

Universe: NASDAQ, US500, DAX, JPN, HK. The other 5 assets are run and reported as placebos with no verdict.

## Verdict (D090)

Per index and side: C1 diff - 1.28 SE > 0 (full history), C1 diff > 0 on 2022+, >= 100 events in edge and C1,
>= 3 events a week on 2022+. Month-matched drift is not defined for this card (the state is the month). Season-clustered
t is a diagnostic, read first.

Trial count: Gate 0 test 41 of the campaign.
