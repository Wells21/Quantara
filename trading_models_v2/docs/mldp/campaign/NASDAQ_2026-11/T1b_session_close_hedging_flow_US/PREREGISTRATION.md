# T1b -- Session-close hedging flow, US equity-index universe (POST-HOC re-test, owner-ordered)

Written 2026-11-04, after T1's Gate 0 result (T1 failed; ../T1_session_close_hedging_flow/PREREGISTRATION.md).

**Status of this test.**
- Owner, 2026-11-04: "retest just this one please" -- a one-off exception to "T1 stays killed".
- It is POST-HOC: the universe is chosen after seeing T1's per-asset results. That is the backtest-tweak-backtest
  risk MLAM p.8 names, so it is recorded as such.
- It is a separate trial (T1 and T1b each count) and is read with that in mind.

**Card.** Identical to T1: same mechanism, sources, trigger, both sides, reference close, counterfactuals C1 (sign)
and C2 (timing), diagnostic C3, falsifiers, Gate 0 cell H1_V1, labeler, drift baseline and one-sided 95 % standard.
One change:

- **Universe (D075 rule C as amended 2026-11-04):** NASDAQ and US500. These are the US equity indices with the large
  same-day options and leveraged-ETF markets where T1's card placed the short-gamma hedging. The pooled tests use these
  two assets only; all 10 assets are still computed and reported (coverage rule).

**Verdict per side for NASDAQ:** PASS if the pooled (NASDAQ + US500) drift baseline, C1 and C2 all pass, and NASDAQ's
own diffs are > 0 for all three. A FAIL kills the thesis with no further re-test.

## RESULT (2026-11-04, gate0_T1b.json) -- FAIL (both sides), on the drift baseline only

Pooled NASDAQ + US500 (one-sided 95 %, lower bound in brackets):

| side | events | drift diff | C1 sign | C2 timing |
|---|---|---|---|---|
| LONG | 1,118 | +0.046 (-0.014) | +0.116 (+0.045) PASS | +0.099 (+0.032) PASS |
| SHORT | 920 | +0.062 (-0.008) | +0.118 (+0.047) PASS | +0.141 (+0.060) PASS |

What it shows:
- The mechanism's counterfactuals pass clearly on both sides: the sign of the day's move matters, and the last hour
  beats mid-session.
- The trade beats same-month random same-side entries by 0.05-0.06 R, but not with 95 % one-sided confidence. With
  about 4 years of data (about 1,000 events per side over the two assets) the test is underpowered for an effect this
  size.

By the rule, T1 / T1b are killed; no further re-test (owner: "retest just this one").

## Re-verdict under the tiered drift bar (2026-11-04) -- SHORT PASSES Gate 0, LONG FAILS

Owner 2026-11-04 adopted the tiered drift bar for future cards: one-sided 90 % (z 1.28) when every counterfactual
passes at one-sided 95 %. The owner then applied it to T1b: "apply the new rule to it". That is a logged POST-HOC
EXCEPTION. T1b's pooled C1 and C2 pass at 95 % on both sides, so its drift bar becomes 90 %. The numbers are unchanged
(same seeds; the earlier file is kept as gate0_T1b_standard_bar.json).

| side | drift diff | SE | lower bound 95 % | lower bound 90 % | verdict |
|---|---|---|---|---|---|
| LONG | +0.0455 | 0.0365 | -0.0145 | **-0.0012** | FAIL |
| SHORT | +0.0617 | 0.0427 | -0.0084 | **+0.0071** | **PASS** |

- NASDAQ SHORT's own diffs are all > 0: drift +0.061, C1 +0.097, C2 +0.114.
- **NASDAQ SHORT proceeds to Gate A.** NASDAQ LONG is killed.
- Correction: the owner was told the LONG 90 % bound was about -0.0004; the exact value is -0.0012.
- Read every later result for this edge knowing it entered on a post-hoc universe choice and a post-hoc bar. Both count
  as trials (T1, T1b).

## Edge build: NASDAQ/closehedgingflow_short -- Gate A-C selection rule (written 2026-11-04, before any Gate A result)

Owner 2026-11-04: "restrict the edge to the 1 hour vertical and also search across the full 405 cells"; row floor
removed (D085). Card: assets/NASDAQ/edges/closehedgingflow_short/edge.yaml (thesis, provenance pre_registered).

- **Trigger (causal).** The first completed bar closing at or after 15:00 New York (at most 30 minutes after);
  r = log(its close / the last bar at or before 16:00 New York on the previous weekday, at most 30 minutes stale);
  r < 0: SHORT. One event per day, no magnitude filter. The difference from Gate 0 (which used the last bar at or before
  15:00) is disclosed; median entry lag 6.7 minutes.
- **Search and pay.** Gate A on the runbook's 405 cells at the Moneta cost-model mean spread (0.2616 bps); Gate C
  replays every cell (gate_c_runner --all-cells, broker cost model, 5700 ms); PBO / CSCV over the 405 + control;
  Level-1 DSR / FWER over every Gate C trial.
- **Lock.** The winner is the argmax compound velocity among the 1 h vertical cells (45 of 405) that are Gate B
  positive with exec_mean_R > 0 and coverage >= 99 %. The unrestricted best cell is reported as a diagnostic and is
  never locked. None eligible = KILL.
- **Then** stress (D079), the post-Gate C drift re-check at the locked cell (tiered bar: one-sided 90 %, since every
  counterfactual passed at 95 %), 4-place lock, D084 mechanism features, export, Task 2A, Steps 01-09, CV leakage,
  Gate D v4.1, DSR / FWER.
- **Structural ceiling:** 451 events (Gate 0 count) on ~4 years of data.

## Gate A result (2026-11-04, grid stamp 20261104T160243Z, 405 cells, cost 0.2616 bps)

- Best cell overall: H6_E100_V16 1:1, gross +0.063 R, 411 rows -> Gate A PASS on the grid (kill line +0.05).
  146 of 405 cells net > 0.
- Lock-eligible 1 h vertical cells (45): best gross +0.006 R (H6_E100_V1 1:1); 2 of 45 net > 0 (+0.0014, +0.0001).
  Gate 0's own cell H1_E100_V1 1:1: -0.013 R.
- **Diagnostic: the Gate 0 pass depends on the non-causal entry.** Same labeler (gate0_tool Labeler, H1_V1), NASDAQ
  shorts:
  - Gate 0 entry (last bar at or before 15:00, ~14:52): 451 events, mean +0.050 R.
  - Causal entry (first bar at or after 15:00, ~15:07): 462 events, mean -0.001 R.
  - In 432 of 462 days the causal entry is exactly the next bar after Gate 0's entry. One bar (~15 minutes) separates
    them. Each mean has SE about 0.045 R, so the two are within about one SE of each other.
- Gate C replays all 405 cells next (runbook); the decision on what this means for the T1b Gate 0 pass is the owner's.
