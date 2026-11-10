# T1 -- Session-close hedging flow (intraday momentum into the close): thesis card + Gate 0 pre-registration

Written 2026-11-04, before any data on this thesis was looked at (owner: "proceed"; NASDAQ first). Campaign plan:
docs/mldp/campaign/NASDAQ_2026-11/CAMPAIGN.md. Rule: THEORY CHECK D075 (docs/mldp/rules/THEORY_CHECK_RULE_D075.md);
Gate 0 cell H1_V1 approved by the owner for intraday theses (register D083).

## Card

- **Mechanism.** Option market makers and leveraged-ETF sponsors who are short gamma must trade in the direction of
  the day's price move to stay hedged, and they concentrate that hedging near the close. Their forced,
  non-informational trades push the price further in the day's direction into the close. The return in the last part of
  the session is therefore predicted by the return over the rest of the day.
- **Who loses and why.** The hedgers: they buy after rises and sell after falls because their book requires it, not
  because of information, and they pay the price impact.
- **Source (read 2026-11-04).** Baltussen, Da, Lammers & Martens (2021), JFE 142:377-403.
  - Abstract: "The return during the last 30 minutes before the market close is positively predicted by the return
    during the rest of the day (from previous market close to the last 30 minutes)"; the effect "reverts over the next
    days" and is linked to "gamma hedging demand".
  - NASDAQ-100 futures (NQ): coefficient 6.36, t 7.97, R2 4.10 %, out-of-sample R2 3.76 % (sample 1996-2020).
  - Also Gao, Han, Li & Zhou (2018), JFE 129:394-414 (S&P 500 ETF; first half-hour predicts the last half-hour).
- **Trigger (both sides).** Every trading day, at the asset's reference close time minus 60 minutes, let r = the log
  return from the previous day's reference close to now.
  - r > 0: LONG.
  - r < 0: SHORT.
  - The trade holds 1 hour, into the reference close. Recall-first: every day trades, with no magnitude filter; |r|
    becomes a feature.
- **Reference close per asset.** These are the times when hedgers and benchmarked flows trade; the mechanism's analogue
  on each asset:

  | Asset | Reference close (local time) |
  |---|---|
  | NASDAQ, US500 | NY cash close 16:00 America/New_York |
  | DAX | Xetra close 17:30 Europe/Berlin |
  | JPN | Tokyo close 15:00 Asia/Tokyo until 2024-11-04, 15:30 from 2024-11-05 (TSE session extension) |
  | HK | HKEX close 16:00 Asia/Hong_Kong |
  | XAUUSD | LBMA gold PM auction 15:00 Europe/London |
  | GBPJPY, USDMXN | WM/Reuters 4pm fix, 16:00 Europe/London |
  | USOIL | NYMEX settlement 14:30 America/New_York |
  | BTCUSD | CME CF Bitcoin Reference Rate, 16:00 Europe/London |
- **Regimes.**
  - Should work: high-volatility days; options-expiry days (third Friday, and daily 0DTE since 2022); large |r|.
  - Should fail: quiet days with small |r|; assets without a large short-gamma options or leveraged-ETF market.
- **Counterfactuals (gating; one clause inverted at a time).**
  - **C1 sign:** the direction of the day's move is the clause.
    - Inverted: the same side traded on days whose move had the opposite sign (LONG on down days, SHORT on up days).
    - Prediction: the edge earns more than the inverted set. If the sign doesn't matter, it is drift.
  - **C2 timing:** the end-of-day concentration is the clause.
    - Inverted: the same sign rule and 1-hour hold, at reference close minus 6 hours, with r measured from the previous
      reference close to that time.
    - Prediction: the edge earns more than the mid-session version.
- **Diagnostic (reported, not gating).** C3 magnitude: within the edge's days, the top half of |r| against the bottom
  half. Prediction: top > bottom.
- **Falsifiers.**
  - F1: no difference between up-day and down-day trades (C1 fails).
  - F2: mid-session works as well as the close (C2 fails).

## Gate 0 (pre-registered)

- **Cell:** H1_V1 -- Event_Vol lookback 1 h, EWM span 100 bars, vertical 1 h, barriers 1:1. Gross mid R, no costs.
  This is the thesis horizon (owner D083).
- **Bars and events:** the research standard_quote_tick bars. The entry bar is the last bar closing at or before the
  entry time, no more than 30 minutes earlier; the previous-reference bar follows the same rule. A day without both
  bars is skipped.
- **Labeler and tool:** shared triple-barrier labeling on bar OHLC. A bar that touches both barriers resolves stop-loss
  first (conservative; counted and reported). Tool: shared/research/gate0/gate0_tool.py.
- **Drift baseline:** each side's events against 20,000 random bars of the same side in the same period. Primary
  comparison: month-matched, 10,000 draws, seed 20261102. diff = edge mean R - month-matched mean R, SE =
  sqrt(SE_edge^2 + SE_draws^2), SE_edge week-clustered. **PASS = diff - 1.645 SE > 0.** All-months and hour-matched
  comparisons are reported as diagnostics.
- **Counterfactuals:** diff = edge mean R - inverted mean R, one week-clustered SE over both sets. **PASS = diff -
  1.645 SE > 0.** INCONCLUSIVE below 100 inverted events.
- **Pooled (D075 C):** per side, all 10 assets together. Each event's month-matched comparison is drawn from its own
  asset; weeks are shared across assets. **PASS = pooled diff - 1.645 SE > 0** for the drift baseline, C1 and C2.
  Per-asset results are always reported.
- **Verdict per side for NASDAQ:** PASS if the pooled drift baseline, C1 and C2 all pass, and NASDAQ's own diffs are
  > 0 for all three. Then NASDAQ moves to Gate A for that side.
  - A FAIL kills T1 as written, with no re-tuning of the reference times, the entry offset or the cell (MLAM p.8).
  - Every run is logged; this is a theory test, not a strategy trial.

## RESULT (2026-11-04, gate0_T1.json, run 9.8 s) -- FAIL as pre-registered (both sides)

Pooled over 10 assets (one-sided 95 %, lower bound in brackets):

| side | events | drift diff | C1 sign diff | C2 timing diff |
|---|---|---|---|---|
| LONG | 5,426 | +0.013 (-0.011) | +0.025 (-0.001) | +0.003 (-0.020) |
| SHORT | 5,015 | +0.004 (-0.022) | +0.025 (-0.001) | +0.006 (-0.021) |

NASDAQ alone:

| side | events | drift diff | C1 diff | C2 diff |
|---|---|---|---|---|
| LONG | 558 | +0.028 (-0.046) | +0.094 (+0.013, passes) | +0.069 (-0.013) |
| SHORT | 451 | +0.061 (-0.023) | +0.097 (+0.016, passes) | +0.114 (+0.025, passes) |

US500: C1 and C2 pass on both sides (+0.13-0.17 R); drift +0.06 on each side, lower bounds -0.011 / -0.024.

What it shows:
- The mechanism shows on the US equity indices: the sign of the day's move matters, and end of day beats mid-session.
- No single asset clears the drift baseline at about 500 events per side.
- The pooled test fails because assets the card listed under "should fail" (no large short-gamma market) drag it
  down: HK, JPN, XAUUSD, USOIL negative.

Verdict by the card's rule: T1 is killed as written. No re-tuning (MLAM p.8). One Gate 0 attempt, counted.
