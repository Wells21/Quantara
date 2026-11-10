# T9 -- Release-reaction continuation (all assets): thesis card + Gate 0 pre-registration

Written 2026-11-05, before any data on this thesis was looked at. Our own thesis; D086 calendar (timing only).
Owner 2026-11-05: "keep going with new calendar theses and other price theses ... testing these across the 10
assets". Rules: D075 (rule C universe, tiered drift bar). Causal entries. T8's contaminated pre-release lead is NOT
used (owner: "drop it").

## Card

- **Mechanism (ours).** A scheduled US release (CPI, payrolls, FOMC) moves every macro-sensitive market within
  minutes, mostly through fast traders. The slower re-positioning follows over the next hours, all in the direction
  of the news:
  - vol-target and CTA-style rules re-weighting;
  - dealers re-hedging options whose delta the jump changed;
  - prop and risk-limited traders who were flat through the release re-entering with the move.
  So the first 30-minute reaction continues for the rest of the session.
- **Who loses.** Traders who fade the release move early (anchored to the pre-release price), providing liquidity to
  the slower directional flow.
- **Events.**
  - Every released calendar event inside the data (FOMC 14:00 ET; CPI / NFP 08:30 ET; one event when CPI and NFP
    share a bar).
  - Reaction m = log(the first bar closing at or after release + 30 min / the last bar at or before the release).
  - Entry on that bar (causal, at most 30 minutes after release + 30 min). m > 0: LONG; m < 0: SHORT.
- **Universe (rule C): all 10 assets.** US macro releases move indices, gold, FX, oil and bitcoin. Assets with no
  bars at those New York times have no events.
- **Cell (rule A):** H1_V4 (1 h lookback, EWM 100, 4 h vertical, 1:1); the thesis horizon is the rest of the
  session.
- **Counterfactuals (gating).**
  - **C1 sign:** the same side on events whose reaction had the opposite sign. Prediction: edge > inverted.
  - **C2 placebo:** the same rule at the same clock times (08:30 and 14:00 ET) on weekdays with no release.
    Prediction: edge > placebo.
- **Diagnostics.** By release type; |m| top vs bottom half.
- **Falsifiers.**
  - F1: the reaction sign does not matter.
  - F2: ordinary days at the same clock time behave the same.

## Gate 0 (pre-registered)

gate0_tool.py T9: the T1 labeler, the month-matched drift baseline, joint week-clustered SE, one-sided 95 %, pooled
over the universe, and the tiered drift bar. Verdict per side for every universe asset: PASS if the pooled tests pass
and that asset's own diffs are > 0. A FAIL kills T9 for that side as written.

## RESULT (2026-11-05, gate0_T9.json) -- FAIL, both sides, every asset

Pooled over all 10 assets, one-sided 95 % (lower bound in brackets):

| side | events | drift diff | C1 sign | C2 placebo |
|---|---|---|---|---|
| LONG | 665 | -0.091 (-0.198) | -0.197 (-0.324) | -0.065 (-0.157) |
| SHORT | 598 | -0.119 (-0.234) | -0.185 (-0.311) | -0.081 (-0.181) |

- The continuation claim is wrong. On 8 of 10 assets, trading WITH the 30-minute release reaction lost:
  - NASDAQ LONG -0.184 R (n 76), SHORT -0.309 R (n 55);
  - US500 -0.196 / -0.107;
  - USDMXN -0.236 / -0.196.
  GBPJPY (+0.137 / +0.140) and BTCUSD LONG (+0.072) were the exceptions.
- **Post-hoc observation, NOT adopted:** release reactions tended to REVERSE over the next 4 hours. Turning this into
  a thesis now would be chosen after seeing T9 (MLAM p.8). It is recorded as a contaminated lead for the owner.
- Checked: identical C1 diffs for LONG and SHORT are expected (with 1:1 barriers the two comparisons are mirror
  images); HK equalling JPN (-0.26519) is a coincidence of the per-asset means, not a bug.

T9 is killed as written for both sides.
