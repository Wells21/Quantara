# RECERT log (runbook Section 16C, Revision 42)

## Step 1 -- Gate A-C re-selection on the D074 grid (2026-11-03, owner: "Start the RECERT, step 1")

**Reuse of the 2026-10-31 runs (owner rule: tests already run are not repeated).** The EWM span test
(logs/ewm_span_test_20261031) ran Gate A and a Gate C replay of every cell over EWM 100,150,200,300 (540 cells per edge;
broker cost model, 5700 ms, bootstrap 2000) on these edges. Checked before reuse:
- no commit or working-tree change since 2026-10-31 to the 4 edges' sources, v3_horizon_lab, gate_c_runner,
  gate_c_pbo, shared/labels, shared/bars, shared/data, or the Moneta cost models;
- bars unchanged (mtimes 2026-09-13; JPN 2026-10-23), cost models 2026-10-17;
- the runs used the feature schema at 63f4a570 (the stored datasets'), as any Gate A-C run must until the step 2
  re-export.

**Read-out** (shared/research/recert_step1_reselection.py -> step1_reselection.json):
- grid = the runbook's 405 cells + control;
- Gate B = Gate A net_mean_R > 0;
- winner = argmax compound velocity among Gate B cells with exec_mean_R > 0 and coverage >= 0.99;
- PBO over the 405 + control, and over all 540 replayed + control.

**Stress** (shared/research/recert_step1_stress.py -> stress/): the winner replayed with broker spread levels
x (1 + level_uncertainty_pct).

| edge | old lock | new winner | exec mean R | Gate B cells / eligible | PBO / Prob[OOS<=0] (405) | PBO / Prob (540) | stress spread x | stress exec R | break-even spread x (approx) |
|---|---|---|---|---|---|---|---|---|---|
| GBPJPY drcf long | H6_E100_V168 2:1 | **H8_E200_V168 2:1** | 0.1013 | 282 / 162 | 0.164 / 0.344 | 0.151 / 0.319 | 1.189 | 0.0981 | 6.8 |
| HK dhc short | H8_E100_V168 2:1 | **H8_E150_V168 2:1** | 0.0916 | 189 / 108 | 0.008 / 0.273 | 0.010 / 0.296 | 1.033 | 0.0901 | 3.9 |
| JPN rs long | H8_E100_V96 2:1 | H8_E100_V96 2:1 (unchanged) | 0.0994 | 343 / 323 | 0.360 / 0.323 | 0.424 / 0.336 | 1.144 | 0.0925 | 4.3 |
| XAUUSD rs long | H8_E100_V16 2:1 | **H8_E200_V16 2:1** | 0.1462 | 405 / 405 | 0.073 / 0.043 | 0.087 / 0.040 | 1.205 | 0.1455 | 30.7 |

All four pass every check:
- Gate A: best gross mean R 0.13-0.26, above the 0.05 kill line;
- the winner is the same with and without the Gate B filter;
- PBO < 0.5 and Prob[OOS <= 0] < 0.5 under both trial sets;
- no edge is fragile under stress.

**Locked** (2026-11-03): EVENT_VOL_LOOKBACK_HOURS and EVENT_VOL_EWM_SPAN in candidate_events.py; VERTICAL_HORIZON_HOURS,
PT_MULTIPLIER and SL_MULTIPLIER in model_rows.py (values unchanged, comments updated); HK
export_research_dataset.py EXPECTED_VERTICAL_HORIZON_HOURS (value unchanged); edge.yaml labeling direct children
(vertical / PT / SL) unchanged -- all four places agree. HK edge.yaml's nested `event_volatility` block (lookback_bars
50, ewm_span 100) is an old descriptive note that no code reads; left untouched (runbook Step 7).

**Trial record (D050-A):** every cell of the 2026-10-31 runs, the 300-span cells included, joins each edge's trial
record. The step 6 DSR run passes these stamps together with all earlier Gate C stamps. The stress replays are
robustness checks of the locked cell, not new trials.

Consequences: GBPJPY, HK and XAUUSD changed their D1 candidate_logic_signature, so their D2/D3/D4 certificates are
invalid (Phase C/D is rebuilt in step 12). Next: step 2 (re-export every edge; same-asset exports serially).

