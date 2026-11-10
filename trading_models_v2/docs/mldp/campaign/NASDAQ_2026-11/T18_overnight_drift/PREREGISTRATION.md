# T18: overnight drift vs the cash session (pre-registered 2026-11-09, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.

## Mechanism

For decades, almost all of the equity index return has accrued OUTSIDE the cash session, between the close and the next
open. The return during the cash session has been about zero. Sources, page numbers NOT checked:
- Lou, Polk and Skouras, "A tug of war: overnight versus intraday expected returns", Journal of Financial Economics
  134(1), 2019. Different clienteles trade at different times (individuals around the open, institutions through the
  day), and their opposite demands are priced at different times;
- Boyarchenko, Larsen and Whelan, "The overnight drift", Review of Financial Studies 36(9), 2023. Dealers end the US
  day with inventory imbalances and are compensated with a positive overnight return, concentrated around the European
  open. That is the source T3 used; T3 tested a reversal at the European open, not the overnight hold.

Prediction:
- **LONG** entered at the cash close beats a LONG entered at the cash open (and random LONG entries);
- **SHORT** entered at the cash open beats a SHORT entered at the cash close (and random SHORT entries). Shorting
  the session that does not carry the drift costs less than shorting the one that does.

## Rule (bars only, every weekday)

Cash sessions (local clocks): NASDAQ, US500 09:30-16:00 New York; DAX 09:00-17:30 Berlin; JPN 09:00-15:00 Tokyo
(15:30 from 2024-11-05); HK 09:30-16:00 Hong Kong.
- close entry = the first bar closing at or after the cash close, at most 30 min later;
- open entry = the first bar closing at or after the cash open, at most 30 min later.

Events:
- LONG: **edge** = LONG at the close entry; **C1** = LONG at the open entry;
- SHORT: **edge** = SHORT at the open entry; **C1** = SHORT at the close entry.

The cell is pre-registered for this card: 4 h lookback Event_Vol, EWM 100, **16 h vertical**, 1:1. The overnight drift
takes most of the night: BLW find it concentrated around the European open, 10-12 h after the US close. A 1 h
vertical would miss it. The same cell serves both sides.

Firing rate: 5 a week per side per index.

Universe (pooled, D075 rule C): the 5 cash equity indices.

## Verdict

The Rev 47 Gate 0 rule (pooled drift and C1 at one-sided 90 %; the asset's own drift and C1 diffs > 0), plus recency:
the asset's own drift diff must be > 0 on 2022+. Diagnostic slices: 2014-2021 and 2022+.

Trial count: Gate 0 test 31 of the campaign.

## Edge build: overnightdrift_long (NASDAQ, US500, DAX, JPN, HK) and overnightdrift_short (DAX, JPN, HK) -- Gate A-C rule (written 2026-11-09, before any Gate A result)

- **Trigger.** Identical to Gate 0 (gate0_tool.t18_entries). Parity is exact on all 8 edges; DAX / HK short each have
  1 Gate 0 entry with no Event_Vol history, which is unlabelled in both.
- **Scaffold.** git d7b19b31^ XAUUSD/rangestructure_long (generic V2 runtime, feature schema verbatim); the shorts have
  the side-specific checks flipped.
- **Search and pay (D050-A).** Gate A on the runbook's 405 cells, full history, at the broker cost-model mean spread.
  Gate C replays every Gate-B-positive cell (--all-cells) on the FULL history with the era-scaled broker spread (STANDING
  RULE 2026-11-09), with PBO / CSCV over every trial and Level-1 DSR. The 2022+ locked-cell exec mean must be > 0.
- **Lock.** The winner is the argmax compound velocity among cells with exec_mean_R > 0 and coverage >= 99 %. None
  eligible = KILL. A Gate C that fails PBO is killed (owner 2026-11-09).
- **Firing.** Each edge fires about 3.8-4.9 a week on 2022+ (rule: >= 3).
