# T5 -- Vol-target rebalancing flow into the close: thesis card + Gate 0 pre-registration

Written 2026-11-05, before any data on this thesis was looked at. Owner 2026-11-05: generate our own theses rather
than published anomalies ("think deep and generate your own theses"); "start with #1, vol-target deleveraging, and
keep going". Rules: D075 (rule C universe, tiered drift bar), D083 (H1_V1). Causal entry from the start.

Overlap check (2026-11-05): no vol-target / risk-control / deleveraging thesis exists in any ledger or in
config/cross_asset_thesis_coverage.json. The killed NASDAQ volatilityshockcontinuation (2026-10-05) traded a
single-bar range outlier for hours; T5 trades a multi-day, rule-based exposure change executed into the close.

## Card

- **Mechanism (ours).** Volatility-targeting money sizes equity exposure as target / trailing realised volatility
  and rebalances mechanically near the US close, using volatility measured up to the previous day. This includes
  risk-control indices behind annuities and structured notes, vol-control funds and risk-parity overlays. The
  exposure change it must trade at the next close is therefore known from past prices:
  - realised volatility rose -> exposure must fall -> forced selling into the next close;
  - realised volatility fell -> exposure must rise -> forced buying.
  The trades are sized by a formula, not by information, and are concentrated in the closing auction (MOC orders)
  and the last hour.
- **Who loses and why.** The vol-target funds: they sell after volatility rises and buy after it falls, on a
  schedule anyone can compute, and pay the price impact.
- **Signal (price-only, causal).**
  - Daily closes: the last bar at or before 16:00 New York on each weekday (at most 30 minutes stale).
  - sigma_t = standard deviation of the last 20 daily close-to-close log returns (consecutive trading days), at the
    close of day t.
  - For trading day D: g = sigma(D-2) / sigma(D-1) - 1, the relative exposure change the funds trade at D's close
    (index-style one-day implementation lag; every input is a past close).
- **Trigger.** On each weekday D, entry = the first bar closing at or after 15:00 New York (at most 30 minutes
  after; causal).
  - g < 0 (volatility rose): SHORT.
  - g > 0 (volatility fell): LONG.
  - Recall-first: no magnitude filter; |g| is a feature.
- **Universe (rule C, pre-registered):** NASDAQ and US500. US large-cap index exposure is where vol-target money is
  concentrated (S&P 500 risk-control indices, Nasdaq-100 volatility-control indices). The other assets are computed
  and reported only.
- **Regimes.**
  - Should work: days with a large |g| (volatility regime changes); high-volatility periods (larger exposure
    changes).
  - Should fail: midday (the flow executes at the close); |g| near zero.
- **Counterfactuals (gating, one clause inverted at a time).**
  - **C1 sign:** the same side on days with the opposite sign of g (SHORT when volatility fell). Prediction:
    edge > inverted.
  - **C2 timing:** the same rule with the entry at 12:00 New York (1 h hold, no closing-auction flow). Prediction:
    edge > C2.
- **Diagnostics (reported, not gating).**
  - C3 magnitude: top vs bottom half of |g|.
  - T1 overlap control: SHORT and LONG events split by the sign of the day's move to 15:00, because volatility tends
    to rise on down days and T1 / T1bc (the day-move sign) already failed.
- **Falsifiers.**
  - F1: the sign of g does not matter (C1 fails).
  - F2: midday works as well (C2 fails).
  - F3: the effect exists only through the day-move sign (diagnostic).

## Gate 0 (pre-registered)

- **Cell:** H1_V1 (1 h lookback, EWM 100, 1 h vertical, 1:1; D083). The flow executes in the last hour.
- **Method:**
  - shared/research/gate0/gate0_tool.py T5: the T1 labeler, the month-matched drift baseline (10,000 draws, seed
    20261102) and joint week-clustered SE.
  - One-sided 95 %, pooled over the universe.
  - Tiered drift bar: 90 % if C1 and C2 both pass at 95 %.
- **Verdict per side for NASDAQ:** PASS if the pooled drift baseline, C1 and C2 pass and NASDAQ's own diffs are > 0.
  A FAIL kills T5 for that side as written (no re-tuning of the window, the lag, the entry time, the cell or the
  universe).

## RESULT (2026-11-05, gate0_T5.json) -- FAIL, both sides

Tool fix before the counted run (no number changed): an empty-closes return for HK (no bars at the New York close).

Pooled NASDAQ + US500, one-sided 95 % (lower bound in brackets):

| side | events | drift diff | C1 sign | C2 timing |
|---|---|---|---|---|
| LONG | 1,034 | +0.009 (-0.052) | +0.015 (-0.053) | +0.030 (-0.042) |
| SHORT | 1,036 | -0.002 (-0.067) | +0.015 (-0.053) | -0.008 (-0.077) |

- NASDAQ LONG: 517 events, mean +0.002 R, drift -0.012, C1 -0.015.
- NASDAQ SHORT: 514 events, mean -0.025 R, drift -0.018, C1 -0.019.
- US500 LONG is mildly positive (+0.042 R mean) but not significant.
- g is uncorrelated with the same-day move (-0.005), so this is not T1 again.

T5 is killed as written for both sides.
