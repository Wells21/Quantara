# MLAM Ch.5 (Financial labels, printed pp.65-73) -- pre-registered tests (2026-11-03, before any result)

## T1 -- trend-scanning replication (s.5.4, Snippets 5.1-5.3, pp.67-72)
Transcribe tValLinR / getBinsFromTrend (span [3, 10, 1]) and run Snippet 5.3 (Gaussian random walk sd .1, 100 points,
plus sin(linspace(0, 10))) at seeds 0-99. The book prints only figures, so the check is qualitative: per seed, the share
of labelled points whose bin equals the sign of the true deterministic trend (d/dt of the sine) over the chosen
look-forward window, and the number of label runs. Reading rule: median agreement >= 0.8 and t1 always after t0 = our
transcription behaves as Figure 5.1 describes (a few long trend segments); otherwise the transcription is re-checked.
Nothing in the pipeline changes from T1.

## T2 -- label seasonality (s.5.2 p.66: constant thresholds + heteroscedastic returns transfer intraday seasonality
to the labels)
Our barriers are scaled by Event_Vol (EWM of H-hour returns, span 100-200), which does not follow the intraday cycle.
The 6 edges, eligible rows (mldp_target_eligible), entry hour bucketed into 6 four-hour UTC blocks. Per edge: chi-square
test of independence of (a) the training target int(label_r > 0) and (b) the barrier outcome (pt / sl / vertical,
barrier_hit) against the block; Cramer's V; per block positive rate, vertical share, median holding hours
(label_t1 - Datetime). Reading rule: p < 0.01 AND Cramer's V >= 0.10 on (b) for an edge = the label distribution is
hour-dependent; reported to the owner with a proposal to test intraday-volatility-adjusted barriers (p.66 second
solution, standardised returns). Otherwise no change. Diagnostic only; the model's features may legitimately carry time
of day, so a dependence on (a) alone is not a defect.

## T3

Result 2026-11-03 (t3_results.json): rs V .146, velocity worse; vc V .107, velocity better only via a different cell.
Rule not met on either edge -> no change.
 -- intraday-seasonal barriers on XAUUSD (owner 2026-11-03: "Approved A, run the test in parallel"; written after
T1/T2, before any T3 result)
Change tested (MLAM s.5.2 p.66, "standardised returns, adjusted for the volatility predicted over the interval"):
Event_Vol x f_D(h0), f = sqrt(predicted variance of the H clock hours starting at the entry hour / H x the average
hour), hourly variance profile from the 60 trading days before the entry day (causal), f = 1 with < 20 days of history,
clipped [0.25, 4]. shared/research/mlam_ch5_seasonal_lab.py wraps v3_horizon_lab: grid (Gate A) and execution
finalists (Gate C) use the adjusted Event_Vol; the control-parity gate is run UNADJUSTED (baseline reproduction check).
Edges: XAUUSD rangestructure_long, XAUUSD volatilitycarry_long (the two T2 hour-dependent edges). Research data only;
holdout sealed. No edge artifact other than new stamped lab outputs is written; no edge changes before the owner decides.
Runs: Gate A = the standing grid (lookbacks 1,2,4,6,8 h x EWM 100,150,200 x verticals 1..168 h x 1:1,1.5:1,2:1,
--hybrid-fast-grid, --cost-bps 0.2583 = the baseline's), both edges concurrently; Gate C = gate_c_runner --all-cells
(broker cost model, 5700 ms, bootstrap 2000), serially (shared tick store).
Baseline: the 2026-10-31 runs (Gate A 20261031T140731Z / 20261031T142307Z; Gate C jsonl
logs/ewm_span_test_20261031/XAUUSD_*_ewm.jsonl), restricted to the same 405 cells (EWM 100,150,200).
Measures per edge, seasonal vs baseline: (1) hour dependence: T2's Cramer's V of barrier outcome (pt/sl/vertical) vs
4-hour entry block, on the Gate C trades of the baseline's Gate C winner cell and of the seasonal run's winner;
(2) Gate A best gross_mean_R / net_mean_R; (3) Gate C winner (argmax compound velocity, exec_mean_R > 0, coverage >= 0.99):
exec_mean_R, compound velocity, trades; (4) PBO / Prob[OOS <= 0] over the 405 replayed cells (gate_c_pbo).
Reading rule: propose seasonal barriers for XAUUSD only if (1) V < 0.10 on the seasonal winner AND (3) the seasonal
winner's compound velocity >= the baseline winner's AND (4) PBO < 0.5 and Prob[OOS <= 0] < 0.5. If (1) holds but (3)
does not, report as "labels fixed, economics not improved" (owner decides). Otherwise no change.
Trials (D050-A): the 405 replayed cells per edge are trials of that edge; recorded, and they join its DSR record if the
owner adopts the change.
