# T23: a currency weakens in its own trading hours -- GBPJPY (pre-registered 2026-11-09, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.
Gate 0 rule: D090 (per asset, drift-neutral C1 decides).

## Mechanism

FX order flow is segmented by time zone. During a country's working hours its residents trade most: importers,
investors buying foreign assets and hedgers mostly BUY foreign currency with their home currency. Dealers absorb that
flow and lay it off when the other side's residents are active. The documented pattern: a currency tends to
depreciate during its domestic trading hours and appreciate during foreign hours.
- A. Ranaldo, "Segmentation and time-of-day patterns in foreign exchange markets", Journal of Banking & Finance 33
  (2009).
- R. Breedon and A. Ranaldo, "Intraday patterns in FX returns and order flow", Journal of Money, Credit and Banking 45
  (2013).
I have not checked page numbers.

Prediction for GBPJPY:
- in Tokyo hours the yen weakens -> GBPJPY rises -> **LONG at the Tokyo open**;
- in London hours sterling weakens -> GBPJPY falls -> **SHORT at the London open**.

Disclosure:
- Like T18 and T20, the two sides are one contrast (Tokyo session vs London session) with edge and C1 swapped; they
  are not independent evidence.
- Seen data: T19 (Tokyo fix, GBPJPY) found the LONG 08:55-09:55 Tokyo hour had the WRONG sign. That is the first hour
  of this card's long window. This card is a different hypothesis (whole Tokyo morning, every day, not the gotobi fix),
  but the overlap is declared.

## Rule (bars only, UTC weekdays)

- Tokyo entry = the first bar closing at or after 09:00 Asia/Tokyo (00:00 UTC), at most 30 min later.
- London entry = the first bar closing at or after 08:00 Europe/London, at most 30 min later.
- New York entry = the first bar closing at or after 09:00 America/New_York, at most 30 min later.
- LONG: **edge** = LONG at the Tokyo entry; **C1** = LONG at the London entry (only the session changes:
  drift-neutral); **C2** = LONG at the New York entry (neither home market).
- SHORT: **edge** = SHORT at the London entry; **C1** = SHORT at the Tokyo entry; **C2** = SHORT at the New York entry.

Card cell: 4 h lookback Event_Vol, EWM 100, **6 h vertical**, 1:1 -- the Tokyo trading day (00:00-06:00 UTC) and the
London morning.

Firing rate: about 5 a week per side.

Universe: GBPJPY. The other 9 assets are run and reported as placebos with no verdict.

## Verdict (D090)

On GBPJPY, per side: C1 diff - 1.28 SE > 0 (full history), C2 diff > 0, C1 diff > 0 on 2022+, >= 100 events in edge
and every counterfactual, >= 3 events a week on 2022+. Month-matched drift, hour-matched drift and pooled results are
diagnostics.

Trial count: Gate 0 test 36 of the campaign.
