# T27: frog in the pan -- continuous vs discrete information, all 10 assets (pre-registered 2026-11-10, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.
Gate 0 rule: D090 (per asset, drift-neutral C1 decides).

## Mechanism

Z. Da, U. Gurun and M. Warachka, "Frog in the pan: continuous information and momentum", Review of Financial Studies
27(7), 2014: investors under-react to information that arrives continuously in many small amounts (limited attention:
small changes do not trigger a response), and react fully to information that arrives in a few large, salient jumps.
So a past move built from many small same-direction steps keeps drifting in that direction; a move of the same sign
made of a few big jumps does not. They measure it with information discreteness
ID = sign(PRET) * (% of negative-return periods - % of positive-return periods); low ID = continuous.
I have not checked page numbers.

The paper is cross-sectional (stocks, daily returns over a year, monthly holding). Applying it to each asset's own time
series on hourly returns over a week with a 24 h hold is OUR extension.

Who pays: traders who react only to salient jumps and ignore a steady grind -- they keep selling (buying) into a gradual
rise (fall) until it becomes obvious.

Prediction, per asset:
- **LONG** after an UP week (PRET > 0) earns more R when the rise was continuous (low ID) than when it was discrete
  (high ID);
- **SHORT** after a DOWN week (PRET < 0) earns more R when the fall was continuous (low ID) than when it was discrete.

Disclosure:
- The two sides use different weeks (up weeks vs down weeks), so they are not the same contrast flipped.
- Related known results: T22 (weekly time-series momentum, test ~33) FAILED Gate 0 everywhere at the time; under D090
  post hoc the BTCUSD pair passed Gate 0 and then died at Gate C / PBO (2026-11-10). T27 does not test momentum itself:
  both edge and C1 sit in same-sign weeks; only the path shape differs (drift-neutral).
- ID is related to realized skewness (T26, test 39): a discrete up-week tends to have positive skew. T26 FAILED every
  asset; on BTCUSD and XAUUSD high skew was followed by HIGHER returns (z about -3.1 / -2.4 against T26's sign). If ID
  tracks skew, that known result points AGAINST this card's LONG side on BTCUSD and XAUUSD. Stated before the run.
- States persist about a week; week-clustered SE is roughly appropriate.

## Rule (bars only)

- Check times: every 4 h on the UTC clock (00, 04, 08, 12, 16, 20); used only when a bar closed within 30 min before
  the check (signal) and one closes within 30 min after it (entry).
- Hourly series: the last close in each UTC hour; hourly log returns r over consecutive hours.
- Over the last 168 hourly returns ending at or before the check (at least 100): PRET = sum(r);
  ID = sign(PRET) * (share of r < 0 - share of r > 0) (zero returns count in neither share).
- Up-week checks: PRET > 0. Down-week checks: PRET < 0.
- m_up_t = median of ID over the UP-week checks of the PRIOR 365 days (at least 100), strictly before t; m_dn_t the same
  over DOWN-week checks.
- LONG: **edge** = up-week checks with ID < m_up; **C1** = up-week checks with ID >= m_up.
- SHORT: **edge** = down-week checks with ID < m_dn; **C1** = down-week checks with ID >= m_dn.

Card cell: 4 h lookback Event_Vol, EWM 100, **24 h vertical**, 1:1.

Firing rate: about a quarter of usable checks per side: about 9 a week on 24/7 assets, about 6 a week on equity indices.

Universe: all 10 assets (the mechanism is generic investor attention).

## Verdict (D090)

Per asset and side: C1 diff - 1.28 SE > 0 (full history), C1 diff > 0 on 2022+, >= 100 events in edge and C1,
>= 3 events a week on 2022+. Month-matched drift and pooled results are diagnostics.

Trial count: Gate 0 test 40 of the campaign.
