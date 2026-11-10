# T4 -- Month-end payment-cycle liquidity (settlement-locked): thesis card + Gate 0 pre-registration

Written 2026-11-05, before any data on this thesis was looked at (owner: keep trying on NASDAQ until it has a live long
and a live short). Rules: D075 (rule C universe, tiered drift bar). Entry bars are causal (first bar closing at or
after the reference close).

Not the same as T2: T2 traded the month-to-date move (60/40 rebalancing) and failed. T4 is a calendar mechanism with
no sign condition: forced selling before the settlement deadline, then a reversal plus new-money buying.

## Source (read 2026-11-05)

Etula, Rinne, Suominen & Vaittinen (2020), "Dash for Cash: Monthly Market Impact of Institutional Liquidity Needs",
RFS 33(1):75-111 (open-access version of record, Aalto repository; page numbers are that reprint's PDF pages).
- Mechanism (PDF p.4, p.12): pension and other payments must reach recipients on the morning of the last trading day T.
  Under T+3 settlement, institutions must sell by the close of T-4.
- Daily returns (Figure 4, PDF p.5-6, CRSP VW, July 1995 - 2013):
  - low T-8..T-4 ("selling pressure");
  - high T-3..T-1 ("positive reversal") and T..T+3 ("buying pressure");
  - low T+4..T+8 ("negative reversal").
- Since 1995 (PDF p.13): T-8..T-4 abnormal -37 bps; T-3..T-1 +25 bps; T-3..T+3 +48 bps.
- In 2003-2013 the positive-reversal periods earned 73 % of the market's excess return (PDF p.6).
- Settlement locks the timing:
  - Europe's move from T+3 to T+2 on 6 October 2014 moved the reversal closer to month end (difference-in-
    differences, Table 3, PDF p.16-17);
  - the reversal is 2.5 times larger when T is a Friday, when the weekly and monthly pay cycles coincide (PDF p.17);
  - 20 of 23 developed markets show significant reversals (PDF p.7).
- The paper's sample ends in 2013. The US moved to T+2 in September 2017 (PDF p.11 note 16) and to T+1 on
  28 May 2024.

## Card

- **Mechanism.** Institutions that must pay cash at month end (pensions, mutual-fund redemptions) sell equities up to
  the last day that settles in time, regardless of price; prices fall. After the deadline the selling stops and
  prices revert. Paid-out cash is then reinvested at the turn of the month.
- **Who loses and why.** The forced month-end sellers, who sell at temporarily low prices because of the settlement
  deadline, not information (the paper estimates about $30.6 billion a year, PDF p.8).
- **Settlement adaptation (pre-registered).** With s = the equity settlement days in force at T, shift the paper's
  T+3 windows by (3 - s) days toward T:
  - last forced-selling day = T-(s+1);
  - selling window = T-(s+5)..T-(s+1) (five days);
  - reversal = T-s..T-1;
  - buying = T..T+3.
  - s per asset: NASDAQ, US500: 2 until 2024-05-27, 1 from 2024-05-28. DAX, JPN, HK: 2 (DAX since 2014; JPN since
    16 July 2019; HK long-standing).
  - At s = 3 this reproduces the paper's windows exactly.
- **Trading days.** Weekdays with a bar closing at or after the asset's reference close (T1's REF times), at most
  30 minutes after. T = the last such day of the calendar month. k = a day's trading-day offset from T: -1, -2, ...
  before T, and +1..+8 after T counted from the previous month's T.
- **Events (one per trading day D).**
  - Entry at the causal bar after the close of the trading day before D; the trade covers day D (24 h vertical).
  - LONG when D is in the reversal + buying window: k in [-s, +3].
  - SHORT when D is in the selling window: k in [-(s+5), -(s+1)].
- **Universe (rule C, pre-registered):** NASDAQ, US500, DAX, JPN, HK -- the developed equity indices where the paper
  finds the pattern. The 5 non-equity assets are computed and reported, not pooled.
- **Regimes.**
  - Should work: months where T is a Friday; volatile months (PDF p.8 note 9).
  - Should fail: mid-month days; non-equity assets.
- **Counterfactual (gating).**
  - **C1 calendar:** the same side on the "other days" of the month (k outside [-(s+6), +8], the set of Figure 5).
    Prediction: LONG in-window beats LONG on other days; SHORT in-window beats SHORT on other days.
  - One counterfactual only: a settlement-shift placebo was considered but has fewer than 100 events in 4 years.
- **Diagnostics (reported, not gating).**
  - Months where T is a Friday vs other months (LONG).
  - The T+4..T+8 negative reversal (SHORT).
  - The T+2 era vs the T+1 era for NASDAQ / US500.
- **Falsifiers.**
  - F1: the window days earn the same as other days (C1 fails).
  - F2: the windows do not beat same-month random entries (drift baseline fails).

## Gate 0 (pre-registered)

- **Cell:** H4_V24 (4 h lookback, EWM 100, 24 h vertical, 1:1) -- the one-day hold of the thesis; the D075 default.
- **Method:** shared/research/gate0/gate0_tool.py T4. Same labeler, month-matched drift baseline (10,000 draws, seed
  20261102), joint week-clustered SE for C1, one-sided 95 %, pooled over the universe. Tiered drift bar: 90 % if C1
  passes at 95 %.
- **Verdict per side for NASDAQ:** PASS if the pooled drift baseline and C1 pass and NASDAQ's own diffs are > 0.
  A FAIL kills T4 for that side as written (no re-tuning of windows, settlement days, the cell or the universe).

## Amendments before any result (2026-11-05; event counts only, no labels looked at)

- **Entry bar.** A first check showed US quote bars after 16:00 New York often span the CME maintenance break: on
  44 % of US500 weekdays and 25 % of NASDAQ weekdays no bar closes between 16:00 and 18:00. Trading days and entries
  are therefore defined by the first bar closing at or after (reference close - 30 min), accepted if it closes by the
  reference close + 30 min. Still causal. For a one-day hold, entering up to 30 minutes before the close does not
  change the claim.
- **Counts after the amendment (NASDAQ):** 1,039 trading days; 280 LONG-window, 251 SHORT-window and 208 other days.
  The other universe assets have similar counts.
- **Disclosed property of the cell.** The 24 h vertical is calendar time. An event entered before a weekend or a
  holiday (about 20 % of events) expires at the reopen, so it covers the gap and not the next session. This is how
  every calendar-time vertical in the pipeline works; the cell stays as pre-registered.

## RESULT (2026-11-05, gate0_T4.json) -- FAIL, both sides

Pooled over NASDAQ, US500, DAX, JPN and HK, one-sided 95 % (lower bound in brackets). The tiered bar does not apply
because C1 fails.

| side | events | drift diff | C1 calendar |
|---|---|---|---|
| LONG | 1,532 | -0.012 (-0.075) | -0.038 (-0.119) |
| SHORT | 1,309 | -0.015 (-0.086) | +0.018 (-0.063) |

- **NASDAQ LONG:** 280 events, mean +0.023 R, drift +0.017, C1 +0.005. Small and not significant.
- **NASDAQ SHORT:** 251 events, mean -0.114 R, drift -0.109, C1 -0.086. The opposite of the prediction: shorting the
  settlement-adjusted selling window loses.
- **Across the universe:** in-window days do not beat other days on either side.
- **Note:** the mechanism was documented on 1995-2013 data under T+3 settlement and is not visible in 2022-2026 under
  T+2 / T+1.

T4 is killed as written for both sides.
