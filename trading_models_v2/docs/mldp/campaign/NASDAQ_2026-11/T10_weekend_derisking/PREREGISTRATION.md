# T10 -- Friday weekend de-risking against the week's crowd (all assets): thesis card + Gate 0 pre-registration

Written 2026-11-05, before any data on this thesis was looked at. Our own thesis, price and clock only. Rules: D075
(rule C universe, tiered drift bar), D083 (H1_V1). Causal entries.

## Card

- **Mechanism (ours).** Leveraged short-horizon traders must reduce positions before the weekend gap:
  - prop-firm accounts with no-weekend-holding rules;
  - retail CFD accounts facing weekend margin increases;
  - intraday desks with gap-risk limits.
  They hold the week's crowded position, so if the week rose the crowd is long and the Friday flattening is selling;
  if the week fell, it is short covering. The flow is forced by the calendar, not by information.
- **Who loses.** The flatteners, who pay the price impact of trading on a deadline.
- **Trigger.**
  - Each Friday, entry = the first bar closing at or after 15:00 New York (at most 30 minutes after; causal). This
    is before the 17:00 ET weekly close of CME and FX; the 1 h hold ends about 16:00 ET.
  - Week trend w = log(the last bar at or before 16:00 ET on Thursday / the same on the previous Friday).
  - w > 0: SHORT. w < 0: LONG.
- **Universe (rule C):** NASDAQ, US500, DAX, JPN, HK, XAUUSD, GBPJPY, USDMXN, USOIL -- every asset whose market
  closes for the weekend. BTCUSD trades through the weekend, so it is reported only. Assets with no bars at 15:00 ET
  have no events.
- **Cell (rule A):** H1_V1.
- **Counterfactuals (gating).**
  - **C1 crowd sign:** the same side when the week trend has the opposite sign. Prediction: edge > inverted.
  - **C2 no weekend:** the same rule on Wednesdays (entry 15:00 ET; trend = Tuesday close / the previous
    Wednesday's close). Prediction: edge > Wednesday.
- **Diagnostics.** |w| top vs bottom half; month-end Fridays vs others.
- **Falsifiers.**
  - F1: the crowd sign does not matter.
  - F2: Wednesdays behave the same.

## Gate 0 (pre-registered)

As T9 (cell H1_V1): verdict per side for every universe asset.

## RESULT (2026-11-05, gate0_T10.json) -- FAIL, both sides, every asset

Tool fix before the counted result: HK (no bars at 15:00 New York) is recorded as NO_EVENTS instead of stopping the
run; the card allows assets without events.

Pooled over 8 assets with events (BTCUSD reported only), one-sided 95 % (lower bound in brackets):

| side | events | drift diff | C1 crowd sign | C2 Wednesday |
|---|---|---|---|---|
| LONG | 581 | +0.019 (-0.067) | -0.024 (-0.106) | -0.018 (-0.118) |
| SHORT | 675 | -0.025 (-0.099) | -0.026 (-0.108) | -0.050 (-0.138) |

- NASDAQ LONG (Friday after a down week): 87 events, mean +0.074 R, drift +0.069, C1 +0.038, C2 +0.027. Every own
  diff is positive, but the pooled tests fail.
- NASDAQ SHORT: -0.055 R.
- Per-asset verdicts: FAIL everywhere (HK NO_EVENTS).

T10 is killed as written for both sides.
