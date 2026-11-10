# T26: realized skewness and lottery demand -- all 10 assets (pre-registered 2026-11-10, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.
Gate 0 rule: D090 (per asset, drift-neutral C1 decides).

## Mechanism

D. Amaya, P. Christoffersen, K. Jacobs and A. Vasquez, "Does realized skewness predict the cross-section of equity
returns?", Journal of Financial Economics 118(1), 2015: stocks with high realized skewness (from intraday returns)
over the past week earn LOWER returns the next week; low (negative) skewness earns higher returns. Mechanism:
preference for lottery-like payoffs (Barberis and Huang 2008; Kumar 2009) -- investors overpay for assets that just
showed large upside jumps and shun those that just showed downside jumps. I have not checked page numbers.

The paper is cross-sectional (stocks). Applying it to each asset's own time series is OUR extension: the claim is that
the same asset is temporarily overpriced after a week of positive-skew trading and underpriced after negative skew.

Who pays: lottery-seeking traders (retail, levered CFD / crypto traders) who chase upside jumps and dump after
crashes.

Prediction, per asset:
- **LONG** entered after LOW recent realized skewness earns more R than a LONG entered after HIGH skewness;
- **SHORT** entered after HIGH skewness earns more R than a SHORT entered after LOW skewness.

Disclosure:
- The two sides are one contrast with the side flipped.
- Skewness has not been computed on any Quantara asset before this card (no placebo or diagnostic has shown it).
- Skewness states persist about a week (the window); week-clustered SE is roughly appropriate.

## Rule (bars only)

- Check times: every 4 h on the UTC clock (00, 04, 08, 12, 16, 20); used only when a bar closed within 30 min before
  the check (signal) and one closes within 30 min after it (entry).
- Hourly series: the last close in each UTC hour; hourly log returns r over consecutive hours.
- RSK_t over the last 168 hourly returns ending at or before the check (at least 100):
  RSK = sqrt(N) * sum(r^3) / (sum(r^2))^1.5 (the paper's definition, on hourly instead of 5-minute returns).
- m_t = median of RSK at the checks of the PRIOR 365 days (at least 100 checks), strictly before t.
- Low state: RSK_t < m_t. High state: RSK_t >= m_t.
- LONG: **edge** = LONG at low-state checks; **C1** = LONG at high-state checks (same asset, side, period and
  frequency; only the skewness state differs: drift-neutral).
- SHORT: **edge** = SHORT at high-state checks; **C1** = SHORT at low-state checks.

Card cell: 4 h lookback Event_Vol, EWM 100, **24 h vertical**, 1:1.

Firing rate: about half of the usable checks, > 8 a week per side on every asset.

Universe: all 10 assets (the mechanism is generic investor preference).

## Verdict (D090)

Per asset and side: C1 diff - 1.28 SE > 0 (full history), C1 diff > 0 on 2022+, >= 100 events in edge and C1,
>= 3 events a week on 2022+. Month-matched drift and pooled results are diagnostics.

Trial count: Gate 0 test 39 of the campaign.
