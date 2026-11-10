# T24: the volatility-managed risk premium -- equity indices (pre-registered 2026-11-09, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.
Gate 0 rule: D090 (per asset, drift-neutral C1 decides).

## Mechanism

A. Moreira and T. Muir, "Volatility-managed portfolios", Journal of Finance 72(4), 2017: realized volatility is
persistent, but expected returns do not rise in proportion with it. So the expected return per unit of risk is high
in calm periods and low in turbulent ones, and scaling exposure by 1/variance earns alpha on the equity market (and on
other factors). Who pays: investors who hold constant notional exposure through volatility spikes and then sell into
them (risk limits, margin, VaR), and who under-allocate in calm markets.

Our labels are in volatility units (barriers = Event_Vol multiples, R = return / Event_Vol). Mean R is therefore a
reward-per-unit-risk measure, which is exactly the quantity the paper says differs across volatility states.

Prediction, per equity index:
- **LONG** entered when realized volatility is BELOW its own past-year median earns more R than a LONG entered when it
  is above;
- **SHORT** entered when realized volatility is ABOVE its median earns more R than a SHORT entered when it is below.

Disclosure:
- The two sides are one contrast (low-vol vs high-vol state) with the side flipped, not independent evidence.
- Volatility states persist for weeks to months. The tool clusters standard errors by ISO week, so the SE is
  understated if states persist longer; read a marginal z with that in mind. Diagnostic: 2014-2021 and 2022+ slices.
- Related earlier cards: T5 (vol-target flow into the close) and T6 (dealer gamma regime) failed; T14 (reversal after
  high-vol moves) failed. None tested the unconditional state contrast.

## Rule (bars only)

- Check times: every 4 h on the UTC clock (00, 04, 08, 12, 16, 20). A check is used only when a bar closed within
  30 min before it (signal) and one closes within 30 min after it (entry, the first bar at or after the check).
- Hourly series: the last close in each UTC hour; hourly log returns.
- sigma_t = standard deviation of the last 120 hourly log returns ending at or before the check (at least 60).
- m_t = median of sigma at the checks of the PRIOR 365 days (at least 100 checks), strictly before t.
- Low state: sigma_t < m_t. High state: sigma_t >= m_t.
- LONG: **edge** = LONG at low-state checks; **C1** = LONG at high-state checks (same asset, side, period and
  frequency; only the volatility state differs: drift-neutral).
- SHORT: **edge** = SHORT at high-state checks; **C1** = SHORT at low-state checks.

Card cell: 4 h lookback Event_Vol, EWM 100, **24 h vertical**, 1:1.

Firing rate: about half of ~25-30 usable checks a week, i.e. > 10 a week per side.

Universe: NASDAQ, US500, DAX, JPN, HK. The other 5 assets are run and reported as placebos with no verdict.

## Verdict (D090)

Per index and side: C1 diff - 1.28 SE > 0 (full history), C1 diff > 0 on 2022+, >= 100 events in edge and C1,
>= 3 events a week on 2022+. Month-matched drift and pooled results are diagnostics.

Trial count: Gate 0 test 37 of the campaign.
