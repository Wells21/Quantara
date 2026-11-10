# T31 -- failed range breakout reversal (all 10 assets; aimed at SHORTS)

Written 2026-11-10, before any return of this card was computed. Gate 0 test 44. Rules: D090, D092 (>= 2/week on
2022+), D093 (EWM 150), D050-A.

## Mechanism
Breakout traders and trend-followers buy a new high of the recent range and place protective sell stops just below the
broken level. When price falls back inside the range soon after the break, the breakout buyers are trapped: their
stops below the old high fire and late longs exit, so the failure continues down (stop cascade on the other side;
Osler 2003/2005 on stop clustering around salient levels; the "false breakout" / "bull trap" of practitioner
literature). The mirror (failed breakdown -> long) is computed too.

Who loses: late breakout buyers whose stops sit just below the broken level.
Should work: ranging-to-trending transitions, assets with heavy breakout / CTA participation.
Should fail: strong trends where pullbacks into the range are bought again.

## Event (causal)
Hourly UTC checks as T30 (c = last close at or before t; entry = first bar closing at or after t; 30-min bound).
H / L = max / min hourly close over the 5 days before the last full hour (that hour excluded).
- Up-break: c > H. SHORT edge: the first check within 24 h after an up-break with c < H_break (the level that was
  broken); then disarmed until the next up-break. Checks that are themselves breaks are not entries.
- LONG edge mirrors: first check within 24 h after a down-break with c > L_break.

## Counterfactual (drift-neutral, D090)
- C1 SHORT: the first check of each UTC day with H - 0.10 (H - L) <= c < H (near the top of the range) and no up-break
  in the previous 24 h. Same asset, side, period, position in the range; only the failed break is removed.
- C1 LONG mirrors (near the bottom, no down-break in the previous 24 h).

## Gate 0 cell and rule
Cell 4 h Event_Vol, EWM 150, 24 h vertical, 1:1. PASS per asset and side (D090): C1 own diff - 1.28 SE > 0 (full
history), C1 diff > 0 on 2022+, >= 100 events in edge and C1, >= 2 events/week on 2022+. Universe: all 10 assets.

## Expected firing rate (event COUNTS only, 2022+, computed before any return)
Edge SHORT / LONG events per week: NASDAQ 2.38 / 1.81; US500 1.80 / 1.70; DAX 1.94 / 1.64; JPN 2.18 / 1.84;
HK 1.50 / 1.66; XAUUSD 2.45 / 1.68; GBPJPY 2.35 / 1.68; USDMXN 1.71 / 2.36; USOIL 2.01 / 1.76; BTCUSD 2.13 / 1.98.
So the firing rule can pass only for SHORT on NASDAQ, JPN, XAUUSD, GBPJPY, USOIL, BTCUSD and LONG on USDMXN.
C1 runs at about half the edge frequency (0.5-1.2/week); every edge and C1 set has >= 244 events on the full history.
Design history (counts only): a 10-day range gave 1.0-1.9 failures/week; the 5-day range is the first that reaches
2/week on several assets.

## Overlap with earlier cards
T30 trades WITH a new range extreme; T31 trades AGAINST a break that has already failed (back inside within 24 h).
T13 used a fixed round-number grid; T25 a 1-year high split at its median. T31's state (a recent break that failed)
is new.

## Trial accounting
Test 44. Gate A-C on the D093 120-cell grid; all cells paid.
