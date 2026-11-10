# T29: 12-month time-series momentum -- all 10 assets (pre-registered 2026-11-10, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.
Gate 0 rule: D090 (per asset, drift-neutral C1 decides).

## Mechanism

T. Moskowitz, Y. Ooi and L. Pedersen, "Time series momentum", Journal of Financial Economics 104(2), 2012: an asset's
own past 12-month excess return predicts its next-month return, in 58 futures and forwards (equity indices,
currencies, commodities, bonds). Proposed causes: initial under-reaction to information and later over-reaction;
hedgers paying speculators to take the other side. B. Hurst, Y. Ooi and L. Pedersen, "A century of evidence on
trend-following investing", Journal of Portfolio Management 44(1), 2017, report it out of sample back to 1880.
I have not checked page numbers.

Who pays: hedgers and slow-moving investors who trade against established trends.

Prediction, per asset:
- **LONG** entered when the past 12-month return is positive earns more R than a LONG entered when it is negative;
- **SHORT** entered when the past 12-month return is negative earns more R than a SHORT entered when it is positive.

Our hold (24 h) is much shorter than the paper's month.

Disclosure:
- The two sides are one contrast with the side flipped.
- Seen data: T22 (1-week momentum, test ~33) failed everywhere; its BTCUSD post-hoc pair died at Gate C / PBO. T25
  (test 38) used price vs the 52-week HIGH on the equity indices (HK passed, died at Gate C / PBO; the other 5 assets
  were run as placebos, so their T25 results are seen). The 12-month return sign is correlated with T25's distance
  from the high, so this card is only partly new evidence.
- The state lasts months. The tool clusters SE by ISO week, which overstates z. A quarter-clustered t (one mean per
  calendar quarter and state) is reported as a diagnostic and read first.

## Rule (bars only)

- Check times: every 4 h on the UTC clock (00, 04, 08, 12, 16, 20); used only when a bar closed within 30 min before
  the check (signal) and one closes within 30 min after it (entry).
- Hourly series: the last close in each UTC hour.
- R12_t = log(close at the check / the last hourly close at or before t - 365 days). The first 365 days are warm-up.
- Up state: R12 > 0. Down state: R12 < 0.
- LONG: **edge** = LONG at up-state checks; **C1** = LONG at down-state checks (drift-neutral: same asset, side,
  period, check grid).
- SHORT: **edge** = SHORT at down-state checks; **C1** = SHORT at up-state checks.

Card cell: 4 h lookback Event_Vol, EWM 100, **24 h vertical**, 1:1.

Firing rate: every usable check in the state; well above 3 a week whenever the state holds. The 2022+ events-per-week
average depends on how long each state lasted.

Universe: all 10 assets (the paper covers all four asset classes).

## Verdict (D090)

Per asset and side: C1 diff - 1.28 SE > 0 (full history), C1 diff > 0 on 2022+, >= 100 events in edge and C1,
>= 3 events a week on 2022+. Month-matched drift and pooled results are diagnostics; quarter-clustered t is read first.

Trial count: Gate 0 test 42 of the campaign.
