# T16: session hand-over continuation (OUR thesis; pre-registered 2026-11-09, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.

## Mechanism

Trading moves around the globe in three regional sessions: Tokyo, London and New York. Each region brings its own
clients (local real-money funds, corporates, local dealers), and many of them act at or soon after their local open:
- they act on the information that arrived while their region was shut: overnight news, and the move the previous
  session already printed;
- they rebalance and hedge exposures that the previous session's move changed;
- local dealers start working orders handed over from the other desks.

That flow arrives in the direction of the previous session's move, because the move is part of the news, and it is
slower-moving money than the intraday traders who made the move. Prediction: the first hour of a new session
continues the previous session's move. The effect is specific to the hand-over. The same look-back signal taken in the
middle of a session, with no new clientele arriving, should continue less (C1).

Disclosure: T3 (2026-11-05) faded the overnight move at the European open on NASDAQ / US500 and failed both sides
(NASDAQ LONG -0.009 R). That covers one of T16's three boundaries on two assets, with the opposite bet. T16 is broader:
three boundaries, 10 assets, continuation.

## Rule (bars only; local clocks follow daylight saving)

Hand-over boundaries B on each weekday D, with the previous session window [A, B):
- **London**: B = 08:00 Europe/London; A = 09:00 Asia/Tokyo the same day (the Tokyo session);
- **New York**: B = 08:00 America/New_York; A = 08:00 Europe/London the same day (the London morning);
- **Tokyo**: B = 09:00 Asia/Tokyo; A = 08:00 America/New_York on the previous weekday (the New York session).

Prices: close of the last bar at or before t, at most 30 min stale. Signal s = log(P(B) / P(A)), s != 0.
Entry: the first bar closing at or after B, at most 30 min later. Side = sign(s) (continuation). No magnitude filter
(recall first; the meta-model filters later).

Events:
- **edge**: every boundary, side = sign(s);
- **C1 (main)**: the same construction moved 3 h later, into the middle of the new session. B' = B + 3 h, A' = B' -
  (B - A), side = sign(log P(B') / P(A')). It is the same look-back length without the hand-over;
- **C2**: a stale signal. The same boundary B today, with the side taken from the PREVIOUS weekday's signal at that
  boundary. Today's move should matter more than yesterday's.

Expected firing rate: 3 boundaries x 5 weekdays = about 15 events a week, about 7 per side (rule: >= 3).

Universe (pooled, D075 rule C): all 10 assets, both sides. Gate 0 cell H1_V1 (D083): 1 h lookback Event_Vol, EWM 100,
1 h vertical, 1:1. The baselines are month-matched random entries (gating) and, diagnostic, hour-matched (T15 lesson:
clock-time entries).

## Verdict

The Rev 47 Gate 0 rule (pooled drift and C1 at one-sided 90 %; C2 diff > 0; the asset's own diffs > 0), plus
recency: the asset's own drift diff must be > 0 on 2022+. Diagnostic slices: 2014-2021 and 2022+. Events per week on
2022+ are reported.

Trial count: Gate 0 test 29 of the campaign.
