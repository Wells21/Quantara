# T6 -- Dealer gamma regime: fade or follow the last hour: thesis card + Gate 0 pre-registration

Written 2026-11-05, before any data on this thesis was looked at. Our own thesis (owner 2026-11-05: "keep going till
you finally gotten a long edge or a short edge"). Rules: D075 (rule C universe, tiered drift bar), D083 (H1_V1).
Causal entry.

**Design note (power).** T1-T5 had about 500 events per side; the detectable effect was about 0.075 R. T6 fires
hourly through the US session (about 5 events a day, about 2,500 per side), with non-overlapping 1 h holds.

## Card

- **Mechanism (ours).** Options dealers delta-hedge mechanically.
  - When they are net long gamma, hedging sells rallies and buys dips: intraday moves mean-revert (pinning).
  - When they are net short gamma, hedging buys rallies and sells dips: moves extend.
  - Positioning changes slowly (open interest, expiries), so the regime of the last few sessions is a usable,
    price-observable proxy for today's. Recent intraday return autocorrelation is negative in a pinned regime and
    positive in an amplifying one.
- **Who loses and why.** The trader on the other side of the dealers' forced hedge flow. In a pinned market,
  momentum traders who chase moves into dealer selling or buying; in an amplifying market, mean-reversion traders
  who fade moves that dealer hedging extends.
- **Novelty.** Practitioner gamma-exposure estimates use options open-interest data, which we do not hold. The
  price-only regime proxy and this hourly fade/follow rule are ours; no ledger entry covers gamma, autocorrelation or
  pinning (checked 2026-11-05) except
  US500/autocorrelationreversal_long (2026-09-14, killed at the old Gate D monotonicity check). That edge faded large
  single bars when the 50-bar lag-1 autocorrelation was <= -0.08, and overlaps only T6's PIN half. T6 differs in:
  - the regime measure (5-session, 30-minute clock returns);
  - the asset (NASDAQ);
  - the event clock (hourly);
  - the TREND / follow half.
- **Regime (causal; known before the session).**
  - Half-hourly New York clock prices 10:00, 10:30, ..., 16:00 on each of the previous 5 trading days (the last bar
    at or before each mark, at most 30 minutes stale).
  - rho = the first-order autocorrelation of the pooled consecutive 30-minute log returns within those sessions
    (about 60 returns; no overnight returns).
  - rho < 0: PIN (fade). rho > 0: TREND (follow).
- **Trigger.**
  - At each hour mark h in {11:00, 12:00, 13:00, 14:00, 15:00} New York, entry = the first bar closing at or after
    h (at most 30 minutes after).
  - m = log(entry close / the last bar at or before h - 60 min).
  - PIN: side = -sign(m). TREND: side = +sign(m).
  - Recall-first: no thresholds on |m| or |rho|.
- **Universe (rule C):** NASDAQ and US500 (US index options dealers: NDX / QQQ / SPX / SPY). The others are reported
  only.
- **Regimes.**
  - Should work: large |m|; strong |rho|; expiry-heavy periods.
  - Should fail: |rho| near 0.
- **Counterfactuals (gating).**
  - **C1 regime inversion:** the same side on events where the regime rule gives the opposite side, i.e. the regime
    label flipped (LONG on PIN bars after a rise and on TREND bars after a fall). Prediction: edge > inverted.
  - **C2 stale regime:** the same rule with rho measured on the 5 sessions ending 20 trading days earlier.
    Prediction: edge > stale (current positioning matters).
- **Diagnostics (reported, not gating).** Top vs bottom half of |m|; PIN-only and TREND-only edge means; by hour.
- **Falsifiers.**
  - F1: the regime label does not matter (C1 fails).
  - F2: a month-old regime works as well (C2 fails).

## Gate 0 (pre-registered)

- **Cell:** H1_V1 (1 h lookback, EWM 100, 1 h vertical, 1:1; D083).
- **Method:** gate0_tool.py T6, with the T1 labeler, the month-matched drift baseline and joint week-clustered SE;
  one-sided 95 %; pooled over the universe; tiered drift bar.
- **Verdict per side for NASDAQ:** PASS if the pooled drift baseline, C1 and C2 pass and NASDAQ's own diffs are > 0.
  A FAIL kills T6 for that side as written.

## RESULT (2026-11-05, gate0_T6.json) -- FAIL, both sides (with ~4x the power of T1-T5)

Count check before the run (no labels): 5,135 NASDAQ events. The PIN share is 52 %. The current and 20-session-old
regimes agree on 49.5 % of events, so the proxy regime has no month-scale persistence.

Pooled NASDAQ + US500, one-sided 95 % (lower bound in brackets):

| side | events | drift diff (SE) | C1 regime flip | C2 stale regime |
|---|---|---|---|---|
| LONG | 4,853 | -0.007 (SE 0.018; -0.037) | -0.015 (-0.047) | +0.003 (-0.020) |
| SHORT | 4,936 | +0.001 (SE 0.017; -0.028) | -0.014 (-0.047) | +0.003 (-0.019) |

- NASDAQ LONG: 2,550 events, mean +0.019 R, drift +0.017, C1 -0.004, C2 +0.012.
- NASDAQ SHORT: 2,585 events, mean -0.025 R, drift -0.011.
- The regime label does not matter (C1 is negative on both sides). The price-autocorrelation proxy for dealer gamma
  does not predict the next hour.

T6 is killed as written for both sides.
