# T13: round-level stop-cascade continuation (OUR thesis; pre-registered 2026-11-07, before any run)

## Mechanism

Traders put stop-loss orders just beyond round price levels, and take-profit orders just in front of them. Osler
documents this in currency order books: C. Osler, "Currency orders and exchange-rate dynamics: an explanation for the
predictive success of technical analysis", Journal of Finance 58(5), 2003; and "Stop-loss orders and price cascades
in currency markets", Journal of International Money and Finance 24, 2005. I have not checked page numbers. The level
attracts both the stops and the attention: index traders quote "NASDAQ 20,000", gold traders quote "2,000".

The longer a round level stays untouched, the more stops pile up beyond it. When price finally breaks through:
- the stops fire as market orders in the break direction (buy stops above the level on an up-break);
- that pushes price further: a cascade.

Prediction: after the FIRST close beyond a round level that has not traded for 24 h, price continues in the break
direction over the next hour.

The mechanism is specific to ROUND levels. A plain 24 h breakout through an arbitrary price has the same momentum
content but not the stop cluster. If continuation is equal at arbitrary levels, the effect is generic breakout
momentum, not this thesis. That is the main counterfactual (C1).

This is not microstructure in the D-rule sense. It needs only bar closes, highs and lows. A round level is the same
price on Dukascopy and on the broker (mid parity, 2026-10-16).

## Rule (live-tradable, bars only)

Universe (pooled, D075 rule C): all 10 assets, both sides. The round spacing s per asset is the conventional figure
traders watch, fixed before any run (absolute price, not scaled):

| asset | s | asset | s |
|---|---|---|---|
| NASDAQ | 100 | XAUUSD | 10 |
| US500 | 50 | GBPJPY | 1.00 |
| DAX | 100 | USDMXN | 0.10 |
| JPN | 500 | USOIL | 1.00 |
| HK | 500 | BTCUSD | 1000 |

For each bar j (decided at its close; entry = bar j's close, as in every Gate 0 card):
- H24 / L24 = the highest high / lowest low of the bars that close in [t_j - 24 h, t_j), i.e. strictly before bar j;
- the bar must follow its predecessor within 2 h (the break happened in a trading market, not over a closure);
- U = the first grid level strictly above H24; D = the first grid level strictly below L24. The grid is
  {k * s + o}, where o is the grid offset.

Events, grid offset o = 0 (round):
- **edge LONG**: Close_j >= U (the first close beyond an untouched round level); side +1;
- **edge SHORT**: Close_j <= D; side -1.

Events, counterfactuals:
- **C1 (main)**: the same rule on the shifted grid o = 0.37 s (not a half or quarter figure), same side. It is the
  same 24 h breakout through a level, minus the roundness.
- **C2**: the near miss on the round grid. LONG: High_j >= U - delta and Close_j < U and H24 < U - delta, with delta =
  0.25 * Event_Vol_j * Close_j. That is a fresh approach to an untouched round level that does not break it. Osler's
  take-profit orders predict a stall or reversal, so the LONG side should do worse than the edge. SHORT mirrors it.

If one bar crosses several levels, it counts once.

Gate 0 cell H1_V1 (D083): 1 h lookback Event_Vol, EWM 100, 1 h vertical, 1:1.

## Verdict

The Rev 47 Gate 0 rule (pooled drift and C1 at one-sided 90 %; C2 diff > 0; the asset's own diffs > 0), plus
recency: the asset's own drift diff must be > 0 on 2022-01-01 .. end, as in R1. The diagnostic slices are 2014-2021
and 2022+. NASDAQ is the target asset; the other 9 assets get their own verdicts (cross-asset coverage).

Trial count: Gate 0 test 26 of the NASDAQ campaign.

## Edge build: <ASSET>/roundlevelcascade_long on XAUUSD, HK, JPN -- Gate A-C rule (written 2026-11-09, before any Gate A result)

Owner 2026-11-09 "yes approve both": T13 LONG to Gate A on XAUUSD, HK and JPN (BTCUSD left out: its C1 is negative in
2022+), plus the HISTORY_EXTENSION_DESIGN.md changes. NASDAQ work continues alongside (T14).

- **Trigger.** Identical to Gate 0 (grid offset 0, up-breaks only): candidate_events.py = gate0_tool.t13_events.
  Scaffold: git d7b19b31^ XAUUSD/rangestructure_long (generic V2 runtime, feature schema verbatim).
- **Search and pay (D050-A).** Gate A on the runbook's 405 cells (1,2,4,6,8 h x EWM 100,150,200 x 1,2,4,8,16,24,48,96,168 h
  x 1:1, 1.5:1, 2:1) on the FULL history at the broker cost-model mean spread. Gate C replays every cell (--all-cells,
  broker cost model, 5,700 ms), PBO / CSCV runs over the 405 + control, and Level-1 DSR / FWER (report-only) runs over
  every Gate C trial, on events from **2022-01-01** onward (owner-approved design: broker costs are fitted on the
  recent period).
- **Lock.** The winner is the argmax compound velocity among cells that are Gate B positive, with exec_mean_R > 0 and
  coverage >= 99 % (all 405 eligible, no vertical restriction). None eligible = KILL.
- **Then.** The Rev 47 post-Gate C drift re-check at the locked cell (one-sided 90 %, 2022+ events), then export,
  Task 2A, Steps 01-09 and Gate D v4.1.
- **Trials.** 3 edges x 405 cells. They are charged in each edge's PBO / DSR. They count as one Gate 0 test (26) in
  the campaign tally.

Data note (2026-11-09): raw ticks cover the whole extended history (owner kept all raw ticks), so Task 2A / model-row
tie resolution use real ticks in every year. The design's bar-OHLC label fallback is needed only where Dukascopy has
no ticks.

Amendment 2026-11-09, during Gate C and before any Gate C result was seen. Owner: "yes run the full history Gate C as
well. It should be the standing rule now since the whole raw ticks are kept for each asset."
- Each edge's Gate C also replays all 405 cells on the FULL history (no --start); PBO / DSR are reported on both
  windows.
- The 2022+ run stays the decision basis unless the owner decides otherwise when the results are shown.