### Step 1 correction -- GBPJPY feature window (2026-11-04, found at the step 2 export)

The GBPJPY re-export kept 3,988 model rows against 4,050 Gate C winner trades. The 62 missing events (2022-05-11 to
2022-06-01) fall before GBPJPY's first HMM regime bar, 2022-06-05 22:02. The 2026-10-23 FFD / HMM rebuild (D031)
lengthened the warm-up. The 2026-10-31 lab took its valid bar times from the stored pre-rebuild dataset, so it
replayed those events. The other three edges start after their warm-up and match exactly (HK 4,206, XAUUSD 4,379,
JPN 4,470).

The reuse check in step 1 missed this input. Re-read (shared/research/recert_step1_feature_window.py ->
step1_gbpjpy_feature_window.json): GBPJPY's saved trades with events before 2022-06-05 22:02 removed, all cells. Gate C
replays events independently, so this equals a replay without them.

| GBPJPY | before (4,050 trades) | feature window (3,988 trades) |
|---|---|---|
| winner | H8_E200_V168 2:1 | H8_E200_V168 2:1 (unchanged; next H6_E200_V168, H6_E100_V168) |
| exec mean R | 0.1013 | 0.0947 |
| PBO / Prob[OOS<=0], 405 + control | 0.164 / 0.344 | 0.236 / 0.441 |
| PBO / Prob[OOS<=0], 540 + control | 0.151 / 0.319 | 0.219 / 0.423 |
| stress x1.189 exec R | 0.0981 | 0.0915 |

Still a PASS, and the lock stands. Prob[OOS<=0] 0.44 is now closer to the 0.5 line, so read GBPJPY's later gates with
that in mind.

## Step 2 -- re-export (2026-11-04 08:15-08:27, 4 assets in parallel, BelowNormal)

All 4 exports returned 0: feature_count 187 and the locked geometry in model_row_manifest.json. Model rows equal the
winners' Gate C trades: GBPJPY 3,988 (feature window), HK 4,206, JPN 4,470, XAUUSD 4,379, all >= 2,000. Positive
labels: GBPJPY 38.3 %, HK 38.8 %, JPN 40.5 %, XAUUSD 49.6 %. Logs: step2/. Next: step 3 (Task 2A, Steps 01-09, Gate D).

## Step 3a -- Task 2A (2026-11-04 08:52-09:02, 4 in parallel)

| edge | Task 2A resolved | stale_entry_quote | quote-executable mean R | Gate C winner minus the stale events | per-trade max abs diff |
|---|---|---|---|---|---|
| GBPJPY | 3,987 / 3,988 | 1 | 0.095025 | 0.095025 (3,987) | 9e-14 |
| HK | 4,205 / 4,206 | 1 | 0.091906 | 0.091906 (4,205) | 4e-14 |
| JPN | 4,461 / 4,470 | 9 | 0.100664 | 0.100664 (4,461) | 7e-14 |
| XAUUSD | 4,379 / 4,379 | 0 | 0.146202 | 0.146202 (exact) | -- |

Exact on every matched trade. The only difference is Task 2A's stale-entry-quote exclusion (coverage 99.8-100 %,
within the runbook's ~99.9-100 %). Mid-path label mean R: GBPJPY 0.113, HK 0.113, JPN 0.147, XAUUSD 0.177.

## Step 3b -- Steps 01-09 + Gate D (started 2026-11-04, 4 lanes, QUANTARA_STEP01_WORKERS=2)

## Step 5 -- D075 theory re-check at the locked geometry (run 2026-11-04 alongside step 3; pre-registered step5/PREREGISTRATION.md)

The reproduction check passed on all 4 edges: the locked-cell event set equals the stored model rows' realized_r. Gross
mid R, one-sided 95 % (lower bound = diff - 1.645 SE).

