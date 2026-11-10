# T21: overnight risk premium in non-equity risk assets (T18 cross-asset coverage; pre-registered 2026-11-09)

Owner standing rules: 1 long + 1 short edge on each of the 10 assets; every edge fires at least 3 times a week.
Cross-asset coverage rule (standing): a thesis that passes on some assets is tried on the others.

## Mechanism

T18's mechanism: US dealers end the cash session holding inventory that clients pushed onto them, and they are
paid to carry it overnight (Boyarchenko, Larsen and Whelan 2023 RFS). The overnight return of RISK assets is
therefore high and the US-session return low (Lou, Polk and Skouras 2019 JFE, for equities). If the premium is
compensation for carrying RISK overnight, it should extend to the other US-traded risk assets:
- each asset's **risk-on** direction earns overnight, from the US cash close to the next US open;
- its **risk-off** direction earns during the US cash session.

Risk-on direction d, fixed before any run (the sign of the asset's beta to US equities):
- GBPJPY +1 (yen-funded carry, risk-on = GBP up);
- USOIL +1;
- BTCUSD +1;
- USDMXN -1 (MXN is the main liquid EM risk-on currency, risk-on = USDMXN down).
Gold is excluded: it has no stable equity beta, and T20 already used its overnight hours.

## Rule (bars only, US weekdays)

- close entry = the first bar closing at or after 16:00 America/New_York, at most 30 min later;
- open entry = the first bar closing at or after 09:30 America/New_York, at most 30 min later.

Events, per asset, mapped to true LONG / SHORT sides (the drift baseline uses random entries of the same side):
- **risk-on overnight**: side d at the close entry. That is the LONG edge for GBPJPY, USOIL and BTCUSD, and the SHORT
  edge for USDMXN. C1 = side d at the open entry;
- **risk-off in session**: side -d at the open entry. That is the SHORT edge for GBPJPY, USOIL and BTCUSD, and the
  LONG edge for USDMXN. C1 = side -d at the close entry.

As in T18, each side's edge is the other side's control: one contrast, two faces.

Cell (as T18): 4 h lookback Event_Vol, EWM 100, 16 h vertical, 1:1. Firing rate: 5 a week per side.

Universe (pooled, D075 rule C): GBPJPY, USOIL, BTCUSD, USDMXN. The equity indices (T18) and gold (T20) are
reported as placebos with no verdict.

## Verdict

The Rev 47 Gate 0 rule (pooled drift and C1 at one-sided 90 %; the asset's own drift and C1 diffs > 0), plus recency:
the asset's own drift diff must be > 0 on 2022+. Diagnostic slices: 2014-2021 and 2022+; hour-matched drift.

Trial count: Gate 0 test 34 of the campaign.
