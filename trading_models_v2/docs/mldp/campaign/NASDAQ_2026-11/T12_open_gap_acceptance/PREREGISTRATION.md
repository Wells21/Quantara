# T12: cash-open gap acceptance (OUR thesis; pre-registered 2026-11-07, before any run)

## Mechanism

Overnight news is priced first by futures/CFDs while the cash market is shut, so the index gaps at the cash open.
Much institutional money (mutual funds, pensions, index and benchmark-tracking desks) can only trade in the cash
session, and large orders are split over time (Kyle 1985-style order splitting). If the first 30 minutes of the cash
session EXTEND the gap, cash-session order flow agrees with the overnight information and keeps arriving. Prediction:
the price continues in the gap direction over the next hour. If the first 30 minutes REJECT the gap, the gap was an
overnight overreaction, and the continuation should be absent.

This is a continuation thesis, not a move-fade. It is distinct from `gapreversion` (which fades gaps) and from
`openingrangebreakout` (no overnight-gap condition; US500 long only).

## Rule (live-tradable, clock + bars only)

Universe (pooled, D075 rule C): the cash equity indices with a cash open:
- NASDAQ, US500: 09:30 New York (close 16:00)
- DAX: 09:00 Berlin (close 17:30)
- JPN: 09:00 Tokyo (close 15:00, 15:30 from 2024-11-05)
- HK: 09:30 Hong Kong (close 16:00)

Other assets have no cash open; they are reported with no events.

For each weekday D:
- P_close = close of the last bar at or before the previous weekday's cash close (stale <= 30 min);
- P_open = close of the last bar at or before D's cash open;
- gap g = log(P_open / P_close);
- big gap: |g| >= the median |g| of the previous 60 days with a gap (strictly earlier days; needs 60);
- entry bar = the FIRST bar closing at or after open + 30 min (at most 30 min stale), so the trade is live-tradable;
- a = log(P_entry / P_open).

Events:
- **edge**: big gap and sign(a) = sign(g) -> side = sign(g) (LONG for gap up, SHORT for gap down);
- **C1 (main counterfactual)**: big gap and sign(a) = -sign(g) (rejected), same side sign(g);
- **C2**: small gap (|g| < median) and sign(a) = sign(g), same side sign(g).

Gate 0 cell H1_V1 (D083): 1 h lookback Event_Vol, EWM 100, 1 h vertical, 1:1.

## Verdict

The Rev 47 Gate 0 rule (pooled drift and C1 at one-sided 90 %; C2 diff > 0; the asset's own diffs > 0), plus
recency (the asset's own drift diff > 0 on 2022-01-01 .. end, as in R1). The diagnostic slices are 2014-2021 and
2022+. NASDAQ is the target asset; the other universe assets get their own verdicts.

Trial count: Gate 0 test 25 of the NASDAQ campaign.
