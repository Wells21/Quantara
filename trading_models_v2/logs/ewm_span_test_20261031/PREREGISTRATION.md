# EWM span test (owner 2026-10-31: "Run the test now") -- written before any result

Question: are EWM spans above 100 better than the runbook's 20 / 50 / 100, so that the grid should become e.g.
{100, 150, 200} or {100, 200, 300}? Diagnostic study; the runbook grid is unchanged until the owner decides.

Background (shared/research/grid_dimension_sensitivity/, 2026-10-31): on the 6 Gate C edges EWM 100 won 12 / 12 full
replays; old bar-count Gate A grids with spans to 300: after cost 200 won 89 / 147 and 300 38 / 147, before cost no span
dominated (cost-in-R mechanism suspected).

Design
- Edges: GBPJPY dailyrangecompletionfade_long, HK drawdownhazardcontinuation_short, JPN rangestructure_long,
  JPN volatilitycarry_long, XAUUSD rangestructure_long, XAUUSD volatilitycarry_long. Research data only; holdout sealed.
- Gate A: v3_horizon_lab --hybrid-fast-grid, lookbacks 1,2,4,6,8 h x EWM 100,150,200,300 x verticals
  1,2,4,8,16,24,48,96,168 h x barriers 1:1,1.5:1,2:1 (540 cells), --cost-bps = the edge's last Gate A cost
  (GBPJPY 0.7388, HK 2.5179, JPN 1.9231, XAUUSD 0.2583).
- Gate C: gate_c_runner.py --all-cells on that grid (broker cost model, 5700 ms, bootstrap 2000), batch 135.
- Reproduction check: the EWM 100 cells must match the last full replay (2026-10-26; XAUUSD 2026-10-17) -- same trade
  counts, exec_mean_R within rounding -- or the comparison is void and investigated.

Measures, per edge
1. Gate C winner (argmax compound velocity among exec_mean_R > 0, coverage >= 0.99) over the 540 cells; its span.
2. Best compound velocity with the span fixed at each of 100 / 150 / 200 / 300 (ratio to the overall best).
3. Paired trade-level tests between adjacent spans (100-150, 150-200, 200-300), week-clustered t.
4. Gate A gross_mean_R (mid) vs net_mean_R by span -- is the gain cost-driven?
5. Median barrier distance (median_event_vol_bps in Gate A) by span.
6. PBO / CSCV over each candidate 3-span grid's 405 cells ({100,150,200}, {100,200,300}) vs the runbook grid's 405
   (20 / 50 / 100 from the existing full replay): PBO and Prob[OOS <= 0].

Decision rule (owner decides)
- Recommend a new span set only if higher spans give the best compound velocity on at least 4 of 6 edges AND the
  candidate grid's PBO / Prob[OOS <= 0] are not worse than the runbook grid's on those edges.
- Otherwise keep 20 / 50 / 100 (or propose only dropping 20).

Trials (D050-A): every replayed cell is a trial of that edge; if an edge later adopts a new span, these runs' stamps join
its DSR record. No edge changes before the RECERT.