| edge | events | edge mean R | random same-side mean R | drift diff (LB) | C1 diff (LB) | verdict |
|---|---|---|---|---|---|---|
| GBPJPY drcf | 4,343 | 0.118 | 0.117 | +0.047 (-0.060) | -0.020 (-0.308), 338 inverted | FAIL |
| HK dhc | 4,735 | 0.144 | 0.086 | +0.004 (-0.098) | +0.053 (-0.057), 2,236 inverted; C2 38 inverted (inconclusive) | FAIL |
| JPN rs | 4,845 | 0.159 | 0.085 | +0.026 (-0.077) | -0.012 (-0.126), 1,001 inverted | FAIL |
| XAUUSD rs | 4,769 | 0.162 | 0.079 | +0.029 (-0.038) | +0.016 (-0.078), 934 inverted | FAIL |
| rangestructure pooled | 9,614 | -- | -- | +0.028 (-0.032) | +0.002 (-0.072); JPN own C1 diff < 0 | FAIL |

Same verdict as at the old locks (2026-11-02). No edge's raw event set is distinguishable from random same-side entries
in the same months, and no card counterfactual shows that its trigger clause matters. D075 item 5: presented to the
owner (decision pending); nothing is retired automatically.
Steps 01-09 completed on all 4 edges (09:03-10:46). Step 01 took 27-29 min per edge (2 workers): every edge kept all 176
candidate features, in 2 clusters each, all passing rule B (HK's smaller cluster at t 1.76). Step 01 OOS AUC 0.513-0.543.
Step 03 (CPCV 45 splits, 1,000 trees) took 43-52 min. Logs: _lifecycle_logs/<ASSET>_<edge>/.

| edge | Gate D AUC | p (one-sided MWU) | OOF rows | verdict |
|---|---|---|---|---|
| GBPJPY drcf | 0.5175 | 0.032 | 3,987 | PASS |
| HK dhc | 0.5346 | 7.6e-05 | 4,205 | PASS |
| JPN rs | 0.5069 | 0.218 | 4,461 | **FAIL** |
| XAUUSD rs | 0.5251 | 0.002 | 4,379 | PASS |

JPN rs fails Gate D (no significant discrimination). Runbook Step 13 FAIL means removal from edge_universe after owner
review. Owner decision pending (presented together with step 5). Step 4 audits (label, CV leakage) are running.

## Owner decisions 2026-11-04 -- RECERT stopped at the edge level

Owner: "Yes, remove all four and propose the D075 fix". The reasoning, agreed:
- An edge enters edge_universe only after every edge-level gate passes; the allocator reads only deployed edges.
- So no edge-level check may depend on the allocator's final_live_threshold.
- The model-filtered drift test did, and on 2026-11-02 it gave the same verdict as the raw event set on all 6 edges.

All four edges fail the D075 theory check at the edge level (step 5); JPN also fails Gate D. They are removed from
config/edge_universe.json, which now has 0 edges (prior file: edge_universe_before_recert_removal_20261104.json). The
edge folders stay in place pending owner review (runbook Step 13 archive rule). The RECERT ends here: steps 6-13 do not
apply with no deployable edge.

Step 4 audits (finished before the decision; record only):
- CV leakage: OK on all 4 (shuffle inflation 0.008-0.025).
- Label audit: read the 2026-10-17 allocator thresholds against the new calibrators (approved share 1.0). Not
  meaningful, and another allocator-coupled edge-level check.

## Archive and owner decisions (2026-11-04, afternoon)

Owner: "Archive the four removed edge folders ... Now". Moved intact to
C:\Users\Administrator\Desktop\Quantara_edge_archive_2026-11-04\<ASSET>\<edge> (README.txt there): GBPJPY drcf
long, HK dhc short, JPN rs long, XAUUSD rs long, with their source, artifacts, models, step 1 locks and the
uncommitted T3 Gate C files.

Owner: leave the Phase D provider tests failing ("the edges are killed so why reverting the geometry locks"). After the
archive, the tests that read those edge folders error (FileNotFoundError); see runbook 12.3.

Gate D v4:
- D1 decision point approved: act when the cross-fitted calibrated P(win) > b.
- D2 weighting: the owner asked what the books say. AFML is weighted throughout (s.4.6, Snippet 7.4, s.9.4,
  s.10.3), which reverses my earlier recommendation. The question of also weighting the live calibrator went to the
  owner.

## Gate D v4.1 settled (2026-11-04, runbook Revision 45)

Owner approved:
- D2 weighted throughout;
- the live calibrator fitted with the AFML return weights (D082);
- D1 acting when the calibrated P(win) > b.

Gate D v4.1 is in force for the next edge. The 4 RECERT edges are archived and are not re-judged.
