# T19: Tokyo fix yen demand on GBPJPY (OUR formalisation; pre-registered 2026-11-09, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.

## Mechanism

Japanese banks set the day's customer FX rates (TTM) at the Tokyo fix, 09:55 JST. Importers and other corporates
buy dollars at that rate, and banks pre-hedge the expected net USD demand in the market before 09:55. That pushes
USDJPY up, the yen weaker, into the fix. The pressure stops at the fix and the price partly reverts. The effect is
documented as strongest on "gotobi" days (the 5th, 10th, 15th, 20th, 25th and the month end), when settlement flows
concentrate. Source, page numbers NOT checked: Ito and Yamada, "Puzzles in the Tokyo fixing in the forex market:
order imbalances and biased pricing", Journal of International Money and Finance 73, 2017.

GBPJPY = GBPUSD x USDJPY, so USDJPY pressure moves GBPJPY the same way (GBPUSD adds noise).

Predictions:
- **LONG** GBPJPY from 08:55 JST for 1 h (into the 09:55 fix): the yen weakens into the fix;
- **SHORT** GBPJPY from 09:55 JST for 1 h (after the fix): the pre-fix push reverts.

## Rule (bars only, Japanese weekdays)

- LONG entry = the first bar closing at or after 08:55 Asia/Tokyo, at most 30 min later; side +1.
- SHORT entry = the first bar closing at or after 09:55 Asia/Tokyo, at most 30 min later; side -1.

Counterfactuals (same side):
- **C1 (main)**: the same clock pattern two hours later, LONG from 10:55 JST and SHORT from 11:55 JST. That is the
  same Tokyo morning with no fix;
- **C2**: non-gotobi days only. Gotobi day = the 5th, 10th, 15th, 20th, 25th or the last calendar day of the month;
  when it falls on a Saturday or Sunday, the preceding Friday. (Japanese public holidays are not modelled.) The fix
  flow is largest on gotobi days, so the all-days edge should beat the non-gotobi subset.

Firing rate: 5 a week per side.

Universe: GBPJPY (the only yen pair we trade). The other 9 assets are run and reported as placebos with no verdict.
Gate 0 cell H1_V1 (D083).

## Verdict

The Rev 47 Gate 0 rule on GBPJPY (drift and C1 at one-sided 90 %; C2 diff > 0; own diffs > 0), plus recency: the
drift diff must be > 0 on 2022+. Diagnostic slices: 2012-2021 and 2022+; hour-matched drift (T15 lesson).

Trial count: Gate 0 test 32 of the campaign.
