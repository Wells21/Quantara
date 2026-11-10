# T8 -- Scheduled-release de-risking (short) and re-risking (long): thesis card + Gate 0 pre-registration

Written 2026-11-05, before any data on this thesis was looked at. Our own thesis; first use of the economic event
calendar (D086: FOMC 14:00 ET, CPI / NFP 08:30 ET; timing only). Rules: D075 (rule C universe, tiered drift bar),
D083 (H1_V1). Causal entries (first bar closing at or after the entry time, at most 30 minutes after).

## Card

- **Mechanism (ours).** Many leveraged and risk-limited traders may not hold positions through a scheduled
  high-impact release:
  - prop-firm funded accounts (news-trading rules);
  - VaR-limited desks and short-gamma books that cut gross exposure before a known jump.
  - **Pre-release (SHORT side):** they flatten before the release. When the crowd is long (the market rose over the
    previous 5 sessions), flattening is selling, so price drifts down in the hours before. Non-informational: the
    sale is forced by the event clock.
  - **Post-release (LONG side):** once the release is out, uncertainty is resolved and the same traders and
    vol-target money re-add risk. That re-risking is buying (equity exposure is net long) in the hours after,
    whatever the sign of the number. The first reaction minutes are excluded: we cannot compete there.
- **Who loses.**
  - Pre-release: buyers who supply the de-risking sellers' liquidity just before the jump.
  - Post-release: sellers into the re-risking demand.
- **Events.** Every calendar release (FOMC, CPI, NFP) inside the data. Universe (rule C): NASDAQ and US500 (US index
  futures; US macro releases); the other assets are reported only.
- **SHORT trigger (pre-release de-risking).**
  - Entry at release - 90 min (CPI / NFP 07:00 ET; FOMC 12:30 ET); 1 h hold, so it ends 30 minutes before the
    release (cell H1_V1).
  - Only when the 5-session trend is up: log(the last bar at or before 16:00 ET on the previous trading day / the
    same 6 trading days earlier) > 0.
- **LONG trigger (post-release re-risking).** Entry at release + 30 min (CPI / NFP 09:00 ET; FOMC 14:30 ET); 1 h hold.
  Unconditional.
- **Cell (rule A, pre-registered):** H1_V1 for both sides. The short must end before the release; the long tests the
  first hour of re-risking. (A per-side H4_V4 long was considered and dropped before any data, so that C2 compares
  like with like.)
- **Counterfactuals (gating).**
  - SHORT
    - C1 crowd sign: the same short when the 5-session trend is down (the crowd is short, de-risking means buying).
      Prediction: edge > inverted.
    - C2 placebo: the same rule (entry 07:00 and 12:30 ET, trend up) on weekdays with no calendar release within
      the calendar day. Prediction: edge > placebo.
  - LONG
    - C1 placebo: the same entry times (09:00 and 14:30 ET) on weekdays with no release that day. Prediction:
      edge > placebo.
    - C2 before resolution: LONG at release - 90 min, 1 h hold (the short side's window). Prediction: post > pre.
- **Diagnostics (reported).** By release type (FOMC / CPI / NFP); |trend| top vs bottom half (short).
- **Power (stated up front).** About 134 releases per asset in the data; the short side needs an up trend (about
  half). Pooled sets are about 130-270 events, an SE of about 0.07-0.09 R, so only large effects can pass.
- **Falsifiers.**
  - F1: the crowd sign does not matter (SHORT C1).
  - F2: non-release days behave the same (C2 / C1 placebos).
  - F3: the post-release window is no better than the pre-release window.

## Gate 0 (pre-registered)

gate0_tool.py T8: the T1 labeler, the month-matched drift baseline, joint week-clustered SE, one-sided 95 %, pooled
over NASDAQ + US500, and the tiered drift bar. Verdict per side as T1. A FAIL kills that side as written.

## RESULT (2026-11-05, gate0_T8.json) -- FAIL, both sides

Count check before the run (no labels): NASDAQ had 120 pre-release entries (76 in an up trend), 131 post-release
entries and over 900 placebo days.

Pooled NASDAQ + US500, one-sided 95 % (lower bound in brackets):

| side | events | drift diff | C1 | C2 |
|---|---|---|---|---|
| LONG (post-release) | 269 | -0.008 (-0.149) | placebo +0.003 (-0.119), n 3,656 | before release -0.089 (-0.252), n 233 |
| SHORT (pre-release, up trend) | 143 | -0.163 (-0.318) | crowd sign -0.075, n 90: INCONCLUSIVE (<100) | placebo -0.186 (-0.310), n 1,934 |

- NASDAQ SHORT: 76 events, mean -0.254 R, drift -0.235. In up trends price ROSE in the 90 to 30 minutes before
  releases. The de-risking (selling) prediction is wrong in sign.
- NASDAQ LONG (post-release): 131 events, mean +0.046 R, not significant; C2 negative (the hour before the release
  did better than the hour after).
- **Post-hoc observation, not adopted.** A pre-release up-drift matches the published pre-announcement drift (Lucca &
  Moench 2015, pre-FOMC). Turning it into a thesis now would be chosen after seeing T8 (MLAM p.8): it is recorded as
  a contaminated lead for the owner to decide, not tested.

T8 is killed as written for both sides.
