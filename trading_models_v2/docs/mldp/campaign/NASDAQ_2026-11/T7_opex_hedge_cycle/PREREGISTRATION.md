# T7 -- Monthly options-expiry hedge cycle: thesis card + Gate 0 pre-registration

Written 2026-11-05, before any data on this thesis was looked at. Owner: generate our own theses; keep going on NASDAQ.
Rules: D075 (rule C universe, tiered drift bar). Calendar input only: the third Friday of each month, the D084
options-expiry calendar feature the owner approved on 2026-11-04. Daily entries use the T4 rule (the first bar
closing at or after the reference close - 30 min, accepted by + 30 min).

**Overlap check (2026-11-05).** None of the 57 thesis families in the NASDAQ ledger or the cross-asset coverage ledger
uses option expiry. **Novelty, honestly stated:** "OPEX-week" hedging flows are discussed by practitioners. We know of
no peer-reviewed trading rule; this formalisation (the windows, the sides and the counterfactuals) is ours.

## Card

- **Mechanism.** Hedgers are net long index puts; dealers are net short them and hold short futures as the delta
  hedge.
  - Into the monthly expiry, out-of-the-money puts lose delta as time runs out, so dealers buy back their short
    hedges: buying pressure in the expiry week.
  - After expiry, hedgers buy next month's protection; dealers sell it and short futures again: selling pressure in
    the week after expiry.
  - Both flows follow the option calendar and hedge ratios, not information.
- **Who loses and why.**
  - In the expiry week, sellers who face the dealers' mechanical buy-back.
  - After expiry, the protection buyers' re-hedging pushes prices down; the dealers hedge into them. The losers are
    longs who buy into that selling.
- **Calendar.**
  - OPEX = the last trading day at or before the third Friday of the month (for example, Thursday when that Friday is
    a market holiday).
  - Trading days and entries as T4: the event for day D enters at the causal bar near the previous trading day's
    16:00 New York close and covers day D (24 h vertical).
- **Trigger.**
  - LONG when D is in the expiry week: the 5 trading days ending at OPEX (k_next in [-4, 0]).
  - SHORT when D is in the 5 trading days after OPEX (k_prev in [1, 5]).
- **Universe (rule C):** NASDAQ and US500 (US index options: NDX / QQQ / SPX / SPY monthly expiries). The others are
  reported only.
- **Regimes.**
  - Should work: quarterly expiries (March, June, September, December; larger open interest); high put demand (after
    selloffs).
  - Should fail: mid-cycle weeks.
- **Counterfactuals (gating).**
  - **C1 calendar placebo:** the same side on days outside both windows (OPEX-4 .. OPEX+5). Prediction:
    edge > placebo.
  - **C2 phase swap:** LONG in the post-expiry week (and SHORT in the expiry week). Prediction: LONG pre > LONG post;
    SHORT post > SHORT pre.
- **Diagnostics (reported).** Quarterly vs other expiries; the 2022+ 0DTE era is the whole sample (noted).
- **Falsifiers.**
  - F1: the expiry windows earn the same as other days (C1 fails).
  - F2: the phase does not matter (C2 fails).

## Gate 0 (pre-registered)

- **Cell:** H4_V24 (one-day holds; the D075 default).
- **Method:** gate0_tool.py T7: the same labeler, the month-matched drift baseline and joint week-clustered SE;
  one-sided 95 %; pooled over the universe; tiered drift bar.
- **Verdict per side for NASDAQ:** PASS if the pooled drift baseline, C1 and C2 pass and NASDAQ's own diffs are > 0.
  A FAIL kills T7 for that side as written.

## RESULT (2026-11-05, gate0_T7.json) -- FAIL, both sides

Pooled NASDAQ + US500, one-sided 95 % (lower bound in brackets):

| side | events | drift diff | C1 placebo | C2 phase swap |
|---|---|---|---|---|
| LONG | 525 | -0.009 (-0.119) | +0.008 (-0.098) | -0.062 (-0.185) |
| SHORT | 516 | -0.054 (-0.166) | -0.066 (-0.172) | -0.058 (-0.181) |

- NASDAQ LONG (expiry week): 255 events, mean +0.017 R; C2 -0.078 (post-expiry LONG did better).
- NASDAQ SHORT (post-expiry week): 251 events, mean -0.095 R. The predicted selling did not happen; the week after
  expiry leaned up.
- The reverse pattern is NOT adopted. Choosing it after seeing this result would be the backtest-tweak-backtest cycle
  (MLAM p.8); any later thesis on it would have to be a separate card with its own mechanism and be logged as
  informed by T7.

T7 is killed as written for both sides.
