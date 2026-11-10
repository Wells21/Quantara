# T20: gold, Asian physical demand vs New York paper supply (OUR thesis; pre-registered 2026-11-09, before any run)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.

## Mechanism

Gold has two very different marginal buyers and sellers, and they trade in different hours:
- **Asian session.** Chinese and Indian physical demand: jewellers, banks, the Shanghai Gold Exchange (SGE). SGE
  trading opens at 09:00 Beijing (01:00 UTC). SGE prices must stay arbitraged with London through import quotas,
  which can keep a local premium. Physical buying hits the international market mainly in Asian hours.
- **New York session.** Western paper gold: COMEX futures (the pit-era open is 08:20 New York), ETFs and macro funds.
  Liquidations and hedging by Western leveraged holders concentrate in New York hours.

Prediction: gold drifts up in the Asian morning and down in the New York morning, relative to the other session.
- **LONG** at the SGE open for 4 h beats a LONG at the COMEX open (and random LONG entries);
- **SHORT** at the COMEX open for 4 h beats a SHORT at the SGE open (and random SHORT entries).

Disclosure: like T18, the two sides are one contrast (Asia session vs NY session) with the edge and the control
swapped. They are not independent evidence. Market commentary has described this "gold rises in Asia, falls in New
York" pattern; I am not citing a peer-reviewed source.

## Rule (bars only, weekdays)

- Asia entry = the first bar closing at or after 09:00 Asia/Shanghai, at most 30 min later.
- NY entry = the first bar closing at or after 08:20 America/New_York, at most 30 min later.
- LONG: **edge** = LONG at the Asia entry; **C1** = LONG at the NY entry.
- SHORT: **edge** = SHORT at the NY entry; **C1** = SHORT at the Asia entry.

Card cell: 4 h lookback Event_Vol, EWM 100, **4 h vertical**, 1:1. That covers the SGE morning (01:00-05:00 UTC) and
the New York morning (about 12:20-16:20 UTC).

Firing rate: 5 a week per side.

Universe: XAUUSD. The other 9 assets are run and reported as placebos with no verdict.

## Verdict

The Rev 47 Gate 0 rule on XAUUSD (drift and C1 at one-sided 90 %; own diffs > 0), plus recency: the drift diff must be
> 0 on 2022+. Diagnostic slices: 2005-2013, 2014-2021 and 2022+; hour-matched drift.

Trial count: Gate 0 test 33 of the campaign.
