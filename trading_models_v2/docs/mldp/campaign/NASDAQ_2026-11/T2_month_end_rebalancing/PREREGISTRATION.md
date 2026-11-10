# T2 -- Month-end rebalancing pressure (equity leg): thesis card + Gate 0 pre-registration

Written 2026-11-04 before any data on this thesis was looked at (owner: "Move on to T2 ... approved"). D075 with rule C
as amended 2026-11-04 (universe pre-registered on the card).

## Card

- **Mechanism.** Pension and balanced funds rebalance to fixed equity/bond weights on a calendar, concentrated in the
  last days of the month. In a month when equities have outperformed, they are overweight and SELL equities into
  month-end; when equities underperformed, they BUY. These trades come from the mandate and from month-end cash needs
  (benefit payments), not from information, so the month-end equity return moves against the month-to-date move.
- **Who loses and why.** The rebalancing funds: they sell after rises and buy after falls on a predictable schedule
  and pay the price impact; front-runners profit.
- **Sources (read 2026-11-04).**
  - Harvey, Mazzoleni & Melone, "The Unintended Consequences of Rebalancing" (WP, 14 January 2026). When stocks are
    overweight, funds sell stocks and buy bonds, "leading to a decrease in equity returns of 17 basis points over the
    next day". The Calendar signal's predictability "peaks in the last four days of the month"; calendar signals
    "also extend to international equity markets".
  - Etula, Rinne, Suominen & Vaittinen (2020), RFS 33(1):75-111: month-end liquidity needs move liquid markets.
- **Our version (price-only).** The equity leg alone: we hold no bond series. Equity volatility dominates the 60/40
  drift (a 10 % equity outperformance moves the weight about 2.26 points), so the equity month-to-date move carries
  most of the signal. Recorded as a limitation.
- **Universe (rule C, pre-registered):** the equity indices NASDAQ, US500, DAX, JPN and HK, where index-tracking
  pension / balanced holdings are the mechanism's subject. The other 5 assets are computed and reported, not pooled.
- **Trigger (both sides).** On each of the last 4 trading days of each calendar month, at the asset's cash close
  (T1's reference times), let r = the log return from the previous month's last trading day's close to today's close.
  - r > 0: SHORT.
  - r < 0: LONG.
  - Hold 24 hours. Recall-first: no magnitude filter; |r| becomes a feature.
  - Trading days are the weekdays with a bar within 30 minutes before the reference close.
- **Regimes.**
  - Should work: months with a large month-to-date move; equity indices.
  - Should fail: mid-month; small month-to-date moves; non-equity assets.
- **Counterfactuals (gating).**
  - **C1 sign:** the same side traded on last-4-day events whose month-to-date sign is the opposite (LONG after an up
    month, SHORT after a down month). Prediction: edge > inverted.
  - **C2 timing:** the same rule on trading days 8-11 of the month (mid-month, with the month-to-date move to that
    day). Prediction: month-end > mid-month.
- **Diagnostic (not gating).** C3 magnitude: top half of |r| against bottom half; prediction top > bottom.
- **Falsifiers.**
  - F1: the month-to-date sign does not matter.
  - F2: mid-month works as well.

## Gate 0 (pre-registered)

- **Cell:** H4_V24 -- 4 h lookback, EWM 100, 24 h vertical, 1:1. The thesis horizon is the next day; this is the D075
  default cell.
- **Method:** labeler, drift baseline (month-matched, 10,000 draws, seed 20261102), joint-SE counterfactuals and the
  one-sided 95 % standard as in T1. Tool: shared/research/gate0/gate0_tool.py T2.
- **Pooled:** over the universe. **PASS = diff - 1.645 SE > 0** for the drift baseline, C1 and C2.
- **Verdict per side for NASDAQ:** PASS if the pooled drift baseline, C1 and C2 pass and NASDAQ's own diffs are > 0.
  A FAIL kills T2 as written (no re-tuning of the day window, the cell or the universe).

## RESULT (2026-11-04, gate0_T2.json) -- FAIL (both sides)

Pooled over the universe (NASDAQ, US500, DAX, JPN, HK; one-sided 95 %, lower bound in brackets):

| side | events | drift diff | C1 sign | C2 timing |
|---|---|---|---|---|
| LONG | 429 | +0.124 (-0.010) | +0.040 (-0.088) | +0.058 (-0.085) |
| SHORT | 626 | +0.044 (-0.058) | +0.037 (-0.091) | -0.014 (-0.129) |

NASDAQ alone: LONG drift +0.133, C1 +0.024, C2 +0.019; SHORT drift -0.026, C1 +0.009, C2 -0.085. All far from
significance (75 / 128 events).

What it shows:
- The counterfactuals that would show the rebalancing mechanism are near zero: the month-to-date sign does not
  matter, and month-end is no better than mid-month.
- The positive long drift diff therefore has no support from the theory.

T2 is killed as written (no re-tuning). One Gate 0 attempt, counted.
