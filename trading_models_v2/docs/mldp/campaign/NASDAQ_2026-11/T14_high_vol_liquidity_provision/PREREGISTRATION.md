# T14: high-volatility liquidity-provision reversal (OUR thesis; pre-registered 2026-11-09, before any run)

## Mechanism

A sharp one-hour move is usually an order imbalance: someone needs immediacy (deleveraging, a margin call, a stop
cascade, a hedge). Liquidity providers absorb it and charge for that through a subsequent partial reversal. The charge
is larger when providing liquidity is riskier, i.e. when volatility is high: providers' capital and risk limits bind,
fewer of them stand in front of the flow, and the price concession needed to clear the imbalance is larger.

Sources, as the idea; page numbers NOT checked:
- Grossman and Miller, "Liquidity and market structure", Journal of Finance 43(3), 1988 (the price of immediacy);
- Nagel, "Evaporating liquidity", Review of Financial Studies 25(7), 2012 (returns to short-term reversal strategies
  rise with VIX). His evidence is cross-sectional (stocks); the index / single-market version here is ours.

Prediction: after a sharp 1 h move in a HIGH-volatility regime, the price partly reverses over the next hour. In a
LOW-volatility regime the same move reverses less (the premium is small when liquidity is cheap). That regime contrast
is the main counterfactual (C1).

This differs from T6, which faded or followed every hourly move by an autocorrelation regime, with no size threshold.
It also differs from the older mechanical ports (reversal, exhaustionreversal, overextensionfade). None of them
conditions on the volatility level or tests the regime contrast.

## Rule (live-tradable, bars only, decided at each bar close)

- vol1h_j = Event_Vol of the Gate 0 cell (1 h calendar-time returns, EWM 100), taken at the PREVIOUS bar j-1, so the
  move itself does not shrink its own z-score;
- r_j = log(Close_j / Close of the last bar at or before t_j - 1 h); that reference bar must be at most 30 min older
  than t_j - 1 h;
- z_j = r_j / vol1h_{j-1}; a FRESH sharp move needs |z_j| >= 2 and |z_{j-1}| < 2;
- regime: R_j = calendar-time EWM vol of 24 h returns with span 1,000 bars (about 20 trading days at 50 bars/day).
  The daily series is R at the last bar of each UTC day. HIGH = R_j > the median of that daily series over the 250
  calendar days ending the PREVIOUS UTC day (needs 120 days of history). LOW otherwise;
- non-overlap: within each event set, an event is skipped if it is less than 1 h (the vertical) after the previous
  accepted event of that set.

Events (side = -sign(z), a fade):
- **edge**: HIGH and |z| >= 2. LONG after a sharp drop (z <= -2), SHORT after a sharp rise (z >= +2);
- **C1 (main)**: LOW and |z| >= 2, same fade side;
- **C2**: HIGH and 1 <= |z| < 2 (moderate moves), same fade side. The premium should concentrate in the large
  imbalances.

Universe (pooled, D075 rule C): all 10 assets, both sides. Gate 0 cell H1_V1 (D083): 1 h lookback Event_Vol, EWM
100, 1 h vertical, 1:1.

## Verdict

The Rev 47 Gate 0 rule (pooled drift and C1 at one-sided 90 %; C2 diff > 0; the asset's own diffs > 0), plus
recency: the asset's own drift diff must be > 0 on 2022-01-01 .. end. The diagnostic slices are 2014-2021 and 2022+.
NASDAQ is the target asset.

Trial count: Gate 0 test 27 of the NASDAQ campaign.
