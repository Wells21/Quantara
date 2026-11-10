# T3 -- Overnight dealer-inventory reversal at the European open: thesis card + Gate 0 pre-registration

Written 2026-11-05, before any data on this thesis was looked at (owner 2026-11-04: T3 "go ahead"; keep trying on
NASDAQ until it has a live long and a live short). Rules: D075 (rule C universe, tiered drift bar), D083 (H1_V1).
The causal entry rule (first bar closing at or after the entry time) is used from the start (lesson of T1b / T1bc).

## Source (read 2026-11-05)

Boyarchenko, Larsen & Whelan, "The Overnight Drift", Federal Reserve Bank of New York Staff Report 917 (Feb 2020,
revised Aug 2022); published in the Review of Financial Studies 36(9):3502-3547 (2023). Read from the NY Fed copy.
- S&P 500 e-mini futures (ES), tick trades and quotes, January 1998 - December 2020 (p.6-7).
- The largest positive returns are between 2:00 and 3:00 ET, the European open: 3.7 % a year, 1.48 bps a day (p.1).
- Mechanism: Grossman-Miller inventory risk. Selling pressure into the US close leaves market makers long; they are
  paid to hold it until new traders arrive overnight (p.2, p.14).
- Asymmetry: "market selloffs generate robust positive overnight reversals", while reversals after rallies are
  "much more modest" (abstract).
- Order imbalance = relative signed volume over 15:15 - 16:15 ET (p.3).
- Near-zero imbalances give reversals indistinguishable from zero (p.20).
- Trading strategies (2004-2020, Table IX):
  - long 2:00 - 3:00 has a Sharpe ratio of 1.1 before costs and -0.5 after the bid-ask spread;
  - "buy the dip" (hold 1:30 - 3:30 only after a negative closing imbalance) has 1.8 before costs and 1.1 after
    (p.28).
- The paper covers ES only. It says nothing about Nasdaq-100 futures.

## Card

- **Mechanism.** End-of-day order imbalances in US equity-index futures are absorbed by risk-averse market makers.
  After a selloff into the close they hold long inventory and need a premium to carry it. They unload it when new
  participants arrive at the European open, so prices revert upward overnight. After a rally into the close they are
  short and the mirror reversal (down) is predicted, but the source finds it much weaker.
- **Who loses and why.** The close-hour sellers (hedgers, rebalancers) who demand immediacy at the US close: they
  sell below fundamental value because they must trade then, not because of information.
- **Price-only adaptation (limitation).**
  - We hold quotes, not signed trades, so the closing imbalance is proxied by the close-hour return
    x = log(close of the last bar at or before 16:15 ET / close of the last bar at or before 15:15 ET), on the
    previous weekday.
  - Signed volume and contemporaneous returns move together (price impact), but the paper does not test a return-
    based sort.
  - Our NASDAQ series is a CFD that tracks the futures (mid parity verified 2026-10-16).
- **Universe (rule C, pre-registered):** NASDAQ and US500, the US equity-index futures where the mechanism was
  documented. The other 8 assets are computed and reported, not pooled.
- **Trigger.** Every New York weekday d:
  - entry bar = the first bar closing at or after 02:00 ET on d, at most 30 minutes after (causal);
  - x from the previous weekday's 15:15 and 16:15 ET bars (each the last bar at or before, at most 30 minutes
    stale; both are in the past at entry);
  - x < 0: LONG. x > 0: SHORT. No magnitude filter (|x| becomes a feature).
- **Expected by the source.** LONG passes. SHORT is predicted to be weak (the paper's asymmetry); it is tested
  because the coverage rule requires both sides.
- **Regimes.**
  - Should work: large close-hour selloffs; high-volatility periods (paper: stronger with a high VIX).
  - Should fail: near-zero close-hour moves; the US midday.
- **Counterfactuals (gating, one clause inverted at a time).**
  - **C1 sign:** the same side on days whose close-hour return has the opposite sign (LONG after a close-hour rally).
    Prediction: edge > inverted.
  - **C2 timing:** the same rule with the entry at 12:00 ET on d (the US midday, which the paper finds flat, after the
    European arrivals). Prediction: edge > C2.
- **Diagnostics (reported, not gating).**
  - C3: top vs bottom half of |x|.
  - C4: the same sort on the full previous-session return (16:15 to 16:15).
  - The hour-matched drift comparison against random bars in the same clock hour (tests the conditioning beyond the
    unconditional 2:00 drift).
- **Falsifiers.**
  - F1: the close-hour sign does not matter (C1 fails).
  - F2: midday works as well (C2 fails).

## Gate 0 (pre-registered)

- **Cell:** H1_V1 -- 1 h lookback, EWM 100, 1 h vertical, 1:1 (D083). The paper's window is the hour 2:00 - 3:00.
- **Method:**
  - shared/research/gate0/gate0_tool.py T3: the same labeler (both-touch = stop-loss first), the same month-matched
    drift baseline (10,000 draws, seed 20261102) and the same joint week-clustered SE for the counterfactuals as T1.
  - Pooled over the universe; one-sided 95 %.
  - The tiered drift bar (90 % when every counterfactual passes at 95 %) applies.
- **Verdict per side for NASDAQ:** PASS if the pooled drift baseline and C1 and C2 pass and NASDAQ's own diffs are
  all > 0. A FAIL kills T3 for that side as written (no re-tuning of the times, the window, the proxy or the cell).

## RESULT (2026-11-05, gate0_T3.json) -- FAIL, both sides

Pooled NASDAQ + US500, one-sided 95 % (lower bound in brackets); the tiered bar does not apply (counterfactuals fail):

| side | events | drift diff | C1 sign | C2 timing |
|---|---|---|---|---|
| LONG | 797 | +0.013 (-0.047) | +0.009 (-0.049) | +0.033 (-0.034) |
| SHORT | 850 | +0.004 (-0.054) | +0.009 (-0.049) | -0.003 (-0.065) |

- NASDAQ LONG (the side the source predicts): 410 events, mean -0.009 R; drift -0.016, hour-matched -0.015,
  C1 -0.021, C2 -0.020. No sign of a reversal after close-hour selloffs.
- US500 LONG is positive (mean +0.052 R, drift +0.043, C1 +0.040, C2 +0.087) but not significant; NASDAQ's diffs are
  all negative.
- HK has no events (no bars at the New York clock times); it is outside the universe.
- Tool fixes before the counted run (no number changed): empty-asset columns and the no-event skip.

T3 is killed as written for both sides (no re-tuning of the window, the proxy, the cell or the universe).
