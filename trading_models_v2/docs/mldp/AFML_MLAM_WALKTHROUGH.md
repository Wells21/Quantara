# AFML / MLAM chapter-by-chapter walkthrough — handover ledger

**This file is the source of truth for where Quantara stands and what to do next.**
Anything in a Claude memory folder (`~/.claude/...`) is machine-local and is lost with the
VPS; everything a new session needs is here or linked from here. Update this file at the end
of every working step.

Last updated: 2026-10-31 (Ch.1-19 signed off (Ch.19 commit c07bf5df); AFML Ch.20 closed with no change (§4T); Ch.21 closed with no change (§4U); Ch.22 closed with no change (§4V) -- AFML complete; runbook reviewed and brought up to date (Revision 29, §4V.1); MLAM next; GEOMETRY RULE D050-A in force; runbook Revision 28).

## 1. What we are doing

The owner wants Quantara to follow López de Prado's *Advances in Financial Machine Learning*
(AFML, 2018) and *Machine Learning for Asset Managers* (MLAM, 2020) as closely as possible, with
every deviation named, justified and recorded. Both books were re-read in full on 2026-10-22.
Since then we go through them **one chapter at a time**: AFML Ch.1 → Ch.22, then MLAM Ch.1 → Ch.8
and Appendix A.

### Working rules (set by the owner)

1. **One chapter at a time.** For each chapter: explain what it says section by section (cite
   book pages, never from memory; book text lives in the PDFs listed in
   `docs/mldp/QUANTARA_MLDP_DOCTRINE.md` / the blueprint), check Quantara's actual code and data
   against it, list deviations, and let the owner decide implement / defer / reject. Implement
   only what the owner approved. Close the chapter before moving on.
2. **Forward references.** When the current chapter needs a section of a later chapter, read
   that section (and what it depends on) and implement it now; re-check it when its own chapter
   comes up. Do not leave the current chapter half-done.
3. **Dynamic, not hardcoded.** Edges, assets, versions and thresholds are read from manifests,
   ledgers and edge code at run time. No per-edge lists in code or config.
4. **Deviations** go into `config/mldp_deviation_register.json` (D001…), with book pages.
5. **Standing constraints:** never change `portfolio_live/live_settings.py` /
   `local_live_settings.py` without the owner's approval; never edit `.gitignore` without telling
   the owner exactly what is added; commit only when asked; never hard-delete research folders
   (archive); research runs at BelowNormal priority.
6. **AFML vs MLAM (owner rule 2026-10-24).** MLAM (2020) is the newer book. Where MLAM explicitly
   revises an AFML method, follow MLAM in full (all of its steps, not a partial port) and record
   which AFML section it replaces; where MLAM is silent, AFML stays the reference. AFML tools that
   MLAM does not reject may still run as diagnostics.
8. **Follow the book; deviations need prior approval (owner standing rule, 2026-10-24).** Implement
   what AFML/MLAM say by default (parameters, sizes, experiments, formulas, steps). Verbatim copying
   is not required: when Quantara's data or output gives a good reason to differ, PROPOSE the
   deviation first with (1) the book text and page, (2) what to do instead, (3) the evidence from
   our data and the reason, and do it only after the owner approves. Never deviate first and report
   later. Approved deviations go in the register (D0xx) and stay approved; new ones need a fresh yes.
7. **Parallel by default (owner rule 2026-10-24).** When there is CPU headroom, run independent
   jobs (per edge / per asset) in parallel at BelowNormal priority with capped math threads;
   serialise only jobs that share state.

## 2. Current system state

- **Live trading is STOPPED** (owner decision 2026-10-23) until the walkthrough is finished.
- Before going live again:
  1. re-run allocator ingestion (`portfolio_allocator/01_ingest_pass_edge_manifests.py` onward) —
     the 6 `edge.yaml` files changed (thesis blocks), so their hashes no longer match the
     allocator-ingested hashes checked by `portfolio_live/00_build_live_deployment_manifest.py`;
  2. rebuild the live manifest (`portfolio_live/00_...`), then
     `python portfolio_live/edge_versions.py` to register it (version tracking, §4.3).
- Live book (live_manifest.json, until the allocator is re-run): 6 edges — XAUUSD rangestructure_long / volatilitycarry_long, JPN rangestructure_long /
  volatilitycarry_long, GBPJPY dailyrangecompletionfade_long, HK drawdownhazardcontinuation_short.
  `config/edge_universe.json` now holds 5 (XAUUSD volatilitycarry_long removed by Gate E 2026-10-23).
  Certified 2026-10-17 (RUNBOOK v5.3, `Quantara_Runbook.docx` Rev 7 Section 6A), deployed 2026-10-19.

## 3. Chapter status

| Chapter | Status |
|---|---|
| AFML Ch.1 Financial ML as a distinct subject | **complete 2026-10-23** (signed off) — see §4 |
| AFML Ch.2 Financial data structures | **complete 2026-10-23** (signed off) — see §4B; runbook Revision 9 |
| AFML Ch.3 Labeling | **complete 2026-10-23** (signed off) — see §4C; runbook Revision 10 |
| AFML Ch.4 Sample weights | **complete 2026-10-23** (signed off) — see §4D; runbook Revision 11 |
| AFML Ch.5 Fractionally differentiated features | **complete 2026-10-23** (signed off) — see §4E; runbook Revision 12 |
| AFML Ch.6 Ensemble methods | **complete 2026-10-24** (signed off) — see §4F; runbook Revision 13 |
| AFML Ch.7 Cross-validation in finance | **complete 2026-10-24** (signed off) — see §4G; runbook Revision 14 |
| AFML Ch.8 Feature importance (+ MLAM Ch.2-4, 6 forward) | **complete 2026-10-24** (closed by the owner) — see §4H; runbook Revisions 15-16 |
| AFML Ch.9 Hyper-parameter tuning with CV | **complete 2026-10-24** (signed off) — see §4I; runbook Revision 17 |
| AFML Ch.10 Bet sizing | **complete 2026-10-26** (signed off) — see §4J; D047-D049; runbook Revision 18 |
| AFML Ch.11 The dangers of backtesting | **complete 2026-10-26** (signed off) — see §4K; D050-D051; runbook Revision 19 |
| AFML Ch.12 Backtesting through cross-validation | **complete 2026-10-26** (signed off) — see §4L; D052; runbook Revision 20 |
| AFML Ch.13 Backtesting on synthetic data | **complete 2026-10-26** (signed off) — see §4M; D053; runbook Revision 21 |
| AFML Ch.14 Backtest statistics (+ MLAM Ch.8) | **complete 2026-10-27** (signed off) — see §4N; D054; runbook Revision 22 |
| AFML Ch.15 Understanding strategy risk | **complete 2026-10-27** (signed off) — see §4O; D055, D056; runbook Revision 23 |
| AFML Ch.16 Machine learning asset allocation (+ MLAM Ch.7) | **complete 2026-10-30** (signed off) — see §4P; D010, D057-D060; runbook Revision 25 |
| AFML Ch.17 Structural breaks | **complete 2026-10-30** (signed off) — see §4Q; D061-D065; runbook Revision 26 |
| AFML Ch.18 Entropy features | **implemented 2026-10-31** (owner approved (a)-(f)) — see §4R; D066-D070; runbook Revision 27 |
| AFML Ch.19 Microstructural features | **implemented 2026-10-31** (owner approved (a)-(e)) — see §4S; D071-D073; runbook Revision 28 |
| AFML Ch.20 Multiprocessing and vectorization | **closed 2026-10-31, no change** (owner: "nothing should be done here") — see §4T |
| AFML Ch.21 Brute force and quantum computers | **closed 2026-10-31, no change** (owner: "Agreed") — see §4U |
| AFML Ch.22 High-performance computational intelligence | **closed 2026-10-31, no change** (owner: "Agreed") — see §4V |
| MLAM Ch.1 Introduction | CLOSED 2026-11-02 -- §4W; THEORY CHECK RULE D075 (Gate 0 before Gate A), runbook Rev 32 |
| MLAM Ch.2 Denoising and detoning | **complete 2026-11-02** (owner: 'Approved 1, 2 and 3') -- see §4X; D076; runbook Revision 34 |
| MLAM Ch.3 Distance metrics | **complete 2026-11-02** (owner: 'Close Ch.3') -- see §4Y; no change; runbook Revision 35 |
| MLAM Ch.4 Optimal clustering | **complete** 2026-11-03 -- D077 Step 01 best-of-20 ONC restarts; trial ONC unchanged; runbook Rev 36 (§4Z) |
| MLAM Ch.5 Financial labels | **complete 2026-11-03** (owner: "Approved, close Ch.5") -- see §4ZA; no change (T3 seasonal barriers rejected by the pre-registered rule); runbook Revision 37 |
| MLAM Ch.6 Feature importance analysis | **complete 2026-11-03** (owner: "I approve") -- see §4ZB; no change (D041, D042, log loss stand; T4 mwfl 0 worse on real edges); runbook Revision 38 |
| MLAM Ch.7 Portfolio construction | **complete 2026-11-03** (owner: "Approved, close Ch.7 without the extra") -- see §4ZC (and §4P); no change (HRP stays, D060; T3 no NCO candidate); runbook Revision 39 |
| MLAM Ch.8 Testing set overfitting | **complete 2026-11-03** (owner: "Approved, close Ch.8") -- see §4ZD (and §4N); no change (D054 stands); runbook Revision 40 |
| MLAM App. A Testing on synthetic data (+ App. B proof) | **complete 2026-11-03** (owner: "Approved, close Appendix A") -- see §4ZE; no change; runbook Revision 41. **MLAM walkthrough complete; AFML + MLAM book walkthrough COMPLETE** |

Known items already waiting for later chapters (from the 2026-10-22 audit): full Ch.10 bet sizing
(D017), CPCV path Sharpe distribution (Ch.12), Ch.13 optimal trading rules on synthetic data,
DSR with the full trial count + FWER (Ch.14; 07b used N=2), bet merging of overlapping same-side
bets (Ch.14 p.196), strategy-risk deployment check (Ch.15; 4/6 edges fail the 5% rule on backtest),
time decay (Ch.4), features stacking (Ch.8), F1 scoring (Ch.9), long bias (5/6 long),
recent-performance feature (MLAM p.69).

## 4. AFML Ch.1 — decisions and implementation

Book: §1.3.1 production chain (p.6-9), §1.3.1.3 strategist must state the economic mechanism
(p.7), §1.3.1.4 backtester accounts for all trials (p.8), §1.3.1.6 *cursus honorum*: embargo,
paper trading, graduation, re-allocation, decommission (p.8-9), Table 1.2 pitfalls (p.13).

| # | Decision (owner, 2026-10-23) | Status | Where |
|---|---|---|---|
| 1 | Thesis card per edge (mechanism, who loses and why, regimes, counterfactuals, falsifiers); required before promotion | DONE — owner approved all 6 cards 2026-10-23 (`review_status: owner_approved_2026-10-23`) | `thesis:` block at the end of each `assets/*/edges/*/edge.yaml`; enforced by `shared/edges/edge_yaml_contract.py::validate_thesis_block` for status deployability_pass / allocator_candidate / live_selected |
| 2 | Embargo = sealed holdout run on data after each edge's backtest; standing gate for every new edge (Gate E) | DONE 2026-10-23 — tool built, rule D023, 6 edges judged, XAUUSD volatilitycarry removed | §4.1 |
| 3 | Prop challenge account = paper trading + graduation | DONE (recorded) | D018 |
| 4 | Re-allocation: shrink only, on any account, no ramp-up | DONE (applied live) | D019, §4.2 |
| 5 | Decommission rule + drift dashboard brought up to date | DONE (applied live; off is not permanent) | D021, D022, §4.2, §4.3 |
| 6 | No parallel old/new versions | DONE (recorded) | D020 |
| — | Shrink/off applied to live sizing automatically (AFML p.9) | DONE — owner approved 2026-10-23 | §4.2 |
| — | Runbook entries for Ch.1 | DONE — `Quantara_Runbook.docx` Revision 8 (6A Steps 4B/12C, 16C, Appendix 25; editor `docs/tools/edit_runbook_rev8.py`) and ledger key `RUNBOOK_v5.3_ADDENDUM_2026-10-23_AFML_CH1` in `config/us500_edge_search_ledger.json` | runbook |

### 4.1 Holdout (embargo) data

- Downloader: `shared/data/download_holdout_ticks.py` (same fetch as the owner's
  `auto_downloader.py`; instrument table inside; UTC-aware; resumable).
  `--parity` re-fetches the newest full week of `assets/{A}/raw/ticks/` and compares tick for tick;
  `--download` refuses an asset whose parity check has not passed.
- Parity 2026-10-23: PASS on all 10 assets (identical timestamps, prices and volumes).
  Reports: `assets/{A}/holdout/parity_check.json`.
- Holdout window per asset: from the last raw tick (exclusive) to **2026-10-23 00:00 UTC**
  (exclusive). Ticks in `assets/{A}/holdout/ticks/` (git-ignored, ~9 GB);
  `assets/{A}/holdout/holdout_manifest.json` lists files + sha256.
  Log: `logs/holdout_download_20261023.log`. If interrupted, re-run
  `python ./shared/data/download_holdout_ticks.py --download` (finished chunks are skipped).
- Download completed 2026-10-23: 195 weekly files, 181.2M ticks, ~11 GB. Checked: every asset's
  first holdout tick is after its last raw tick, no duplicate timestamps, no out-of-order ticks,
  no empty files. Windows start 2026-04-07 (XAUUSD), 2026-04-20 (BTCUSD, NASDAQ), 2026-07-05/09
  (the other 7) and end 2026-10-22 23:59 UTC.
- **Sealed:** research code never reads `holdout/`. **One look per frozen edge version:** the
  holdout result is recorded as a trial; changing an edge after seeing it burns the holdout for
  that edge (MLAM §1.4.2 p.8 test-set overfitting).
- Holdout replay design (2026-10-23): replay the holdout ticks through the same causal path live
  uses (streaming bars with causal thresholds, FFD/HMM continuation with its past-only refit
  schedule, edge runtime features/events/model rows, frozen meta-model + calibrator, live threshold
  and AFML size), resolve with Task 2A exact execution on holdout ticks under the broker cost model.
- **Gate E rule (owner decision 2026-10-23, D023 — supersedes the first PSR rule):** the holdout must be
  *consistent with the backtest* (AFML p.8). Windows of the holdout's span, stepped every 7 days through the
  backtest (meta-approved bets at the live threshold, broker costs, >= 20 bets each); share_as_bad = fraction
  of windows with mean R <= the holdout's mean R. PASS if >= 30 holdout bets and share_as_bad >= 5%; FAIL if
  < 5%; INCONCLUSIVE below 30 bets (may go live under the shrink rule). PSR reported as a diagnostic only.
  Rationale: the first rule compared 3.5-6.5 months with a 4-year average; the backtests themselves lose over
  3-6 month stretches in 12-38% of periods (one path among many, p.8, p.155; CPCV path distribution, p.166).
  Every holdout look is a trial (one look per frozen version); a failed edge is removed from
  `config/edge_universe.json` and its folder is left untouched. When >= 3 months of newer data exist, the old
  holdout becomes research data and a fresh holdout is downloaded.
- **Tool:** `shared/research/holdout_replay.py` (run from trading_models_v2, venv312):
  `setup --assets ...` (isolated workspace at `../../Quantara_holdout_workspace/trading_models_v2`, outside
  git: code copied, raw + holdout ticks hard-linked) → `build --assets ...` (bars, FFD, HMM with the repo's
  recorded manifest parameters; frozen_d checked) → `export --edges ...` (each edge's own research export)
  → `verify --edges ...` (rows up to the research end must be IDENTICAL to the repo) → `replay --edges ...`
  (frozen model + calibrator, live threshold, Task 2A exact execution with broker costs and live entry
  delay) → `judge --edges ...` (report in `portfolio_live/outputs/gate_e_holdout/`).
- **First Gate E run 2026-10-23** (report `portfolio_live/outputs/gate_e_holdout/gate_e_holdout_20261023T*.json`,
  trials logged in each edge's `artifacts/model/*_research_trial_ledger.jsonl`, trial_type `gate_e_holdout`).
  Workspace verified IDENTICAL to the repo for all 6 edges (bars, FFD, HMM, vol cache, events, model rows).
  Final verdicts (consistency rule, report `gate_e_holdout_20261023T102834Z.json`): HK drawdownhazard PASS
  (17.8% of backtest windows as bad), JPN rangestructure PASS (16.3%), XAUUSD rangestructure PASS (19.9%),
  GBPJPY dailyrangecompletionfade PASS narrowly (8.0%), JPN volatilitycarry INCONCLUSIVE (3 bets — the model
  approved 1% of holdout events), **XAUUSD volatilitycarry FAIL (0.0%: worse than any 6-month backtest period) —
  removed from `config/edge_universe.json` 2026-10-23** (folder untouched; backup
  `config/edge_universe.json.bak_20261023_pre_gate_e`). Holdout mean R per bet: XAU range +0.02, HK -0.05,
  JPN range -0.12, GBPJPY -0.19, XAU volcarry -0.22. Backtest expectancy is regime-concentrated (both XAUUSD
  edges earned mostly in 2024-26; HK in 2022-23 and 2026). Consistent does not mean good: Ch.14 (DSR) and
  Ch.15 (strategy risk) still have to judge whether these backtests are good enough at all.
  Lessons fixed in code: holdout tick files must use the raw naming `{asset}_ticks_YYYY-MM-DD_to_YYYY-MM-DD.csv`
  (edges parse it); the TABLE spread model needs the minute vol cache rebuilt in the workspace (now a build step);
  events within the vertical horizon of the data end are censored (cannot be judged yet).
- Earlier plan note: build holdout bars (causal thresholds, raw ticks as warm-up only, FFD/HMM/model/calibrator
  frozen — verify each is applied without refit), score the 6 edges once, run
  `portfolio_live/decommission_rule.py --bets-csv <holdout bets> --sample-label holdout`, and test
  the thesis-card falsifiers/counterfactuals.

### 4.2 Decommission / shrink rule

- `portfolio_live/decommission_rule.py`, config `portfolio_live/config/decommission_rule.json`,
  tests `tests/test_decommission_rule.py` (`python -m unittest tests.test_decommission_rule`).
- Statistic: Probabilistic Sharpe Ratio (AFML §14.7.2 p.203; 0.95 level p.205-206), per bet.
  Off if PSR[SR*=0] ≤ 0.05. Otherwise allocation by PSR[SR*=backtest per-bet SR]:
  >0.25 → 100%, >0.15 → 80%, >0.10 → 60%, >0.05 → 40%, else 20%. No verdict below 30 bets.
  Recomputed every run, so allocation recovers when evidence improves (no ratchet).
- Benchmark = meta-approved backtest bets at the live threshold with broker costs
  (`portfolio_allocator/outputs/v2_representative_trade_ledger_core_plus_watchlist.csv`,
  AFML p.211). §15.4 strategy risk is reported as a diagnostic only (it measures design fragility,
  not decay; the Ch.15 deployment check owns it).
- First run 2026-10-23: all 6 edges insufficient_sample (0-25 bets of the current versions).
- **Applied live (owner approval 2026-10-23):** `portfolio_execution_bot.build_order_plan` calls
  `edge_decommission_allocation()` → `decommission_rule.live_allocations()` (recomputed at most
  hourly; last-good verdicts in `outputs/decommission/latest_verdicts.json` if a recompute fails;
  fail closed if there are none — retried as a transient wait). Risk is multiplied by the allocation
  in `compute_candidate_sizing`. Fewer than 30 bets → 100%.
- Shadow bets are charged the active broker's modelled spread, swap and commission (same cost model as
  allocator 00d) — `shadow_bets.apply_broker_costs`.
- **Off is not permanent (D022):** an off edge places no orders; its approved signals are recorded
  as shadow bets (`portfolio_live/shadow_bets.py`), resolved on live bars with the signal's own
  barriers, and judged with the real bets. When PSR[0] climbs back above 0.05 the edge trades again
  at the level its PSR vs backtest gives.
- Tests: `tests/test_decommission_rule.py`, `tests/test_decommission_live.py` (25 tests). Bot
  backup before this change: `logs/portfolio_execution_bot.py.bak_20261023_pre_decommission`.

### 4.3 Version tracking and drift dashboard

- `portfolio_live/edge_versions.py`: version = sha256 of each edge's meta-model + calibrator
  (paths from `live_manifest.json`). Registry `portfolio_live/config/edge_version_registry.json`
  maps every live-manifest hash to the version each edge ran. Closed trades carry
  `live_manifest_hash`, so only bets of the current version are judged. Run
  `python portfolio_live/edge_versions.py` after every manifest rebuild.
- Drift dashboard (`portfolio_live/drift_dashboard/`, build with
  `python -m portfolio_live.drift_dashboard.build_report`): fixed 2026-10-23 — reads hours-based
  geometry constants, finds each edge's Gate C winner row across all batch summaries, compares
  live trades with meta-approved backtest bets, scopes to the current version, and shows the
  Decommission / Shrink panel. Design ledger: `docs/dashboards/LIVE_VS_BACKTEST_DRIFT_DASHBOARD_PLAN.md`.

## 4B. AFML Ch.2 — decisions and implementation

Book: s.2.2 data types (p.23-25), s.2.3 bars (time p.26, tick p.26-27, volume/dollar p.27-28, information-driven
p.29-32), s.2.4 multi-product series (ETF trick p.33-34, PCA weights p.35-36, single futures roll p.36-37),
s.2.5 sampling (CUSUM p.38-40), exercises p.40-41. Owner rule from this chapter: **every check and fix covers all 10
assets**, and **one bar type for all 10** (quote-tick bars) so allocator/dashboard/live stay on one contract.

| # | Decision (owner, 2026-10-23) | Status | Where |
|---|---|---|---|
| 1 | Bad-quote filter, research and live alike; quality report per asset; rebuild bars | DONE (D026) — 1 episode in 1.55 billion ticks (JPN 2025-08-26); curated layer built for all 10; JPN and USOIL bars/FFD/HMM/vol cache rebuilt (others unchanged, byte-identical ticks); old bars archived at `../archive_bars_pre_curation_20261023` | `shared/data/tick_curation.py` (QuoteTickQualityFilter, curated layer), `config/asset_data_policy.json`, `AssetLayout.tick_root` follows the policy (fails closed if curated ticks are missing), live: `portfolio_live_dashboard_orchestrator.apply_tick_quality_filter` before daily tick counts; tests `tests/test_tick_curation.py` (run on venv39: the orchestrator import runs Phase C.4 validation) |
| 2 | USOIL roll adjustment | DONE (D026) — multiplicative backward (Snippet 2.3 returns preserved; additive Snippet 2.2 rejected: -30.64 USD cumulative shift inflated 2022 returns ~1.5x); 43 rolls in research data, 11 months undetected | roll dates detected from early-stop daily breaks (`detect_rolls`); undetected months reported, never guessed; Moneta live roll handling must be checked before any USOIL edge goes live |
| 3 | Bar-quality report (Exercise 2.1) for all 10 assets on every bar rebuild | DONE — quote-tick bars beat equal-count time bars on normality for 8/10 and variance stability for 7/10; US500/DAX tails worse (session gaps); reports in each asset's bars/standard_quote_tick/bar_quality_report.json | `shared/bars/bar_quality_report.py` |
| 4 | Deviations: quote-tick bars only (D024); no ETF trick/PCA weights (D025) | DONE | register |
| 5 | Event clustering (CUSUM reset) | deferred to Ch.4 (overlap, uniqueness) | — |
| — | Re-certify JPN edges (their research data contained the 2025-08-26 bad quotes; their research datasets now predate the cleaned bars) | planned ONCE after Ch.3-4, as a new version with a fresh holdout; until then the JPN edges are not deployable | — |

Findings: (a) 2025-08-26 06:24-09:20 UTC Dukascopy JPN quotes ~15,150 instead of ~42,400 entered 10 bars, Event_Vol
(~60x normal), FFD/HMM and ~40 model rows per JPN edge; the only corrupt episode found by the 15% scan across all 10
assets (BTCUSD/USOIL flags were real moves). (b) USOIL monthly roll jumps (-0.5% to -4.7%) at an 80-minute break
after quotes stop ~20 minutes early; 47 of 58 months detected. (c) Exercise 2.1: quote-tick bars beat time bars on
XAUUSD/GBPJPY; on JPN/HK they cut serial correlation but have fatter tails.
Holdout for assets without an edge: ticks are downloaded for all 10; the replay runs when an edge reaches Gate E.

## 4C. AFML Ch.3 — labeling audit and decisions (2026-10-23)

Book pages = pdf page - 26 (AFML pdf 69-82 = pp.43-56). Audit script (read-only): stats below were computed from each
edge's `artifacts/research_dataset/*_mldp_execution_resolution.csv`, calibrated OOF predictions and
`model_training_manifest.json`. Only 6 edge folders exist (GBPJPY, HK, JPN x2, XAUUSD x2); the other 4 assets have no
edge, and every future edge on any of the 10 assets uses the same shared labelers (model_rows template,
`shared/labels/calendar_time_barriers.py`, `shared/execution/executable_target_dataset.py`, `shared/research/v3_horizon_lab.py`).

| § / snippet (page) | Book | Quantara | Status |
|---|---|---|---|
| 3.2 fixed horizon (pp.43-44) | avoid fixed tau on time bars; ignores path/stops | not used anywhere | matches |
| 3.3 Snippet 3.1 getDailyVol (p.44) | 1-day simple returns, ewm(span=100).std() (adjust=True) | H-hour log returns (H = Gate A grid 1-8h), ewm span 20/50/100, adjust=False, floor 1e-8 | small deviation, already documented in calendar_time_barriers.py (corr 0.9998); H is a searched parameter |
| 3.4 Snippet 3.2 applyPtSlOnT1 (pp.45-46) | path of closes, first touch of pt/sl, barriers multiplicative on entry | training label: tick-mid replay (Task 2A, D016); model_rows: bar High/Low with tick-mid tie-break, sl_first fallback (1 fallback in 26,000 rows) | matches in method; stricter path (intrabar) |
| 3.4 vertical label (p.45) | sign of return (preferred) or 0 | edges + Task 2A: int(r > 0) | matches; **v3_horizon_lab writes meta_label=0 on vertical exits** (unused diagnostic column — no gate reads it) |
| 3.4 eight configurations (p.46) | [1,1,1] standard | [1,1,1] everywhere, PT 2 / SL 1 | matches |
| 3.5 Snippet 3.3 minRet (p.48) | drop events with trgt <= minRet | no minRet (floor 1e-8 only) | deviation; measured cost per R by Event_Vol quintile: 0.02-0.07R, lowest quintile not cost-dominated |
| 3.5 Snippet 3.4 vertical (p.49) | first bar at/after t0 + numDays | first bar at/after t0 + H hours; events past data end dropped | matches |
| 3.6 Snippets 3.6/3.7 meta-labeling (pp.50-51) | side from primary, asymmetric barriers, bin = 1 if side*ret > 0 else 0 | side-specialised rule primaries, PT2/SL1, target int(label_r > 0) | matches |
| 3.7 F1 (pp.51-53) | high-recall primary; meta-model raises F1 by removing false positives | CPCV F1 of tuned model vs approve-all: -0.0027, -0.0030, -0.0010, +0.0015, -0.0022, 0.0000; OOF at live thresholds XAUUSD approves 99-100% (no filtering), JPN 10-13% | **finding** — meta-models do not raise F1; economic value is judged by Gates C/D/E instead. Root cause sits in Ch.9 (F1 scoring with base rates 38-48%) and Ch.14 |
| 3.7 long-only / short-only (p.53) | separate ML strategies per side allowed | side-specialised edges | matches |
| 3.8 quantamental (pp.53-54) | meta-label any white-box primary | rule primary + thesis cards (Ch.1) | matches |
| 3.9 Snippet 3.8 dropLabels (p.54) | drop classes < 5% | binary, positives 37.7-48.2% | not needed |
| Exercises 3.4/3.5 (p.55) | report primary vs secondary precision/recall/F1 | not reported per edge | gap — proposal below |

Owner decisions (2026-10-23, all four approved):

| # | Item | Decision | Where |
|---|---|---|---|
| 1 | v3_horizon_lab vertical exits labeled 0 | FIXED: `label = int(realized_r > 0)` (Snippet 3.7). Column was never read by a gate (Gate A-C use realized/exec R, which was always signed), so no past result changes | shared/research/v3_horizon_lab.py `materialize_horizon` |
| 2 | minRet | not added; recorded as deviation, re-checked each certification | D027 |
| 3 | meta-models do not raise F1 | recorded now; fixed in Ch.9 (scoring), Ch.10 (sizing), Ch.14 (metrics) | D028 |
| 4 | Exercise 3.4/3.5 report | `shared/research/label_audit.py` (writes artifacts/label_audit/label_audit.json per edge); runbook 6A Step 12D | Revision 10, Appendix 27 |

Per-edge numbers (eligible rows; OOF at final_live_threshold):

| edge | rows | pos rate | PT / SL / vert | vert>0 | Event_Vol median bps | approve | precision primary -> meta | recall | F1 primary -> meta |
|---|---|---|---|---|---|---|---|---|---|
| GBPJPY dailyrangecompletionfade_long | 4049 | 0.381 | .36/.62/.02 | .90 | 30 | .68 | .381 -> .405 | .72 | .551 -> .518 |
| HK drawdownhazardcontinuation_short | 4205 | 0.384 | .37/.61/.02 | .91 | 76 | .73 | .385 -> .407 | .77 | .556 -> .532 |
| JPN rangestructure_long | 4463 | 0.405 | .36/.58/.06 | .73 | 59 | .10 | .406 -> .428 | .10 | .578 -> .166 |
| JPN volatilitycarry_long | 4894 | 0.377 | .37/.62/.02 | .67 | 61 | .13 | .377 -> .483 | .17 | .548 -> .251 |
| XAUUSD rangestructure_long | 4379 | 0.482 | .21/.40/.40 | .69 | 47 | .99 | .481 -> .483 | .99 | .650 -> .650 |
| XAUUSD volatilitycarry_long | 4606 | 0.466 | .21/.41/.38 | .67 | 44 | 1.00 | .467 -> .467 | 1.00 | .636 -> .636 |

## 4D. AFML Ch.4 — sample weights audit and decisions (2026-10-23)

Book pages = pdf - 26 (pdf 85-99 = pp.59-73). Code: `shared/models/afml_sample_weights.py` (Snippets 4.1/4.2/4.10,
vectorised), `shared/models/sequential_bootstrap.py` (Snippets 4.3-4.5, checked against the s.4.5.3 worked example),
each edge's `research/02_train_meta_model_optuna.py` and `03_generate_oof_predictions.py` (per-edge copies of one template).

| § / snippet (page) | Book | Quantara | Status |
|---|---|---|---|
| 4.2 overlapping outcomes (pp.59-60) | allow t_i,1 > t_i+1,0; never restrict horizons to avoid overlap | overlap allowed | matches |
| 4.3 Snippet 4.1 concurrency (pp.60-61) | c_t = labels spanning bar t, [t0,t1] inclusive | same, prefix sums on the quote-tick bar clock | matches |
| 4.4 Snippet 4.2 average uniqueness (pp.61-62) | mean of 1/c_t over the label's life; computed on all events | same | matches; **t1 used is `gross_t1` (bar labeler) while the training label is Task 2A `label_t1`** — differs by ~1 bar on 4-6% (GBPJPY/HK) and ~40% (XAUUSD, vertical exits) of rows |
| 4.5 bagging + uniqueness (pp.62-63) | do not drop overlaps; max_samples = avgU or better | sequential bootstrap | matches |
| 4.5.1-4.5.3 sequential bootstrap (pp.63-66) | draw prob ∝ avg uniqueness if added | exact (worked example 5/14, 3/14, 6/14 reproduced); used in the final bagged model (Step 02) and every OOF fold model (Step 03); Optuna search uses one tree per fold (Ch.9 item) | matches |
| 4.5.4 Monte Carlo (pp.66-68) | sequential > standard uniqueness | real edges: 0.207->0.231, 0.182->0.198, 0.198->0.215, 0.242->0.267 (one draw each) | confirmed |
| 4.6 Snippet 4.10 return attribution (pp.68-69) | w = abs(sum r_t / c_t), scaled to sum I; drop neutral labels | same; binary labels | matches |
| 4.7 Snippet 4.11 time decay (pp.70-71) | piecewise-linear decay on cumulative uniqueness, parameter c | not implemented (= c = 1, no decay) | deviation |
| 4.8 class weights (pp.71-72) | class_weight='balanced'; 'balanced_subsample' when bagging | balanced, recomputed per bootstrap draw (= balanced_subsample) — **but applied twice in Steps 02/03** (tree class_weight='balanced' AND compute_sample_weight('balanced') inside sample_weight; sklearn multiplies them) | **bug** |
| Ex 4.1/4.2 (p.72) | concurrency, AR(1) of uniqueness | AR(1) 0.57-0.65 (significant: neighbouring events share bars) | reported here |
| Ex 4.3 (p.72) | OOB accuracy is inflated | OOB not used anywhere | matches |

Owner decisions (2026-10-23, all four approved):

| # | Item | Decision | Where |
|---|---|---|---|
| 1 | class balance applied twice in Steps 02/03 | FIXED in all 6 edges: estimator's class_weight='balanced' only; sample_weight = AFML return weight x time decay. Retrain once after Ch.6/7/9 (new versions, Gates C/D/E) | D030; `docs/tools/patch_afml_ch4_sample_weights.py`; `tests/test_afml_ch4_weights.py` |
| 2 | weight spans on gross_t1 | FIXED: label_t1 (fallback gross_t1) snapped to the bar clock, Steps 01-03 + shared module (contract v2_label_t1). Purge still gross_t1 -> Ch.7 | D030; `shared/models/afml_sample_weights.py` |
| 3 | time decay | Snippet 4.11 implemented (`get_time_decay`), wired with TIME_DECAY_C = 1.0; c chosen in Ch.9 | D029 |
| 4 | event clustering (Ch.2 item) | closed: handled by uniqueness weights + sequential bootstrap (book p.62); label audit now reports uniqueness, effective N, overlap share, AR(1), standard vs sequential bootstrap | `shared/research/label_audit.py` (v2) |

Verified after the patch (XAUUSD x2, GBPJPY, HK): shared and flat_colab weight tables identical; make_train_weights equals
the return weight; no-information leaf P(win) 0.495-0.503 (book 0.50). Bagging sync tests and 7 new tests pass.
Effective independent outcomes (label audit): GBPJPY 965, HK 867, XAUUSD rangestructure 967, XAUUSD volatilitycarry 1,282;
JPN = STALE_BAR_CLOCK.

Double class weighting, shown on a leaf with no information and 38% positives: no weight -> P(win) 0.38; balanced once
-> 0.50 (book); Quantara (twice) -> 0.62. All 6 edges' Steps 02 and 03 do it; Step 01 (XGBoost MDA) weights once.

Overlap / clustering (Ch.2 deferred item): 86-92% of events start while an earlier event of the same edge is still
open; median gap between events 0.8-1.6 h vs median hold 12-18 h; mean uniqueness 0.19-0.28, so the effective number of
independent outcomes (sum of uniqueness) is roughly 800-1,300 per edge against 3,400-4,600 rows.

JPN: 1,049 / 1,132 research rows no longer map onto the rebuilt curated JPN bar clock — confirms the JPN re-certification.

## 4E. AFML Ch.5 — fractionally differentiated features audit and decisions (2026-10-23)

Book pages = pdf - 26 (pdf 101-115 = pp.75-89). Code: `shared/ffd/fractional_differentiation.py` (uses the `fracdiff`
0.9.0 package: `FracdiffStat` to pick d, `Fracdiff` to apply it), `shared/ffd/build_asset_ffd_features.py`,
`shared/regime/hmm_regime.py` (Close_FFD is one of the 5 HMM inputs), live `shared/live_streaming/continuation.py`.
Package source read: weights `(-1)^k binom(d,k)`; d = binary search on [0,1] (precision 0.01) for the minimum d whose
ADF p-value < 0.05, ADF = `statsmodels.adfuller(x)` defaults (regression 'c', autolag 'AIC', maxlag 12(n/100)^(1/4)).

| § / snippet (page) | Book | Quantara | Status |
|---|---|---|---|
| 5.1-5.3 motivation (pp.75-77) | integer differencing erases memory; find the minimum d that gives stationarity | FFD on log OHLC of every asset | matches in intent |
| 5.4 / Snippet 5.1 weights (pp.77-79) | w_k = -w_k-1 (d-k+1)/k | (-1)^k binom(d,k); identical to 1e-16 | matches |
| 5.4.3 convergence (p.80) | weights -> 0, alternate per int[d] | same coefficients | matches |
| 5.5.1 / Snippet 5.2 expanding window (pp.80-82) | drifts; replaced by FFD | not used | matches |
| 5.5.2 / Snippet 5.3 FFD (pp.82-84) | fixed width, drop weights with abs(w) < tau (default 1e-5) | fixed width = 1,000 bars (= tau ~1.4e-5 to 4.6e-5 at the frozen d); tau 1e-5 would need 1,400-4,000 bars; live window pinned to 1,000 by live settings | small deviation |
| 5.6 / Snippet 5.4 minimum d (pp.84-85) | min d whose FFD(log price) passes ADF at 95%, on the whole series (example: daily, maxlag=1, thres=.01) | min d on the FIRST 20,000 bars only (~1.3 years), ADF with AIC lags | **finding** |
| 5.6 memory check (p.85) | report corr(original, FFD) | min corr >= 0.80 enforced; actual 0.986-1.000 | matches |
| 5.7 conclusion (p.88) | experiment: cumsum any feature, FFD it, keep the min-d series | FFD only on OHLC | not adopted (suggestion) |
| Ex 5.4 (p.89) | FFD(FFD(x,d),-d) | recovers x (corr 0.9995, level offset from truncation) | checked |

Owner decisions (2026-10-23, 1-5 approved; ADF = library defaults; keep 1,000 window; cumsum not adopted):

| # | Item | Decision | Where |
|---|---|---|---|
| 1 | d chosen on first 20,000 bars | FIXED: min ADF-passing d (statsmodels defaults) on the full research history, frozen; `--frozen-d`; holdout replay passes the repo d | D031; `shared/ffd/*`, `shared/research/holdout_replay.py`; patch `docs/tools/patch_afml_ch5_ffd_full_history_d.py` |
| 2 | rebuild + ADF gate | FFD + HMM rebuilt for all 10 assets; build fails if an estimated d does not pass ADF; manifest records ADF full/halves; old artifacts archived at `../archive_ffd_hmm_pre_ch5_20261023` | logs/ch5_ffd_hmm_rebuild_20261023/ |
| 3 | 1,000 window vs tau 1e-5 | keep 1,000 (measured: features correlate 0.993-0.9996) | D032 |
| 4 | s.5.7 cumsum-then-FFD | not adopted | D033 |
| 5 | tests | Snippet 5.1 weights, Exercise 5.4 inverse, full-history selection, supplied-d path | `tests/test_afml_ch5_ffd.py` (venv312 + venv39) |

New frozen d (ADF p, full sample): BTCUSD 0.344 (0.042), DAX 0.359 (0.038), GBPJPY 0.258 (0.048), HK 0.172 (0.049),
JPN 0.352 (0.041), NASDAQ 0.336 (0.042), US500 0.344 (0.045), USDMXN 0.141 (0.050), USOIL 0.000 (0.030), XAUUSD 0.438
(0.047); min memory corr 0.970-1.000. All 10 manifests pass verify_asset_ffd_manifest; HMM manifests carry the new d.
All edges' research datasets now predate the asset FFD/HMM features: re-export + retrain in the planned batch.

Frozen d and full-history stationarity of the built Close_FFD (ADF p, statsmodels defaults, bar level):
BTCUSD d 0.258 p 0.29; DAX 0.188 p 0.66; GBPJPY 0.000 p 0.60 (Close_FFD = log price); HK 0.063 p 0.27; JPN 0.016 p 0.98;
NASDAQ 0.055 p 0.90; US500 0.023 p 0.94; USDMXN 0.211 p 0.005; USOIL 0.094 p 0.001; XAUUSD 0.211 p 0.92.
**8 of 10 assets' FFD features are not stationary** — the first 20,000 bars happened to be range-bound, so d came out
too small for the rest of the history. Consumers: raw Open/High/Low/Close_FFD are selected features of HK,
JPN rangestructure, XAUUSD rangestructure; all 6 edges use Close_FFD_Z/Momentum (local differences, roughly stationary
either way); the HMM regime features (all edges) take Close_FFD as an input.

Minimum d on the full research history (window 1,000): book Snippet 5.4 ADF spec (maxlag=1) at bar level 0.00-0.25;
statsmodels default ADF (what the code uses) BTCUSD 0.344, DAX 0.359, GBPJPY 0.258, HK 0.172, JPN 0.352, NASDAQ 0.336,
US500 0.344, USDMXN 0.141, USOIL 0.000, XAUUSD 0.438, with corr(log price, FFD) 0.970-1.000 (memory kept).
Book Snippet 5.4 literal (daily, thres .01, maxlag 1): 0.00-0.60, in line with Table 5.1 (all < 0.6).

## 4F. AFML Ch.6 — ensemble methods audit and decisions (2026-10-23/24)

Book pages = pdf - 26 (pdf 119-128 = pp.93-102). Code: `SequentialBootstrapBaggingClassifier` (inline copies in every
edge's Steps 02/03; shared copy `shared/models/sequential_bootstrap_bagging.py`), Step 02 Optuna (one tree per CPCV fold),
Step 01 feature surgery (XGBoost, 500 trees).

| § (page) | Book | Quantara | Status |
|---|---|---|---|
| 6.2 bias / variance / noise (pp.93-94) | ensembles cut variance and/or bias | — | concept |
| 6.3 bagging (p.94) | average of the N estimators' probabilities | mean predict_proba of the trees | matches |
| 6.3.1 variance (pp.94-95) | V = sigma^2 (rho + (1-rho)/N); bagging helps only while rho < 1 | rho_bar reported per edge 0.26-0.75 (in-sample); N = 100; projected variance at N = 1000 vs 100: 0.975-0.997 | matches; N below the book's 1000 example |
| 6.3.2 accuracy (pp.96-97) | bagging cannot rescue poor classifiers (p << 1/k), only lowers their variance | trees near chance (OOF AUC 0.52-0.56, D028) | consistent with D028 |
| 6.3.3 redundancy (pp.97-98) | overlapping labels -> sample ~avgU of the observations per estimator ("but not more"), or the sequential bootstrap; ignore OOB | sequential bootstrap, but each tree draws n rows (avgU ~0.21-0.28); OOB never used; purged CPCV instead of k-fold | **finding** (sample size) |
| 6.4 random forest (pp.98-99) | second randomness: random feature subset per split to decorrelate trees; max_features low; min_weight_fraction_leaf ~5%; class_weight balanced(_subsample); Snippet 6.2 clf1 = bagged entropy trees | entropy trees, max_features='sqrt', balanced, min_weight_fraction_leaf searched 0-0.05 — **but every tree is built with random_state=42**, so all trees draw the same feature subsets | **bug** |
| 6.4 PCA suggestion (p.99) | fit RF on a PCA of the features | not done | -> Ch.8 |
| 6.5-6.6 boosting vs bagging (pp.99-101) | bagging generally preferable in finance (overfitting) | final model bagged; Step 01 feature importance uses XGBoost (boosting) | -> Ch.8 |
| 6.7 bagging for scalability (p.101) | bag slow learners | n/a | n/a |
| Ex 6.4 (p.102) | trees vs features | deployed trees use 10-24 of 42-157 features | caused by the seed bug |

Owner decisions (2026-10-24, 1-5 approved):

| # | Item | Decision | Where |
|---|---|---|---|
| 1 | shared tree seed | FIXED: per-tree seeds from the seeded ensemble rng | D034; Steps 02/03 all edges + `shared/models/sequential_bootstrap_bagging.py`; `docs/tools/patch_afml_ch6_ensemble.py` |
| 2 | sample size per tree | round(avgU x n), avgU of the training set's own events (max_samples='avgU') | D034 |
| 3 | number of trees | N_ESTIMATORS_BAGGING = 1000 (Snippet 6.2) | D034 |
| 4 | stale ledger search space | Step 02 logs OPTUNA_SEARCH_SPACE; historical records corrected in the register | D035 |
| 5 | forward items | max_depth boundary + single-tree Optuna -> Ch.9; PCA + XGBoost Step 01 -> Ch.8 | — |

Verified on current data (patched Step 02, 1,000 trees): XAUUSD rangestructure 138 distinct root features, 157/157 features
used, 966 rows per tree, rho_bar 0.237; GBPJPY 39 root features, 49/50 used, 965 rows, rho_bar 0.642; 76 s per fit;
portable payload round trip identical. Tests: `tests/test_afml_ch6_ensemble.py` (venv312 + venv39) and the bagging sync test.

Seed bug evidence (deployed models): every tree random_state = 42; distinct root-split features over 100 trees:
GBPJPY 1 (all 100 trees split on the same feature), XAUUSD volatilitycarry 2 (98 on one), HK 6, JPN x2 5, XAUUSD
rangestructure 5. Reproduction on current data (100 trees, best params): XAUUSD rangestructure features used 21/157 with
the shared seed vs 88/157 with per-tree seeds; GBPJPY 11/50 vs 43/50; HK 27/157 vs 99/157.

Sample size per tree (per-tree seeds): rho_bar n rows -> avgU x n rows: XAUUSD rangestructure 0.470 -> 0.228;
GBPJPY 0.698 -> 0.659; HK 0.219 -> 0.194.

Also found: Step 02 logs its Optuna search space to the research-trial ledger as an XGBoost space (n_estimators,
learning_rate, ...) while it actually searches decision-tree parameters (max_depth 2-8, min_samples_leaf 5-200,
min_samples_split 10-400, min_weight_fraction_leaf 0-0.05, ccp_alpha 1e-6-1e-2) — stale provenance in every edge's ledger.
Best max_depth = 2 (the lower bound) on all 6 edges and Optuna tuning a single tree, not the bagged model -> Ch.9.

## 4G. AFML Ch.7 — cross-validation audit and decisions (2026-10-24)

Book pages = pdf - 26 (pdf 129-137 = pp.103-111). Code: every edge's Steps 01/02/03 (`purge_train_overlaps`,
`apply_embargo`, skfolio `CombinatorialPurgedCV` with purged_size = embargo_size = 0 generating the splits, then a
manual t1-overlap purge and a 100-observation embargo) and `shared/calibration/fit_sigmoid_calibrator_and_thresholds.py`
(Step 04, same scheme). No shuffled CV, `train_test_split`, `cross_val_score`, `KFold`, `TimeSeriesSplit` or
out-of-bag score anywhere in the edges, shared or allocator code.

| § / snippet (page) | Book | Quantara | Status |
|---|---|---|---|
| 7.1-7.2 goal (pp.103-104) | CV estimates generalisation error; train/test disjoint | CPCV (Ch.12) for tuning, OOF, calibration | matches |
| 7.3 why k-fold fails (pp.104-105) | serially correlated X + overlapping Y leak across folds; cure: purge, and avoid overfitting (early stopping, bagging with avgU / sequential bootstrap) | purge + embargo; depth-2 trees, min_weight_fraction_leaf, sequential bootstrap, avgU samples (Ch.6) | matches |
| 7.4.1 / Snippet 7.1 purge (pp.105-107) | drop train obs whose [t0,t1] overlaps any test label (3 conditions) | per-observation interval test `train_start <= test_end and train_end >= test_start` (= all 3 conditions), Steps 01-04 | matches; t1 = `gross_t1`, not the training label's `label_t1` |
| 7.4.2 / Snippet 7.2 embargo (pp.107-108) | embargo train obs right AFTER each test set; h ~ 0.01 T; check performance does not rise as k -> T | 100 observations after each contiguous test block = 2.2-2.5% of rows (more than 1%) | matches (stricter) |
| 7.4.3 / Snippet 7.3 PurgedKFold (pp.108-109) | purged, embargoed, unshuffled k-fold class | skfolio CPCV splits + manual purge/embargo (CPCV is Ch.12) | matches |
| 7.5 / Snippet 7.4 sklearn bugs (pp.109-110) | own cvScore: weighted neg log loss on the test fold with labels=classes_; avoid cross_val_score | cross_val_score never used; test-fold score is unweighted F1 | scoring -> Ch.9 |
| Ex 7.1-7.3 (pp.110-111) | shuffled vs unshuffled vs purged+embargo | measured below | done |
| Ex 7.4-7.5 (p.111) | second failure = multiple testing / selection bias | trial ledger; Chs.11-14 | -> Ch.11-14 |

Owner decisions (2026-10-24, 1-4 approved):

| # | Item | Decision | Where |
|---|---|---|---|
| 1 | purge t1 | label_t1 (fallback gross_t1) in Steps 01-04 | D036; `docs/tools/patch_afml_ch7_cv.py` |
| 2 | embargo size | max(100, ceil(1% of rows)) after each test block | D036 |
| 3 | standing leakage check | `shared/research/cv_leakage_audit.py`, runbook 6A Step 12E; first run: GBPJPY, HK, XAUUSD x2 all OK | D036 |
| 4 | weighted test-fold scoring | -> Ch.9 | D037 |

Verified: patched Step 02 CPCV and Step 04 calibration CPCV run on XAUUSD (10 folds each; embargo 100 at 4,379 rows,
200 at 20,000). Tests: `tests/test_afml_ch7_cv.py` (venv312 + venv39).

Purge t1 gap: label_t1 later than gross_t1 for 3-40% of rows, but by a median 0.15-4.5 s (JPN volatilitycarry 3.5 min);
overlaps with the next event visible only through label_t1: GBPJPY 1, HK 1, JPN rs 4, JPN vc 3, XAUUSD rs 1, XAUUSD vc 0.

Leakage experiments (current research datasets, AFML Ch.6 bagging, 50 trees, plain k-fold; AUC):

| edge | 10-fold no purge | 10-fold SHUFFLED | purged + 1% embargo | purged + 100 (current) | purged label_t1 + 100 | k = 5 / 10 / 20 / 40 purged |
|---|---|---|---|---|---|---|
| GBPJPY dailyrangecompletionfade_long | 0.532 | 0.530 | 0.530 | 0.530 | 0.530 | 0.532 / 0.530 / 0.532 / 0.532 |
| HK drawdownhazardcontinuation_short | 0.541 | **0.553** | 0.543 | 0.543 | 0.543 | 0.533 / 0.543 / 0.540 / 0.533 |
| XAUUSD rangestructure_long | 0.511 | **0.526** | 0.513 | 0.518 | 0.519 | 0.522 / 0.518 / 0.511 / 0.511 |
| XAUUSD volatilitycarry_long | 0.503 | **0.511** | 0.503 | 0.505 | 0.505 | 0.501 / 0.505 / 0.500 / 0.501 |

Shuffling inflates AUC on 3 of 4 edges (Ex 7.2). Purged results do not rise with k (p.107 leak test passes). Unpurged
unshuffled k-fold is barely different from purged here: the regularised, avgU-sampled trees cannot exploit the overlap
(s.7.3 cure 2). JPN not run (stale bar clock).

## 4H. AFML Ch.8 — feature importance audit (2026-10-24, decisions pending)

Book pages = pdf - 26 (pdf 139-153 = pp.113-127). Code: every edge's `research/01_feature_surgery_clustered_mda.py`
(Step 01): Spearman correlation of the 157 candidate features -> Marcenko-Pastur denoising -> ONC clustering (MLAM
Ch.4) -> clustered MDA (MLAM Ch.6) with an XGBoost classifier (fixed V1 params: 500 trees, depth 3, lr 0.03,
colsample 0.75) on purged CPCV, weighted log loss on the test fold -> drop the most negative cluster, repeat until no
cluster is negative.

| § / snippet (page) | Book | Quantara | Status |
|---|---|---|---|
| 8.1-8.2 (pp.113-114) | "Backtesting is not a research tool. Feature importance is." (Snippet 8.1) | importance runs before any backtest (Step 01) | matches in intent |
| 8.3 substitution effects (p.114) | correlated features dilute importance; PCA on raw features | clustering instead (MLAM) | see clustering finding |
| 8.3.1 / Snippet 8.2 MDI (pp.114-116) | in-sample, trees only; max_features=1 to avoid masking; zero importances -> NaN | not computed | **gap** |
| 8.3.2 / Snippet 8.3 MDA (pp.116-117) | OOS permutation, any classifier, purged + embargoed CV, sample-weighted neg log loss, importance relative to the permuted score | clustered MDA, purged CPCV + embargo, AFML-weighted log loss on test fold; raw loss difference (not divided by -scr1) | matches (scale differs, ranking same) |
| 8.4.1 / Snippet 8.4 SFI (pp.117-118) | single-feature OOS score, no substitution effects; complement to MDI/MDA | not computed | **gap** |
| 8.4.2 / Snippets 8.5-8.6 orthogonal features (pp.118-121) | PCA (standardised, 95% variance); weighted Kendall tau between importance and inverse PCA rank as an anti-overfitting check | not done | **gap** |
| 8.5 parallelised vs stacked (pp.121-122) | importance per instrument then aggregate, or stack instruments into one dataset (preferred) | per edge only; all edges share the same 157-feature schema | gap (diagnostic possible) |
| 8.6 / Snippets 8.7-8.10 synthetic test (pp.122-127) | validate the importance code on data with known informative / redundant / noise features; Snippet 8.8 classifier = bagged entropy trees, max_features=1, balanced | importance code never validated on synthetic data; classifier is XGBoost (boosting), not the final model's bagged trees (Ch.6 p.101 prefers bagging) | **gap** |

Step 01 outcome per edge (iterations; clusters at iteration 1; features kept):
GBPJPY 3 iterations, 2 clusters, 157 -> 50; HK 1 iteration, 2 clusters (89 + 68), **157 kept**; JPN rangestructure 1, 2
(79 + 78), **157 kept**; JPN volatilitycarry 9, 10 clusters, 157 -> 42; XAUUSD rangestructure 1, 2 (78 + 79),
**157 kept**; XAUUSD volatilitycarry 6, 4 clusters, 157 -> 53. With two clusters, clustered MDA permutes ~80 features at
once; that always hurts, so nothing is removed — for 3 edges Step 01 performs no selection at all.

ONC stability check (same 157 features, Marcenko-Pastur signal factors 15-18 on every edge; clusters found):
raw / denoised (current) / denoised + detoned (MLAM p.87 says denoise AND detone before clustering):
GBPJPY 3 / 2 / 2; HK 6 / 2 / 2; JPN rs 2 / 2 / 2; JPN vc 2 / 10 / 2; XAUUSD rs 2 / 2 / 7; XAUUSD vc 2 / 4 / 2.
Near-identical correlation structures give 2-10 clusters: the ONC step is unstable (its own audit belongs to MLAM Ch.4).

### 4H.1 Owner decisions 2026-10-24 and implementation status

Approved: 1-6 (Snippet 8.8 / MLAM 6.8 estimator; MDI + MDA + SFI; PCA weighted-Kendall diagnostic; MLAM clustering in
full; synthetic test; cross-edge report) + standing rules 6 (MLAM over AFML where it revises) and 8 (deviations only
after approval) + D038 (reduced unit test beside the book-exact run), D039 (sequential bootstrap + purged CV for edge
importance), D040 (seeded k-means). Built: `shared/features/clustered_feature_importance.py`,
`shared/research/feature_importance_audit.py`, `shared/research/mlam_ch6_synthetic_replication.py`,
`shared/research/cross_edge_feature_importance.py`, `tests/test_afml_ch8_clustered_importance.py`. Not yet wired into
Step 01 (waiting for the clustering / selection / residualisation / estimator / SFI decisions below).

Corrections made on the owner's instruction (2026-10-24): MLAM p.55 signal dimensions now taken literally from "X's
correlation matrix" (my first version used the feature correlation; XAUUSD rangestructure 2 -> 15 clusters); SFI at the
book's 1,000 trees (I had used 250 without approval); synthetic validation run book-exact.

Book-exact MLAM replication (10,000 rows, sklearn bagging 1,000 trees, KFold 10): 6 clusters (Fig 6.4); noise cluster
clustered MDA 0.0006 +/- 0.0016 vs 0.14-0.23 (Fig 6.6); clustered MDI noise 0.058 vs 0.14-0.31; noise features last by
MDA (Fig 6.3). Selection rules on it: A (MDA > 0) keeps noise; B (MDA - 1.645 se > 0) drops only noise (correct);
C (MDI > cluster share) drops noise and a real 10-feature cluster.

### 4H.2 Edge audit results (2026-10-24; 1,000 trees, 10-fold purged + embargoed, sequential bootstrap avgU)

Run time per edge 2 h 14 m - 4 h 50 m (one core each; SFI ~half). OOS weighted neg log loss of the all-feature model:
GBPJPY -0.703, HK -0.766, XAUUSD rs -0.705, XAUUSD vc -0.706 (a constant 0.5 forecast scores -0.693: the fully grown
Snippet 6.8 trees are worse than a coin flip in log-loss terms).

Features kept (rule A / B / C) by clustering method; [clusters]:
| edge | current (old Step 01) | MLAM corr signed | MLAM corr abs | MLAM VI |
|---|---|---|---|---|
| GBPJPY | [2] 157/157/91 | [2] 157/149/149 | [2] 157/157/63 | [6] 75/63/63 |
| HK | [4] 157/43/114 | [2] 157/9/148 | [2] 157/59/98 | [2] 157/4/4 |
| XAUUSD rs | [7] 140/10/39 | [15] 116/28/77 | [2] 157/44/44 | [2] 157/119/38 |
| XAUUSD vc | [2] 157/0/77 | [2] 9/0/148 | [2] 23/0/134 | [2] 110/0/47 |
With MLAM p.85 residual features, rule B: GBPJPY 91/149/63/63, HK 0/0/59/4, XAUUSD rs 57/49/44/119, XAUUSD vc 0/0/0/0.
Mean silhouettes 0.12-0.37 (low). Clustered MDA t-stats mostly < 2; XAUUSD volatilitycarry has no cluster with t > 1.3
under any method (that edge already failed Gate E, Ch.1). Clustered MDI ~= the cluster's size share everywhere
(e.g. 0.952 vs 0.949): with max_features=1 MDI carries almost no information here, so rule C is close to arbitrary.

AFML diagnostics: PCA weighted Kendall tau GBPJPY 0.558, HK 0.329, XAUUSD rs 0.251, XAUUSD vc 0.305 (book example 0.82).
Cross-edge (AFML s.8.5): per-feature MDA rank agreement between edges -0.035..0.211 (no feature is consistently
important across instruments); within-edge agreement MDA vs MDI 0.07-0.34, MDI vs SFI -0.43..0.02.
Outputs: assets/*/edges/*/artifacts/feature_importance_audit/feature_importance_audit.json,
docs/mldp/feature_importance/cross_edge_feature_importance.json, logs/ch8_feature_importance_audit_20261024/.

### 4H.3 Follow-up decisions and tests (2026-10-24)

Approved: D041 (importance trees min_weight_fraction_leaf 0.05, AFML p.98), D042 (no MLAM p.85 residual features),
D043 (SFI dropped from routine, tool kept). Fast method made standard: each fold's sequential-bootstrap draws computed once
and shared across configurations, folds in parallel worker processes (4 min per edge vs 2.5-4.8 h).

Thesis-field test (owner request; paired folds and bootstrap draws; Snippet 6.8 trees with mwfl 0.05; 1,000 trees):
| edge | thesis fields | OOS wNLL base -> +thesis | AUC base -> +thesis | paired diff (t, folds better) | thesis group MDA t |
|---|---|---|---|---|---|
| GBPJPY | 13 | -0.6793 -> -0.6792 | 0.519 -> 0.521 | +0.0001 (t 0.28, 5/10) | 1.4 (not kept) |
| HK | 34 | -0.6943 -> -0.6934 | 0.545 -> 0.549 | +0.0008 (t 0.88, 6/10) | 2.2 |
| XAUUSD rs | 11 | -0.6681 -> -0.6674 | 0.520 -> 0.516 | +0.0007 (t 1.33, 6/10) | 7.3 |
| XAUUSD vc | 10 | -0.6865 -> -0.6863 | 0.506 -> 0.505 | +0.0001 (t 0.40, 6/10) | 2.3 |
Thesis fields chosen automatically (numeric, not generic, not label metadata, non-constant, ADF p < 0.05); price levels
excluded by ADF. The model does use them (group MDA significant on 3 of 4 edges), but adding them does not improve OOS fit
on any edge (no paired t above 1.33). The fields that carry the group MDA on XAUUSD are near-copies of generic features
(Bar_Return, Body_Direction, Close_Location); the true thesis variables are weak (e.g. Structure_Bias_TStat t 1.1).
Logged as trials in each edge's research_trial_ledger.jsonl (10 records: audit, config test, thesis test).
Output: assets/*/edges/*/artifacts/feature_importance_audit/thesis_feature_test.json.

Owner conclusion: keep the uniform feature schema (thesis fields add no OOS improvement).

### 4H.4 Step 01 wired (2026-10-24, D044)

Every edge's Step 01 now calls `shared/features/clustered_feature_importance.select_features_clustered` (patch tool
`docs/tools/patch_afml_ch8_step01.py`): VI clusters, clustered MDA + MDI, rule B, single pass, folds in parallel; Step 01
is repo-mode only. Wiring check on XAUUSD rangestructure: 3 min; 2 clusters, 119 features kept (t 8.9), 38 dropped (t 1.6);
OOS weighted neg log loss -0.668, AUC 0.520; Step 02's manifest validation PASS; original artifacts restored (new outputs
kept in logs/ch8_step01_wiring_check_20261024/). Tests: 33 walkthrough tests pass.
Observation for the owner: a cluster close to the rule-B line can flip with the single random permutation per fold
(that 38-feature cluster scored t 2.3 in the config test, t 1.6 here).

Shuffle-stability test (owner request; Step 01 setup; trees fitted once per fold; K shuffles per fold averaged; 10 seeds):
| edge | same rule-B selection across seeds K=1 (book) / 5 / 10 | borderline cluster t (mean +/- sd) K=1 -> K=5 |
|---|---|---|
| GBPJPY | 70% / 100% / 100% | 12-feature C_2: 1.26 +/- 0.74 (kept 30% of seeds) -> 0.99 +/- 0.43 (kept 0%) |
| HK | 100% / 100% / 100% | 4-feature C_0: 2.31 +/- 0.20 -> 2.49 +/- 0.08 |
| XAUUSD rs | 90% / 100% / 100% | 38-feature C_0: 2.00 +/- 0.34 (kept 90%) -> 1.99 +/- 0.16 (kept 100%) |
| XAUUSD vc | 100% / 100% / 100% | 110-feature C_1: 4.71 +/- 0.61 -> 4.85 +/- 0.39 |
t-stat spread falls ~2-3x at K=5; K=10 adds little. With 5% early stopping XAUUSD volatilitycarry now has a significant
cluster (110 features, t ~4.8). Logged as trials (4 records). Output: artifacts/feature_importance_audit/shuffle_stability_test.json.

D045 approved and implemented (5 shuffles per fold, STEP01_N_SHUFFLES); Step 01 re-run on XAUUSD rangestructure 2 min 49 s,
both clusters kept (t 9.5 and 1.95); originals restored. **Ch.8 closed by the owner 2026-10-24.**

Carried to Ch.9: F1 scoring vs approve-all (D028), weighted test-fold scoring (D037), time-decay c (D029), Optuna tuning a
single tree, best max_depth = 2 at the search bound. Carried to the retrain batch: all edges re-run Steps 01-09 with the
Ch.4-8 changes (D030, D031, D034, D036, D044, D045); JPN research datasets re-exported first (stale bar clock).

## 4I. AFML Ch.9 — hyper-parameter tuning audit (2026-10-24, decisions pending)

Book pages = pdf - 26 (pdf 155-162 = pp.129-136). MLAM does not revisit hyper-parameter tuning (AFML stays the reference).
Code: every edge's `research/02_train_meta_model_optuna.py` (Optuna TPE, 30 trials; one DecisionTreeClassifier per CPCV
fold, CPCV(5, 2) = 10 splits purged + embargoed; objective = mean test-fold F1 at the F1-best threshold chosen on the
train fold from a 0.30-0.80 grid; space max_depth 2-8, min_samples_leaf 5-200 log, min_samples_split 10-400 log,
min_weight_fraction_leaf 0-0.05, ccp_alpha 1e-6-1e-2 log; final model = sequential-bootstrap bagging of the tuned tree).

| § / snippet (page) | Book | Quantara | Status |
|---|---|---|---|
| 9.1-9.2 / Snippet 9.1 (pp.129-130) | purged k-fold inside the search; stage 1 tune the estimator on the train data, stage 2 fit the validated estimator on all data, optionally bagged | purged + embargoed CPCV in the search; tuned tree then bagged (Ch.6) on all rows | matches (CPCV instead of k-fold: more splits, same purge rules) |
| 9.2 scoring (p.130) | f1 for meta-labeling (guards against a classifier that predicts all negatives in a sample with many negatives) | f1, but at a threshold optimised on each train fold (0.30-0.80); unweighted | **finding** |
| 9.3 / Snippet 9.3 (pp.131-132) | randomized search with parameter distributions (good statistical properties) | Optuna TPE (Bayesian, adaptive), not in the book | deviation (unrecorded) |
| 9.3.1 / Snippet 9.4 log-uniform (pp.132-133) | log-uniform draws for positive scale parameters | log=True for min_samples_leaf, min_samples_split, ccp_alpha | matches |
| 9.4 (pp.134-135) | neg_log_loss for investment strategies: accounts for confidence; sample-weighted log loss ties the score to PnL (Ch.7 cvScore) | not used for tuning | **finding** |
| Ex 9.6 (p.136) | equal-size bets -> accuracy may be the more appropriate score | live bets are NOT equal: risk = base budget x portfolio weight x AFML Ch.10 bet size (2 Phi(z) - 1 on the calibrated probability, payoff-aware, `shared/calibration/sigmoid_afml.payoff_aware_afml_bet_size`) x throttle x target lock / AFML concurrency divisor x decommission level, capped at the single-trade maximum (`portfolio_execution_bot.compute_candidate_sizing`) -> confidence-sized bets favour log loss (s.9.4) | noted (corrected 2026-10-24 after the owner pointed out my wrong 'fixed risk' statement) |
| Snippet 9.2 MyPipeline | sample_weight through pipelines | weights passed directly to fit | matches |

F1 objective evidence (deployed Optuna runs): best CPCV F1 is the approve-all F1 (D028: -0.003..+0.0015); mean F1 falls
with tree depth on every edge (e.g. XAUUSD rs depth 2: 0.645, depth 7: 0.629); best max_depth = 2 = the search lower
bound on all 6 edges; the train-fold threshold is usually 0.30 (the grid minimum) and test recall 0.84-1.00 (XAUUSD vc:
threshold 0.30 and recall 1.00 in every fold = approve everything). The book's reason for F1 (many negatives) is the
opposite of our case (positives 38-48%): with a weak signal F1 is maximised by approving all events.
Already carried here: D028 (F1 vs approve-all), D037 (weighted test-fold scoring), D029 (time-decay c).

### 4I.1 Nested-CV tuning test (owner-approved, 2026-10-24)

`shared/research/tuning_objective_test.py`: outer purged + embargoed 5-fold; inner purged + embargoed 5-fold tuning of
Step 02's tree (Snippet 9.1 stage 1; max_depth 1-8, rest of the space unchanged, 30 trials); stage 2 = Ch.6 bagging
(1,000 trees, shared draws); scored on the pooled outer-test predictions. 5-7 min per edge, 4 edges in parallel.
OOS weighted neg log loss vs the current method (A = F1 at a tuned threshold, TPE); + = better; [paired t, 5 folds]:
| config | GBPJPY | HK | XAUUSD rs | XAUUSD vc |
|---|---|---|---|---|
| A F1 tuned threshold, random | +0.0051 [+1.5] | -0.0007 [-0.8] | +0.0000 [0.0] | +0.0002 [+1.0] |
| B F1 at 0.5 (Snippet 9.1), TPE | +0.0071 [+3.0] | -0.0025 [-1.4] | -0.0006 [-1.0] | -0.0018 [-2.0] |
| B F1 at 0.5, random (Snippet 9.3) | +0.0065 [+4.0] | -0.0013 [-0.9] | -0.0013 [-1.6] | +0.0004 [+0.3] |
| C weighted neg log loss (s.9.4), TPE | +0.0024 [+1.1] | +0.0012 [+0.5] | -0.0012 [-0.5] | -0.0003 [-0.3] |
| C weighted neg log loss, random | +0.0031 [+1.9] | +0.0025 [+0.7] | -0.0006 [-0.2] | -0.0004 [-0.4] |
| C random, time decay c = 0.5 | +0.0019 [+1.2] | -0.0016 [-0.6] | -0.0022 [-0.9] | -0.0024 [-2.0] |
| C random, time decay c = 0 | -0.0023 [-1.2] | +0.0012 [+0.3] | -0.0036 [-3.1] | -0.0033 [-1.2] |
Mean AUC 0.518-0.521 and Spearman(prob, executed R) 0.035-0.040 for every configuration. Reading: once the tuned tree is
bagged, the tuning score and the search method make no consistent OOS difference (largest mean gain +0.0012, 2 of 4
edges); only GBPJPY separates (B better, t 3-4). Time decay hurts on 3 of 4 edges (c = 0: XAUUSD rs t -3.1). Tuned
max_depth now spreads 1-7 and depth 1 is chosen in many folds. Logged as trials (4 records).

### 4I.2 Owner decisions and implementation (2026-10-24)

| # | Item | Decision | Where |
|---|---|---|---|
| 1 | scoring + search | B: F1 at the classifier's decision (p > 0.5) with randomized search = AFML Snippets 9.1/9.3 literally (book rule: labels {0,1} = meta-labeling -> 'f1', p.130; s.9.4 neg log loss is the book's choice for non-meta-labeling labels; s.14.8 pp.206-207 F1 for meta-labeling, label swap only when positives dominate) | D046 |
| 2 | depth range | max_depth 1-8 | D046 |
| 3 | tuning CV | Snippet 9.1 purged 5-fold (CPCV stays for Ch.12 backtest paths) | D046 |
| 4 | time decay | c = 1 (no decay), on data | D029 closed |
| — | weighted test-fold scoring | book F1 is unweighted; closed | D037 closed |
Correction recorded: live sizing is probability-based (AFML Ch.10 bet size x weights x caps), not fixed risk.
Implemented with `docs/tools/patch_afml_ch9_step02.py` on all 6 edges; check on XAUUSD rangestructure: 5 folds, threshold
0.5, random search over depth 1-8, ~0.5 s per trial; test recall still 0.92-1.00 (D028 stays open as a finding).
Tests: `tests/test_afml_ch9_tuning.py`.

## 4J. AFML Ch.10 — bet sizing audit (2026-10-25, discussion and decisions pending)

Book pages: Ch.10 = pdf 168-176 = pp.141-149 (printed page = pdf - 27 here, after the Part 3 title pages). Read line by
line from the PDF (pymupdf extraction). MLAM does not revisit bet sizing (AFML is the reference).
Code read: `shared/calibration/sigmoid_afml.py` (payoff_aware_afml_components), `shared/sizing/afml_ch10_reference.py`
(book transcriptions), `shared/live_payoff_contract.py` (frozen live sizing), `portfolio_allocator/afml_concurrency_sizing.py`
(D017), `portfolio_live/portfolio_execution_bot.compute_candidate_sizing`, `portfolio_live/live_manifest.json` (last
deployed manifest; live stopped; still lists XAUUSD volatilitycarry, which Gate E later removed).

| § / snippet (page) | Book | Quantara | Status |
|---|---|---|---|
| 10.2 (pp.141-142) | three strategy-independent ways: (a) mixture of two Gaussians on net concurrency c_t (EF3M), (b) budgeting m_t = c_t,l / max c_l - c_t,s / max c_s (max "or some other quantile"), (c) meta-labeling probability -> size | (c) meta-model probability -> size, combined with (b) per edge: each signal gets risk / c_q, c_q = 0.95-quantile of the edge's concurrent signals; (a) not used | D017 (implemented 2026-10-17) |
| 10.3 / Snippet 10.1 (pp.142-144) | binary: z = (p - 1/2)/sqrt(p(1-p)), m = 2 Z[z] - 1; meta-labeling: signal = side x size, no bet when the predicted label is 0 (p <= 1/2) | the same formula applied to an "effective probability": p_eff = 1/2 + 1/2 x clip((p - p_be)/(1 - p_be), 0, 1), p_be = empirical break-even probability from OOS executed R (avg win / avg loss); bet only if p >= final_live_threshold (0.36-0.48 in the last manifest) | D007 (payoff extension; OOS benchmark still open) |
| 10.4 / Snippet 10.2 (p.144) | average all active bets' sizes into one position (avoid override and turnover) | each signal its own MT5 order with its own SL/TP, never resized; budgeting divisor approximates the averaged position | D017 |
| 10.5 / Snippet 10.3 (pp.144-145) | discretise m* = round(m/d) d to stop jitter from re-targeting | no re-targeting, so no jitter; broker lot step rounds sizes | n/a under D017 |
| 10.6 / Snippet 10.4 (pp.145-148) | dynamic position size from forecast price f_i vs market price (sigmoid x/sqrt(w + x^2) or power), target position, breakeven limit price for the order | market entries with fixed triple-barrier SL/TP; no forecast-price-driven resizing or limit prices | not used |
| outside Ch.10 | — | risk = base 0.5 x HRP portfolio weight x bet size x throttle x target lock / divisor x decommission level, capped at 0.5% per trade, 2.2% per edge/family, 2.8% per asset, 3.5% total | Quantara overlays |

OOS evidence (each edge's calibrated OOS predictions, live calibrator; last live manifest's p_be and thresholds):
| edge | p 5/50/95% | p_be | share p > 0.5 (book bets) | share p >= live thr (we bet) | mean executed R: p < p_be / p_be..0.5 / > 0.5 |
|---|---|---|---|---|---|
| GBPJPY | 0.306 / 0.396 / 0.411 | 0.343 | 0.0% | 67.6% | -0.086 / +0.121 / — |
| HK | 0.322 / 0.389 / 0.423 | 0.344 | 0.0% | 61.1% | -0.211 / +0.104 / — |
| JPN rs | 0.388 / 0.403 / 0.428 | 0.357 | 0.0% | 99.2% | -0.441 / +0.110 / — |
| JPN vc | 0.282 / 0.383 / 0.446 | 0.351 | 0.0% | 19.7% | -0.100 / +0.116 / — |
| XAUUSD rs | 0.462 / 0.481 / 0.504 | 0.395 | 9.0% (mean size 0.012) | 51.4% (mean size 0.128) | — / +0.130 / +0.194 |
| XAUUSD vc | 0.444 / 0.466 / 0.486 | 0.403 | 0.0% | 37.1% | — / +0.096 / — |
Under the book's literal 10.3 rule five of six edges would never bet (the calibrated probabilities sit near the base rate,
38-48%, below 1/2). The book's null hypothesis p = 1/2 is the break-even only for symmetric payoffs; with PT 2 / SL 1
barriers (asymmetric barriers are allowed for meta-labeling, AFML s.3.6 p.50) the empirical break-even is 0.34-0.40, and
OOS trades between p_be and 1/2 earned +0.10 to +0.13 R while trades below p_be lost. Caveat: the calibrator, p_be and
thresholds were fitted on these same OOS rows, so the band means are not a clean holdout test.

### 4J.1 Owner decisions so far (2026-10-26)

| # | Item | Decision |
|---|---|---|
| 1 | 10.3 payoff-aware size (D007) | KEEP. Clean OOS benchmark (book formula vs ours on rows none of calibrator / p_be / threshold saw, trials counted) deferred until the owner asks |
| 2 | live threshold | no change; owner knows how the allocator picks it |
| 3 | 10.6 dynamic sizing + limit prices | EXPLORE (owner wants it); explanation and test design pending |
| 4 | 10.4 averaging / 10.5 discretisation / budgeting combined | AGREED target stack (2026-10-26): payoff-aware 10.3 -> 10.4 average per edge -> 10.5 discretise -> 10.6 dynamic size + limit price per order; D017 divisor retired (budgeting is a 10.2 alternative, would double-shrink). Not implemented; 10.6 discussion continues first |
| 5 | Quantara overlays (throttle, target lock, decommission, caps) | revisit after Ch.10 is implemented; drop any that show no use |
| 6 | Sizing stack (owner, 2026-10-26, supersedes row 4) | payoff-aware 10.3 size -> D017 budgeting per order (KEPT, approved deviation from Snippet 10.1 step #2 on the s.4J.3 evidence) -> s.10.6 dynamic size per order -> s.10.6 limit price on every order -> s.10.5 only if per-order resizing creates jitter (step 3 measures). Book-literal 10.4 averaging rejected on data |
| 7 | Limit price at entry | design approved and to be implemented (neutral on research data, s.4J.4; a slippage cap) |
| 8 | Step 3 (cost sim of s.10.6 on GBPJPY, HK, JPN vc) | design approved; owner asked for a plain-language explanation of Ch.10 first, then go-ahead |
| 9 | Ch.17-19 feature programme | NOT now: chapters in order, step by step |
| 10 | Close (owner, 2026-10-26) | no s.10.6 dynamic sizing (D048, re-test after retrain); no limit price (D049: neutral, owner: no need); D047 keeps D017; register + runbook Revision 18 (Step 10F, Appendix 35); holdout sealed; next Ch.11 |

Owner concern recorded: live trades often show large floating profit and then close at the stop loss (example: XAUUSD long,
entry 4100, TP 4300, SL 4000, price reaches 4250 then reverses to 4000).
Worked example of literal s.10.6 on that trade (sigmoid, f = TP, rebalanced every point, no costs; scratchpad
ch10_6_gold_example.py): the target position depends only on the current price, so units sold on the way up are bought back
on the way down. 4100 -> 4250 -> 4000 loses about the same as the fixed position (-9,576 vs -9,500 with omega for m* = .95 at
the entry gap; -5,732 vs -5,000 with m* = .5) after 74-89 orders; a straight run to TP earns 24-47% less than the fixed
position. s.10.6 is an inventory rule that takes profit as price nears the forecast, not a profit-lock / trailing exit.
Correction: the s.10.6 limit price is the worst acceptable average price for the adjustment order (at entry it is
marketable: 112.37 against market 100 in the book example), not a passive order that improves the entry price.
Clarification (2026-10-26): the worked example above froze f. The book's s.10.6 setting (p.145) has new forecasts of the same
bet's outcome price formed while it is open, E_{t_j,0}[p_{t_i,1}] for t_j,0 <= t_i,1; s.10.6's value comes from those updates
(target falls when the updated forecast falls). The book does not say how the updated forecasts are produced; Quantara's
meta-model only predicts at candidate-event bars, so an in-trade re-forecast would be new modelling, to be tested for
information content before any build. Owner shared an outside chat on s.10.6 (read 2026-10-26); its points match the book
except that "built for institutional size" is an inference, not Ch.10 text.

### 4J.2 Ch.10 tests, steps 1-2 (owner-approved 2026-10-26; pre-registered before any result was seen)

Owner instruction: steps 1 and 2 now; trader exits (ratchet, breakeven, trailing) deferred — book first; step 3
(cost simulation: fixed brackets vs s.10.6 frozen f vs s.10.6 updated f + s.10.4 + s.10.5) only if step 2 passes.
Data: research period only. The holdout (assets/*/holdout, to 2026-10-22) stays sealed (s.4.1: one look per frozen
version, MLAM s.1.4.2 p.8); it was looked at once by Gate E 2026-10-23 and is kept for the final one-look
confirmation of whatever Ch.10 design is chosen.
- Step 1 `shared/research/ch10_path_giveback_audit.py`: every resolved research trade replayed on curated Dukascopy
  MID ticks from its entry_time (5.7 s delay) with its own TP / SL / vertical; MFE as a share of the TP distance;
  share of losers / SL exits that first reached 50 / 75 / 90 % of TP; references: gambler's ruin b(1-k)/(ka+b)
  (2:1 -> 25.0 / 10.0 / 3.6 % of SL exits) and 5 random entries per trade (same weekly file, side, distances as a
  share of price, vertical duration). Check: recomputed outcome sign must match label_r. Logs `logs/ch10_step1_20261026/`.
- Step 2 `shared/research/ch10_intrade_forecast_test.py`: rows = (open trade, bar close strictly inside it), label =
  the trade's mid meta label, span [t_j, label_t1]; purged + embargoed 10-fold (Snippets 7.1-7.3, embargo max(100, 1%
  of rows)); forecasts (a) random walk s = (side(close - entry) + SL)/(TP + SL), (b) bagged trees on s, time left,
  entry OOF probability, (c) (b) + the edge's selected features at bar j. Model AFML s.6.4 pp.98-99 option 3 + option 2
  (Bagging of entropy trees, max_features sqrt, class_weight balanced, min_weight_fraction_leaf 0.05, 1000 trees,
  max_samples = avgU); weights = average uniqueness (Snippet 4.4). **GO rule (pre-registered):** (c) beats (b) on
  pooled OOS weighted log loss AND (c) beats (a) on OOS weighted AUC, both with trade-clustered bootstrap 95% CI above
  zero. NO-GO on an edge means s.10.6 updated forecasts have no fuel there. Each edge's run is one trial in its ledger.
  Logs `logs/ch10_step2_20261026/`.

**Results (2026-10-26).** Reports copied to each edge's `artifacts/feature_importance_audit/ch10_*.json`; 2 trials per
edge logged (`afml_ch10_path_giveback_audit`, `afml_ch10_intrade_forecast_test`).
Step 1 — share of stop-loss exits that first reached 50 / 75 / 90 % of the TP distance (recomputed outcome sign matched
the stored mid label on 99.4-99.5 % of trades):
| edge | all trades | approved (p >= live thr) | random entries | gambler's ruin |
|---|---|---|---|---|
| GBPJPY | 24.1 / 10.2 / 2.9 | 24.3 / 10.5 / 2.9 | 25.5 / 9.6 / 3.4 | 25.0 / 10.0 / 3.6 |
| HK | 23.8 / 8.6 / 3.4 | 26.1 / 10.1 / 4.2 | 23.6 / 9.3 / 3.4 | same |
| JPN rs | 23.9 / 9.1 / 3.2 | 24.0 / 9.1 / 3.2 | 22.2 / 9.2 / 3.2 | same |
| JPN vc | 25.0 / 9.3 / 3.2 | 23.2 / 7.5 / 2.4 | 24.1 / 9.7 / 3.4 | same |
| XAUUSD rs | 13.7 / 3.8 / 1.5 | 15.5 / 4.1 / 1.3 | 14.4 / 4.3 / 1.2 | (39 % vertical exits) |
| XAUUSD vc | 12.8 / 4.1 / 1.3 | 14.9 / 4.5 / 1.9 | 15.5 / 4.7 / 1.5 | (38 % vertical exits) |
Finding: the giveback the owner sees live is the normal random-walk rate for 2:1 brackets (about 1 trade in 16-19 reaches
75 % of TP and then loses: 5.4-6.3 % of all trades; 1.7-2.0 % on XAUUSD, whose 16 h vertical closes many trades first).
No edge shows more giveback than random entries. Side note (diagnostic only, not acted on): random entries in the same
weekly files earned a higher mean mid R than the edge's own entries on 5 of 6 edges (GBPJPY the exception); the
same-week sampling conditions on the week an event occurred, so this is not a clean timing test — carry to Ch.14/15
(with D028).
Step 2 — pooled OOS (10-fold purged): AUC a / b / c; log-loss gain of c over b with 95 % CI:
| edge | rows | AUC a random walk | AUC b state | AUC c full | NLL gain c over b | verdict |
|---|---|---|---|---|---|---|
| GBPJPY | 189,573 | 0.815 | 0.813 | 0.806 | +0.0003 (-0.0042..+0.0048) | NO-GO |
| HK | 186,012 | 0.791 | 0.786 | 0.754 | -0.0262 (-0.0331..-0.0193) | NO-GO |
| JPN rs | 182,154 | 0.771 | 0.763 | 0.745 | -0.0131 (-0.0192..-0.0070) | NO-GO |
| JPN vc | 229,948 | 0.775 | 0.769 | 0.763 | -0.0041 (-0.0090..+0.0010) | NO-GO |
| XAUUSD rs | 105,255 | 0.791 | 0.787 | 0.755 | -0.0276 (-0.0341..-0.0214) | NO-GO |
| XAUUSD vc | 106,210 | 0.791 | 0.787 | 0.763 | -0.0274 (-0.0313..-0.0237) | NO-GO |
Finding: NO-GO on all 6. Where price sits between the barriers already explains the outcome (AUC 0.77-0.82, the random
walk's own number); the edge's features measured during the trade add nothing and make the forecast worse out of sample
(c beats b on log loss in 0/10 folds on HK and both XAUUSD edges). So on current features, an "updated forecast" is a
function of price only — which is exactly the frozen-f s.10.6 already shown to give back the same at the stop and earn
less at the target (s.4J.1). Per the pre-registered rule, step 3 is not run. Limits: one model family (the book's s.6.4
bagged trees), the edges' current selected features (pre-retrain), research period only.
Self-critique of step 2: model (b) scored below the plain formula (a) on every edge, so the bagged trees (min weight
fraction 5 %) cannot even reproduce the smooth price-location signal; a small feature effect could be hidden behind that
loss. A cleaner test predicts the remaining return directly (the s.10.6 forecast is a price, f) against the martingale
forecast 0 — proposed in s.4J.3.

### 4J.3 s.10.4 averaging + s.10.5 discretisation vs D017 (owner-approved 2026-10-26)

`shared/research/ch10_averaging_cost_sim.py` (research period; approved bets p >= live threshold; size = afml_bet_size;
bets end at the mid-path touch; fills at Moneta bid/ask from the broker cost model, commission, swap per rollover;
averaged position re-targeted at each bet entry and each bet end + 5.7 s; Q = 1 / median SL of the trailing 30 days,
weekly; d = 0 / .01 / .05 / .10 = AFML exercise 10.5 values). Sanity: D017 net R per unit risk committed (0.12-0.35)
is close to Gate C executed_r on the same bets (0.11-0.31). Reports in each edge's
`artifacts/feature_importance_audit/ch10_averaging_cost_sim.json`; one trial per edge.
| edge | D017 Sharpe | AVG d=0 | d=.01 | d=.05 | d=.10 | D017 net/maxDD | best AVG net/maxDD | risk in use D017 / AVG |
|---|---|---|---|---|---|---|---|---|
| GBPJPY | 0.84 | 0.33 | 0.32 | 0.32 | 0.27 | 4.22 | 1.27 | 0.012 / 0.031 |
| HK | 0.78 | 0.38 | 0.36 | 0.49 | 0.46 | 3.33 | 1.52 | 0.011 / 0.040 |
| JPN rs | 0.73 | 0.41 | 0.42 | 0.52 | 0.42 | 3.70 | 1.74 | 0.009 / 0.028 |
| JPN vc | 1.14 | 0.66 | 0.65 | 0.71 | 0.74 | 4.59 | 2.51 | 0.005 / 0.020 |
| XAUUSD rs | 1.42 | 1.36 | 1.36 | 1.33 | 1.38 | 10.76 | 8.32 | 0.012 / 0.042 |
| XAUUSD vc | 1.14 | 0.99 | 0.98 | 1.04 | 1.15 | 4.20 | 4.23 | 0.012 / 0.037 |
Finding: risk-adjusted, D017 beats book averaging on all 6 edges (Sharpe), and on net/maxDD on 5 (XAUUSD vc tie at
d = .10). Averaging earns more in total only because it holds 3-4x more risk: mean concurrency when active (2.5-7.4) is far
below c_q (6-18), and an averaged position is full size (m Q) even when a single bet is open. It also detaches P&L from
each bet's own barriers (a stopped bet only re-weights the average). Discretisation cut orders 2-5x but not spread cost
(the cost model has no per-order fee). Owner decision pending: keep D017 against the agreed stack (evidence above), or
not. Note: the AVG arms ARE the book's Snippet 10.1 pipeline (10.3 meta-label size -> 10.4 average -> 10.5 discretise,
no budgeting divisor); D017 = 10.3 + 10.2(b) budgeting.

### 4J.4 Plan step A and limit-price test (owner-approved 2026-10-26; pre-registered before results)

- Step A `shared/research/ch10_remaining_return_test.py`: same in-trade rows as step 2; target = remaining mid R from
  bar close j to the trade's exit, r_rem = label_r - side (close_j - entry_mid) / SL (the s.10.6 forecast is a price:
  f - p_j = E[r_rem] x SL). Forecasts: zero (martingale), constant drift (training-fold weighted mean), (b') bagged
  regression trees on trade state [s, time-left fraction, entry OOF probability, R so far, best / worst R so far,
  hours in trade, realised vol since entry / pre-entry vol, distance to TP and to SL in units of recent vol x sqrt(hours
  left), hour of week, hours to weekend close, hours to the next 21:00 UTC], (c') (b') + the edge's selected features.
  Model: BaggingRegressor(DecisionTreeRegressor(max_features sqrt, min_weight_fraction_leaf 0.05), 1000, max_samples
  avgU) (regression analogue of s.6.4 option 3 + 2); weights = average uniqueness; purged + embargoed 10-fold.
  **GO_state:** (b') beats zero AND constant drift on pooled OOS weighted MSE, trade-clustered bootstrap 95 % CI of the
  gain above 0. **GO_features:** (c') beats (b') the same way. Logs `logs/ch10_stepA_20261026/`.
- Limit price `shared/research/ch10_limit_price_test.py` (design approved): per approved bet, barriers anchored at the
  signal mid; f = SL + p (TP - SL), p = live calibrated probability; x0 = side (f - signal mid); omega = x0^2 (m^-2 - 1)
  with m = the s.10.3 payoff-aware size (so s.10.6 and s.10.3 agree at the decision price); Q = 1 / (c_q SL). At the
  fill (5.7 s later) the size is re-computed at the actual ask (bid for shorts): m1 = x1 / sqrt(omega + x1^2),
  x1 = side (f - fill); no trade if x1 <= 0; the book limit price p_bar = average of L(f, omega, u) over the units is
  reported (full fill if fill <= p_bar). Outcome per unit = Gate C executed_r (broker costs). Compared with the current
  always-fill entry: total net R, daily Sharpe, trades skipped / shrunk and their mean R. No ticks needed (entry_ask /
  entry_bid / signal_mid are in the calibrated OOF file). Logs `logs/ch10_limit_20261026/`.

**Results (2026-10-26).** Reports in each edge's `artifacts/feature_importance_audit/` (ch10_remaining_return_test.json,
ch10_intrade_forecast_test_rerun.json, ch10_limit_price_test.json); trials logged. Data note: JPN research datasets
predate the 2026-10-23 bar rebuild, so both scripts read each edge's own research bars (feature-matrix OHLC), not
assets/*/bars; rows at or after an intrabar barrier touch are removed (0.4-1.1 % of rows).
Step A (pooled OOS MSE gain, trade-clustered 95 % CI):
| edge | R2 vs zero, state (b') | state beats zero | state beats drift | features (c') beat state | GO_state / GO_features |
|---|---|---|---|---|---|
| GBPJPY | 2.9 % | +0.045 (+0.032..+0.058) | +0.041 (+0.027..+0.055) | -0.008 (-0.013..-0.001) | GO / no |
| HK | 1.4 % | +0.024 (+0.011..+0.038) | +0.021 (+0.008..+0.033) | +0.008 (-0.004..+0.020) | GO / no |
| JPN rs | 0.6 % | +0.010 (-0.004..+0.024) | +0.016 (+0.003..+0.030) | -0.003 (-0.018..+0.013) | no / no |
| JPN vc | 1.2 % | +0.020 (+0.007..+0.033) | +0.025 (+0.012..+0.039) | +0.005 (-0.007..+0.016) | GO / no |
| XAUUSD rs | 0.3 % | +0.003 (-0.006..+0.012) | +0.003 (-0.006..+0.011) | +0.006 (-0.001..+0.013) | no / no |
| XAUUSD vc | 0.5 % | +0.006 (-0.002..+0.012) | +0.008 (+0.001..+0.014) | -0.001 (-0.005..+0.003) | no / no |
Mean remaining R by quintile of the state forecast (lowest -> highest): GBPJPY -0.25 .. +0.17/+0.20, HK -0.22 .. +0.09,
JPN vc -0.16 .. +0.23. Mechanism (rank correlations, quintile means): the main signal is R so far — trades that are
losing so far have negative remaining return (-0.11 to -0.23 R in the bottom quintile) and trades that are winning so far
have positive remaining return (+0.09 to +0.18 R), i.e. in-trade continuation, which a martingale would not show
(E[r_rem | state] = 0); plus the entry probability on JPN vc (bottom quintile -0.24 R, top +0.21 R). The same direction
appears on XAUUSD rs (-0.05 .. +0.13) but is not significant. The edge's selected features add nothing on any edge.
Step 2 re-run with the touch filter: unchanged (NO-GO on all 6; AUC a 0.769-0.815).
Limit price: book limit within reach on 99.3-100 % of fills; average size kept 91-99 % (spread + 5.7 s slippage eat part
of x0 = 0.21-0.47 SL); skipped 0-0.1 %; net R per unit risk and daily Sharpe change by -0.02 .. +0.02 (GBPJPY Sharpe
0.768 -> 0.785, HK 0.728 -> 0.711, JPN rs 0.680 -> 0.666, JPN vc 1.102 -> 1.121, XAUUSD rs 1.512 -> 1.507, XAUUSD vc
1.133 -> 1.143). Neutral on research data; it is a cap for rare large slippage.
Entry meta-model, for the owner's model-quality question (raw OOF AUC, month-block bootstrap 95 % CI; Gate C executed R
all vs approved): GBPJPY 0.537 (0.516-0.561), +0.087 -> +0.150; HK 0.538 (0.506-0.571), +0.074 -> +0.157; JPN rs 0.525
(0.486-0.557), +0.107 -> +0.110; JPN vc 0.563 (0.521-0.597), +0.056 -> +0.312; XAUUSD rs 0.519 (0.495-0.545), +0.135 ->
+0.200; XAUUSD vc 0.518 (0.500-0.536), +0.096 -> +0.156 (thresholds were chosen on these rows).

### 4J.5 Step 3 — s.10.6 dynamic sizing per order vs fixed brackets (owner go-ahead 2026-10-26)

`shared/research/ch10_dynamic_sizing_sim.py` on the three step-A GO edges (research period; approved bets; D017 units
m Q, Q = 1/(c_q SL); fixed TP/SL stay broker-side; re-target at each bar close + 5.7 s at Moneta bid/ask; commission;
swap on units held; omega calibrated so the s.10.6 size = m at entry; size floored at 0). Check: the fixed arm reproduces
the s.4J.3 D017 run exactly (JPN vc +2.72). A smoke test with stand-in forecasts caught a closing-order bug (NaN marker)
before the real runs; fixed. One trial per edge.
| edge | arm | net | gross (mid, no costs) | daily Sharpe | net/maxDD | orders | bets closed early |
|---|---|---|---|---|---|---|---|
| GBPJPY | fixed | +2.28 | +2.62 | **0.84** | **4.25** | 5,476 | 0 % |
| | frozen f = expected price | -1.40 | +2.26 | -0.39 | -0.72 | 65,902 | 77 % |
| | frozen f = TP | +1.27 | +2.37 | 0.54 | 2.30 | 144,083 | 0 % |
| | updated f, d = .05 | -0.04 | +1.27 | -0.02 | -0.09 | 20,527 | 99 % |
| HK | fixed | +2.01 | +2.49 | **0.81** | **3.42** | 5,142 | 0 % |
| | frozen f = expected price | -0.59 | +3.15 | -0.23 | -0.35 | 54,192 | 77 % |
| | frozen f = TP | +1.13 | +2.33 | 0.57 | 2.17 | 105,954 | 0 % |
| | updated f, d = .01 | -0.61 | +0.41 | -0.73 | -0.61 | 32,483 | 96 % |
| JPN vc | fixed | +2.72 | +3.17 | **1.15** | **4.56** | 1,932 | 0 % |
| | frozen f = expected price | +0.63 | +2.37 | 0.29 | 0.72 | 25,517 | 73 % |
| | frozen f = TP | +1.67 | +2.46 | 0.87 | 2.83 | 42,515 | 0 % |
| | updated f, d = .05 | +0.44 | +1.11 | 0.42 | 1.15 | 6,707 | 92 % |
(d = 0 / .01 / .05 differ by < 0.1 Sharpe; d cuts orders 2-5x.) Finding: fixed brackets beat every s.10.6 version on all
three edges, before and after costs. Why: (1) taking profit step by step gives up the 2:1 payoff, and step A showed
in-trade continuation (winning trades tend to keep winning), so holding winners to TP is what pays; (2) the updated
forecast's average remaining R is negative on GBPJPY / HK (-0.08 / -0.07 over all in-trade rows), so with the size
floored at 0 it closes 92-99 % of bets early and loses the payoff the entry signal earned (gross P&L 52-84 % lower);
(3) spread on 3-26x more orders. Limits: one forecaster (trained on all events, not only approved ones), research period,
current features. A "cut only the losers" rule is not in the book (owner deferred trader exits).

**Population flaw found by the owner (2026-10-26).** Steps 2, A and the step-3 forecaster were trained and judged on the
in-trade rows of ALL primary events (approved + rejected); live, an in-trade model only ever sees approved trades.
Uniqueness-weighted mean remaining R, approved vs rejected rows: GBPJPY -0.010 vs -0.183, HK -0.057 vs -0.108, JPN vc
+0.178 vs -0.048 (only 18 % of its rows approved), JPN rs -0.012 vs +0.067, XAUUSD rs +0.058 vs +0.007, XAUUSD vc +0.043
vs -0.003. The rejected rows dragged the forecast down, which is the main reason the updated-forecast arm closed 92-99 %
of bets early, so the step-3 updated-forecast verdict is NOT valid for live. Still valid: step 1, s.4J.3 averaging, the
limit-price test (all on approved bets) and the frozen-forecast arms (no forecaster).
**Re-run (pre-registered, owner go-ahead 2026-10-26):** step A with `--approved-only` on all 6 edges (train and test on
approved trades; same GO rules), then step 3 with `--approved-only` on every edge that is GO_state there. Caveat: the
approval threshold was chosen on the same rows (optimistic); JPN vc has 966 approved trades. Logs
`logs/ch10_stepA_approved_20261026/`, `logs/ch10_step3_approved_20261026/`.
**Standing rule (owner 2026-10-26):** any in-trade forecaster is trained and judged on live-approved trades only.
Re-run results (reports `*_approved.json` in each edge's feature_importance_audit; trials logged):
- Step A approved-only: GO_state on GBPJPY (R2 2.3 %, gain over zero +0.036, CI +0.020..+0.051), HK (0.9 %, +0.016,
  +0.001..+0.031), JPN vc (2.8 %, +0.048, +0.004..+0.095); not on JPN rs, XAUUSD rs, XAUUSD vc. Features beat state only on
  XAUUSD vc (+0.0067, +0.0003..+0.0137), where state itself does not beat zero. Approved mean remaining R: GBPJPY -0.059,
  HK -0.033, JPN vc +0.089.
- Step 3 approved-only forecaster (daily Sharpe; fixed / frozen f = TP / updated d = 0): GBPJPY 0.84 / 0.54 / 0.01,
  HK 0.81 / 0.57 / -0.59, JPN vc 1.15 / 0.87 / 0.56; updated arm still closes 75-98 % of bets early, gross P&L 36-79 %
  below fixed before costs. The correction helped (JPN vc 0.42 -> 0.56) but fixed brackets still win on all three, before
  and after costs. A 1-3 % R2 forecast is real information but too weak to pay for giving up the 2:1 payoff.

## 4K. AFML Ch.11 — the dangers of backtesting (audit 2026-10-26, decisions pending)

Book pages: Ch.11 = pdf 177-186 = pp.151-160 (read line by line from the extracted text). MLAM revisits it in Ch.8
s.8.1-8.3 pp.105-107 (selection bias under multiple testing; "backtest hyperfitting" = a firm selecting among researchers'
already-overfit backtests; precision / recall under multiple testing: with 1 true strategy in 100, alpha 0.05 and beta
0.2, a p-value of 0.05 means a false discovery rate of about 86 %). MLAM s.8.4-8.8 (False Strategy theorem, DSR,
effective number of trials, FWER) are taken with AFML Ch.14.
| § (page) | Book | Quantara | Status |
|---|---|---|---|
| 11.1-11.2 (pp.151-152) | a backtest is a hypothetical, not an experiment; use it as a sanity check (bet sizing, turnover, costs, scenarios); seven sins (Luo et al. 2014) + other errors | see the sins rows below | — |
| sin 1 survivorship (p.152) | universe must include what died | single instruments, no delistings; BUT the edge universe itself is a survivor set: 548 non-deployable edge folders deleted 2026-10-16; 461 family-side-asset attempts in config/cross_asset_thesis_coverage.json | trial-count issue -> Ch.14 |
| sin 2 look-ahead (p.152) | timestamps, release dates, backfill | features as-of bar close, entry 5.7 s later (Ex. 11.2 avoided), causal walk-forward HMM, frozen FFD d (d itself chosen on the full research history, D031, mild); calibrator / p_be / live threshold fitted on the same OOF rows | partial |
| sin 3 storytelling (p.152) | ex-post story for a random pattern | thesis cards were written after the backtests (Ch.1: post_hoc, draft_pending_user_review) | open |
| sin 4 data snooping (p.152) | training on the test set | purged CV + embargo (Ch.7); holdout one-look rule (D023); but thresholds / geometry chosen on the full research sample | partial |
| sin 5 costs (p.152) | hard to simulate | Moneta broker cost model, validated on 213 live fills (+-11 %), swap + commission | matches |
| sin 6 outliers (p.152) | few extreme outcomes | Gate E: XAUUSD edges earned mostly 2024-26, HK in 2022-23 and 2026 (regime concentration) | open -> Ch.12 |
| sin 7 shorting (p.152) | borrow cost / availability | CFDs: shorting available, swap charged | n/a |
| other errors (p.152) | non-standard metrics, stop-out limits / margin calls, funding costs, unrepresentative period | compound velocity (own metric) -> Ch.14; prop-firm daily / max loss limits handled outside the backtest (allocator overlays) -> Ch.15; swap = funding; research period 2022-2026 only | open |
| 11.3 (pp.152-153) | even a flawless backtest is probably a false discovery after many tests | many backtests per edge (see 11.5 #4) | -> Ch.14 |
| 11.4 (p.153) | backtesting is not a research tool; feature importance is; backtest only a fully specified model; purpose = discard, not improve | Gate A/B/C: a 405-cell geometry grid (horizon x EWM span x vertical x PT/SL) is BACKTESTED first, before features / ML, and the best cell is selected by backtest economics (compound velocity); failed edges were re-run with new geometry (v5.1 re-selection, shorter-barrier cycle) | **structural deviation** |
| 11.5 #1 (p.154) | models for asset classes, not single securities; "mistake X only on security Y" is likely false | thesis ported to all assets (coverage rule), but each edge is fitted per asset; deployable families: drawdownhazardcontinuation_short works on 1 of 10 assets tried (HK), volatilitycarry_long 1 of 5 (JPN; XAUUSD removed at Gate E), rangestructure_long 2 of 3 (JPN, XAUUSD), dailyrangecompletionfade_long 1 of 2 (GBPJPY) | **open** |
| 11.5 #2 (p.154) | bagging; if bagging hurts, it was overfit | sequential-bootstrap bagging, N = 1000 (Ch.6) | matches |
| 11.5 #3 (p.154) | do not backtest until research (Ch.1-10) is complete | Gate A-C backtests precede Steps 01-09 | deviation (same as 11.4) |
| 11.5 #4 (p.154) | record every backtest; estimate PBO on the final selection; deflate SR by the number of trials | per-edge research trial ledgers since 2026-10; older campaigns in config/*_edge_search_ledger.json; DSR used N = 2 (audit 2026-10-22) | -> Ch.14 |
| 11.5 #5 (p.154) | simulate scenarios, not one history | CPCV paths not used for selection; Gate E uses backtest windows | -> Ch.12 |
| 11.5 #6 (p.154) | if the backtest fails, start from scratch; do not reuse | failed theses were re-run with new geometry / thresholds | partial |
| 11.6 CSCV / PBO (pp.155-157) | M = T x N performance of ALL N configurations tried; S even (S = 16 -> C(16,8) = 12,870 splits; the book prints 12,780, a typo); IS winner's OOS rank logit; PBO = P[logit <= 0]; Figure 11.1 degradation + Prob[SR OOS < 0] | shared/research/gate_c_pbo.py + shared/backtest_statistics/afml_ch11_pbo_reference.py: S = 16, daily prorated log-growth (synchronous rows), selection metric = compound velocity on each block (book condition 2 met), PASS = PBO < 0.5 AND Prob[OOS <= 0] < 0.5 (our convention; the book gives no cutoff). BUT N = only the Gate-B-positive cells that Gate C replayed AND that had exec_mean_R > 0 and 99 % coverage (e.g. 42-255 of 405 cells): a selection on full-sample performance before CSCV | **deviation (N filtered)** |
| exercises 11.1-11.5 (p.158) | look-ahead in revised data, close-to-close execution, spurious correlation, seasonality storytelling, point-in-time fundamentals | 11.2 avoided (5.7 s delay); others n/a (no fundamental data) | — |
Code fix done 2026-10-26: the page / section citations in afml_ch11_pbo_reference.py were wrong (cited s.11.7-11.8 and
pp.157-167; Ch.11 has no s.11.7/11.8); corrected to s.11.6 pp.155-157; the stochastic-dominance diagnostic is from Bailey
et al. 2017a, not AFML Ch.11. No logic change; tests.test_gate_c_pbo_regression passes.
Last v5.1 Gate C PBO of the deployable edges (2026-10-17): XAUUSD rs 0.085 (Prob 0.058), HK 0.254 (0.282), XAUUSD vc 0.305
(0.171, removed at Gate E), JPN rs 0.401 (0.367), GBPJPY 0.454 (0.432), JPN vc 0.507 (0.433, KILLED_PBO, promoted by the
owner's borderline rule after Gate D AUC 0.563).

## 4L. AFML Ch.12 — backtesting through cross-validation (audit 2026-10-26, decisions pending)

Book pages: Ch.12 = pdf 187-194 = pp.161-168 (read line by line). MLAM does not revise it (MLAM s.1 p.8 refers to CPCV
as one of three answers to test-set overfitting, with FWER/DSR and Monte Carlo synthetic data, AFML Ch.13).
| § (page) | Book | Quantara | Status |
|---|---|---|---|
| 12.2 walk-forward (pp.161-162) | clear historical interpretation, reconcilable with paper trading, no embargo needed; but one path, sequence-dependent, early decisions use little data (on average 3/4 t0/T + 1/4 of the sample) | no WF backtest of the meta-model; Gate E holdout / live paper-trading play the WF role (one look) | n/a / Gate E |
| 12.3 CV backtest (pp.162-163) | every observation tested once by a model that saw everything else (purged + embargoed); equal-size training sets; one path, no historical meaning | — | — |
| 12.4 CPCV (pp.163-166) | N groups, k test groups, C(N, N-k) splits, phi[N,k] = k/N C(N, N-k) paths; purge + embargo; fit every split, ASSEMBLE THE phi PATHS and compute one Sharpe per path -> an empirical distribution of the Sharpe ratio; rule of thumb k = 2, N = phi + 1 | Step 03 runs CombinatorialPurgedCV(N = 5, k = 2): 10 splits, phi = 4 paths, training share 0.6, purge on label_t1, embargo max(100, 1 %); BUT every row's 4 path forecasts are AVERAGED into one probability (oof_pred_count = 4), calibration / thresholds / allocator / Gate E windows all use that single averaged series; no path is assembled, no per-path Sharpe, no distribution | **deviation** |
| 12.5 (pp.166-167) | E[max SR] of I zero-SR trials ~ sqrt(2 log I) x sigma; WF's high variance -> false discoveries; CPCV's mean over paths has variance phi^-1 sigma^2 (1 + (phi-1) rho) -> fewer false discoveries; a referee can demand CPCV(N, k) the researcher did not anticipate | selection is done on single-path economics (Gate C full history, allocator threshold on the averaged OOF path) | open |
| exercises 12.1-12.5 (pp.167-168) | reversed-series WF, warm-up selection, WF-vs-CV reporting, (N, k) for 1,000 paths, CPCV ethics | — | — |
Note: Gate C is not a WF, CV or CPCV backtest -- it is a full-history replay of the rule-based primary events for every
geometry cell (no fitted model), judged by the Ch.11 CSCV PBO. Ch.12 applies to the meta-model + sizing layer.

## 4M. AFML Ch.13 — backtesting on synthetic data (audit 2026-10-26, CLOSED 2026-10-26)

Book pages: Ch.13 = pdf 195-219 = pp.169-193 (read line by line; Table 13.1 labels its figures "16.x", a book typo for
13.x). MLAM does not revise it (MLAM s.1 p.8 lists Monte Carlo synthetic data as the third answer to test-set overfitting).
| § (page) | Book | Quantara | Status |
|---|---|---|---|
| 13.2 (pp.169-170) | trading rules (entry / exit thresholds, PT / SL) calibrated by historical simulation overfit the past; the chapter is about the EXIT corridor of an existing position | PT / SL / vertical multiples are chosen by Gates A-C over a 405-cell grid (historical simulation, judged by PBO) and are both the labels and the live exits | **this is the case the chapter targets** |
| 13.3 (pp.170-172) | brute force R* = argmax over Omega of SR(R) (eq. 13.1) is easy to overfit; Definition 2 (overfit rule: OOS below the median rule) = the PBO idea | Gate C winner = argmax compound velocity over 405 cells; PBO on all N (D051) | matches the book's diagnosis, not its cure |
| 13.4 (pp.172) | model the opportunity's P&L as a discrete O-U process P_t = (1-phi) E0[P_T] + phi P_(t-1) + sigma eps (eq. 13.2); stationary iff phi in (-1, 1); half-life tau = -log 2 / log phi | no stochastic model of the trade P&L | missing |
| 13.5 (pp.173-176) | algorithm: (1) estimate phi, sigma by OLS on pooled opportunities (eq. 13.5-13.7); (2) mesh of PT / SL (e.g. 20 x 20 from 0.5 to 10 sigma); (3) simulate many paths (e.g. 100,000) from the observed initial conditions, with a maximum holding period = the vertical barrier; (4) Sharpe ratio per node; (5a) optimal (PT, SL), (5b) optimal SL given a PT, (5c) optimal PT given a maximum SL; Snippets 13.1-13.2 | — | missing |
| 13.6 (pp.176-191) | zero equilibrium (market maker): small PT + large SL best at short half-life; positive equilibrium (position taker): optimal PT around 3-6 sigma with wide SL; as tau grows (phi -> 1, random walk) the Sharpe surface flattens and NO rule is optimal -- choosing one is overfitting; symmetric PT = SL is near neutral (keep in mind for triple-barrier labels) | Ch.10 step 1: in-trade giveback equals the random-walk rate; step A: weak continuation (R2 1-3 %) -- i.e. close to the random-walk case where the book says any chosen PT/SL is likely noise | open: the data has not been asked |
| 13.7 (p.192) | OTR conjecture: for an O-U price there is a unique Sharpe-maximising (PT, SL); derive it from synthetic data, not a backtest | — | — |
| exercises 13.1-13.6 (pp.192-193) | execution-trader exits, fit O-U to dollar bars, per-product OTRs, regime split (2009), recalibration frequency, parallelise | — | — |

## 4N. AFML Ch.14 — backtest statistics, with MLAM Ch.8 (audit 2026-10-26, CLOSED 2026-10-27)

Book pages: AFML Ch.14 = pdf 222-237 = pp.195-210; MLAM Ch.8 "Testing set overfitting" = pdf 111-130 = pp.105-124 (both
read line by line). MLAM revises AFML s.14.7.3: N and V[{SR_n}] are estimated by ONC-clustering the trials' return series
(E[K] = number of clusters, s.8.7.1 p.114) and by min-variance cluster portfolios, annualised per cluster (s.8.7.2
pp.114-116); MLAM adds FWER / Sidak and Type II errors under multiple testing (s.8.8 pp.116-123) as the second test.
Code read: portfolio_allocator/07b_dsr_psr_validation.py, 07c_backtest_statistics_validation.py,
shared/backtest_statistics/afml_ch14_ch15_reference.py, portfolio_live/decommission_rule.py (PSR), Gate C trade files.

| § (page) | Book | Quantara | Status |
|---|---|---|---|
| 14.3 (pp.196-197) | general characteristics: time range, AUM, capacity, leverage, max position, ratio of longs, frequency of BETS (same-side run = one bet, Snippet 14.1), avg holding period (Snippet 14.2), turnover, correlation to underlying | not reported; trades counted, not bets; 3 of 4 deployed edges long, no correlation-to-underlying check | missing |
| 14.4 (pp.198-199) | PnL, PnL from longs, TWRR annualised return, hit ratio, avg return of hits / misses | partly inside Gate C / 04c outputs, no single report | partial |
| 14.5 (pp.199-202) | HHI of + / - returns and of bets per month (Snippet 14.3), DD and TuW (Snippet 14.4), 95th percentiles | 07c, book formulas (R-space uses the dollars branch) | done (diagnostic) |
| 14.6 (pp.202-203) | broker fees / slippage per turnover, $ performance per turnover, return on execution costs | broker cost model exists (Gate C broker mode); ratios not reported | missing |
| 14.7.2 (pp.203-204) | PSR[SR*], T, skew, kurtosis (gamma4 = 3 Gaussian); > 0.95 | 07b + decommission rule, formula correct (07b docstring wrongly says Gaussian kurtosis 4.0; code gives 3.0) | done |
| 14.7.3 (pp.204-205) + MLAM 8.5-8.7 | DSR = PSR[E max SR] with N = ALL independent trials and V across their SRs; third law (Snippet 14.5): report every result with all trials | 07b uses the 6 DEPLOYED edges as the trials -> ONC K = 2, SR* tiny -> 6 / 7 "significant" (2026-10-22 run); the real search (Gate C 405 cells per edge, Gate A grids, ~196 thesis sides in git history, Optuna 30 trials, 04c threshold grid) is not counted; cluster series = plain sum, not min-variance | **deviation (major)** |
| 14.7.4 (pp.205-206) | annualised SR, information ratio, PSR > 0.95, DSR > 0.95 | DSR / PSR are diagnostics, not gates (07b / 07c "not a gate"); no IR | deviation |
| MLAM 8.8 (pp.116-123) | FWER via Sidak with E[K]; Type II / power at the implied SR | missing | missing |
| 14.8 (pp.206-207) | meta-labelling overlay scores: accuracy, precision, recall, F1, neg log-loss | Step 03 fold rows report them; Gate D uses AUC (Ch.9 decisions) | done (scattered) |
| 14.9 (pp.207-208) | PnL attribution by risk class, disjoint categories, index per category | missing | missing |
| D052 / Ch.12 | path SR distribution | D052 code done, awaits retrain; AFML Ch.14 gives no recipe for combining paths with PSR / DSR | open |
| D028 | judge the meta-model's value | open | open |

Trial data on disk / in git: per deployed edge, the full 405-cell Gate C trade files (daily return series per cell ->
book-exact MLAM ONC + min-variance clusters possible); Gate A grid JSONs for 196 thesis sides in git history (per-cell
net mean R, no std -> trial Sharpe ratios NOT exactly recoverable); per-edge research trial ledgers (Optuna).

### 4N.1 Owner decisions (2026-10-26) and the all-trials DSR test (pre-registered before any result)

Approved: fix 07b (all trials, MLAM min-variance cluster series, FWER / Sidak + Type II, kurtosis comment); extend 07c
with the missing Ch.14 statistics (correlation to the underlying, bets not trades first); D052 paths -> PSR per path +
share of paths with PSR > 0.95 (after the retrain); D028 -> DSR of the meta-filtered strategy vs taking every primary
signal; DSR > 0.95 as a gate (at promotion after Gate C / PBO and in 07b) to be decided on this test's results.
Owner note on Optuna: Ch.9 (D046) kept Optuna but replaced TPE with RandomSampler (randomised search, AFML s.9.3), still
30 trials per model; they are model-selection trials scored by purged-CV F1, not strategy backtests.

Test 1, Level 1 (`shared/research/dsr_all_trials.py`; diagnostic, one trial per edge), per deployed edge:
- trials = EVERY configuration replayed in the edge's full Gate C run (405 cells + control; AFML third law p.205;
  stamps = the D051 full-405 runs, XAUUSD rs = its 2026-10-17 4-stamp run); trial return series = the Gate C / PBO
  matrix (daily-prorated log(1 + 0.01 exec_r), calendar days, zero-filled);
- selected trial = the Gate C winner (argmax compound velocity among eligible cells, gate_c_pbo rule);
- E[K] = number of ONC clusters (MLAM s.8.7.1, clusterKMeansTop, max k = N - 1, n_init 10, seed 0) on the trials'
  correlation matrix (no denoising: the section does not ask for it; denoised count reported as a diagnostic);
- cluster series S_k = min-variance weights Sigma_k^-1 1 / 1' Sigma_k^-1 1 (MLAM p.114; pseudo-inverse when Sigma_k is
  singular -- flagged); aSR_k = SR_k sqrt(Frequency_k), Frequency_k = T_k / Years_k; V = V[aSR_k] / Frequency_selected
  (MLAM p.116);
- SR* = sqrt(V) ((1 - gamma) Z^-1[1 - 1/K] + gamma Z^-1[1 - 1/(K e)]) (AFML p.204 = MLAM p.109); DSR = PSR[SR*] on the
  selected series with its T, skew, kurtosis (gamma4 = 3 Gaussian); PSR[0] alongside;
- FWER (MLAM s.8.8.2): z = (SR - 0) sqrt(T - 1) / sqrt(1 - g3 SR + (g4 - 1)/4 SR^2), alpha = 1 - Z[z], alpha_K =
  1 - (1 - alpha)^E[K]; Type II (s.8.8.3) at alpha_K: beta = Z[z_alpha - theta], beta_K = beta^K, for SR_ = annualised
  1.0 (book example) and SR_ = the median trial SR (exercise 8.5);
- pass marks reported, not applied: DSR > 0.95 (AFML p.206), alpha_K < 0.05.
Level 2 (thesis search across the asset) is NOT run: its Sharpe-variance method will be proposed to the owner first.

### 4N.2 Test 1 Level 1 results (2026-10-26; per-edge `artifacts/feature_importance_audit/ch14_dsr_all_trials.json`, logs/ch14_dsr_20261026, trials logged)

| edge | trials | E[K] (ONC, = denoised) | selected SR ann. | PSR[0] | SR* ann. | DSR (book) | FWER alpha_K | beta_K at SR 1.0 |
|---|---|---|---|---|---|---|---|---|
| GBPJPY/dailyrangecompletionfade_long | 406 | 2 | 0.849 | 0.9551 | 0.236 | 0.8896 | 0.0878 | 0.146 |
| HK/drawdownhazardcontinuation_short | 406 | 2 | 0.772 | 0.9447 | 0.588 | 0.6479 | 0.1075 | 0.101 |
| JPN/rangestructure_long | 406 | 2 | 0.958 | 0.9807 | 0.146 | 0.9603 | 0.0382 | 0.215 |
| XAUUSD/rangestructure_long | 406 | 2 | 1.643 | 0.9999 | 0.062 | 0.9999 | 0.0002 | 0.866 |

Post-hoc robustness diagnostic (`shared/research/dsr_all_trials_sensitivity.py`, written AFTER seeing the above): the MLAM
min-variance cluster weights are numerically extreme (weights up to +-150, negative weights summing to -527: inverse of
near-singular covariances of hundreds of near-duplicate trials), so both cluster Sharpe ratios come out strongly negative
and V[SR] is the variance of two unstable numbers. DSR by variant:

| edge | A book (min-var) | B equal-weight clusters | C AFML V over all trials, N = E[K] | D AFML V, N = all trials (no ONC) |
|---|---|---|---|---|
| GBPJPY/dailyrangecompletionfade_long | 0.8896 | 0.8834 | 0.8624 | 0.0373 |
| HK/drawdownhazardcontinuation_short | 0.6479 | 0.6886 | 0.7585 | 0.0002 |
| JPN/rangestructure_long | 0.9603 | 0.9526 | 0.9428 | 0.2271 |
| XAUUSD/rangestructure_long | 0.9999 | 0.9996 | 0.9995 | 0.8718 |

Reading: HK and GBPJPY fail DSR > 0.95 and FWER < 0.05 under A, B and C; XAUUSD passes A-C; JPN is at the line
(0.943-0.960). D ignores MLAM's clustering (MLAM revises AFML here) and is a strict bound only. ONC finds E[K] = 2 on every
edge (raw and denoised). FWER depends only on PSR's z and E[K], so it is the same under A-C. Level 2 (thesis search) raises
E[K] and is expected to lower these values (V may also change). Shared module:
`shared/backtest_statistics/afml_ch14_dsr.py` (tests/test_afml_ch14_statistics.py reproduces MLAM's printed numbers).

### 4N.3 Owner decisions (2026-10-26, second round) and results

Approved: (1) read Lopez de Prado & Lewis (2018) and implement its robust cluster weighting exactly; (2) the DSR > 0.95 gate
(timing -- now vs at the retrain / RECERT -- asked back to the owner, not yet applied); (3) run Level 2.
Owner note: be accurate.

**Paper read** (SSRN 3167017, revised Nov 2018; published Quantitative Finance 19(9) 2019 pp.1555-1565): s.8.2 (p.11)
min-variance cluster weights; Appendix A.4 (pp.17-18): intra-cluster correlations are high so Sigma_k "may be
ill-conditioned and difficult to invert"; approximate Sigma_k by a constant-correlation matrix (rho = average off-diagonal
correlation) and invert by Sherman-Morrison -> w_i ~ 1/sigma_i^2 - rho sum_j(1/sigma_j) / ((1 + (N_k - 1) rho) sigma_i);
Snippet 4 getIVP(cov, use_extended_terms=True). Implemented exactly as `afml_ch14_dsr.get_ivp`, now the default
(`CLUSTER_WEIGHTING`); tests: exact on a true constant-correlation matrix, rho = 0 -> inverse variance.
Weights are now bounded (JPN: -0.03..+0.10; before: +-150). Cluster SRs stay negative because within a cluster the
higher-volatility trials (wider barriers) are also the higher-Sharpe ones (corr(sigma, aSR) +0.43 / +0.19) and
min-variance tilts away from them -- the method's stated purpose (paper s.8.2: "mitigate the adverse effects of any
strategies with larger variance").

**Correction (accuracy):** the Level 2 thesis counts first given to the owner (1 / 3 / 5 / 9) were wrong -- they counted
only theses whose v3 lab files reached git. The per-asset search ledgers (config/<asset>_edge_search_ledger.json; a thesis
counts only if it reached a backtest, floor failures excluded; re-runs counted once) give GBPJPY 51, HK 50, JPN 52,
XAUUSD 50 thesis-sides.

| edge | Level 1 DSR (406 Gate C trials, E[K] 2, LdP-Lewis weights) | FWER alpha_K L1 | Level 2 DSR (E[K] = 2 x theses, flagged approximation) | FWER alpha_K L2 | 07b on the allocator stream (L1 record; 10-17 allocator run) |
|---|---|---|---|---|---|
| XAUUSD rangestructure_long | 0.9983 PASS | 0.0002 | 0.3749 | 0.0079 | DSR 0.9999 PASS |
| JPN rangestructure_long | 0.9379 fail | 0.0382 | 0.2975 | 0.8683 | DSR 0.9138 fail |
| GBPJPY dailyrangecompletionfade_long | 0.6851 fail | 0.0878 | 0.0000 | 0.9908 | DSR 0.8487 fail |
| HK drawdownhazardcontinuation_short | 0.6498 fail | 0.1075 | 0.0000 | 0.9966 | DSR 0.8721 fail |

07b portfolio (trials = the 20 Step 07 HRP scenarios, E[K] 5): DSR 1.000 -- counts only the portfolio-level choice, not the
edges' own searches (MLAM s.8.1 hyperfitting), so it is not evidence on its own. 07b rewritten (all trials per edge from the
edge's record, fail closed without one; robust weights; FWER + Type II; calendar-day grid; kurtosis comment fixed);
07c extended (s.14.3 / 14.4 / 14.6 / 14.9). Records: per-edge ch14_dsr_all_trials.json (robust main + superseded variants
+ Level 2), logs/ch14_dsr_20261026, trial ledgers.

**Owner decisions 2026-10-27 (D054):** the DSR gate applies at the RECERT, after the book walkthrough and the retrain -- nothing is removed from edge_universe before; the gate uses Level 1 (each edge's own Gate C trials: DSR > 0.95 and FWER alpha_K < 0.05); Level 2 (thesis search) is reported alongside and does not gate.

### 4N.4 Correlation-to-underlying benchmark test (owner-approved 2026-10-27; pre-registered before any result)

Book: AFML p.197 -- a strategy significantly correlated with its underlying "is essentially holding or short-selling the
investment universe, without adding much value"; s.14.7.4 information ratio vs a benchmark. Design note found while
designing: comparing a trade with holding the underlying over the SAME interval gives zero difference by construction (a
trade's mid P&L is the underlying's move over its interval), so the benchmark is random TIMING:
- edge trades = the representative ledger's trades matched to the Step 04 OOF; edge return per trade = label_r (mid path,
  no costs -- the benchmark has no costs either);
- benchmark trade i = the same side, the same holding duration and the same stop distance in price, entered at a uniformly
  random bar time of the asset's bar file (entry + duration inside the data); R = side * (Close(t + dur) - Close(t)) /
  stop_distance (bar closes, last close at or before each time; no barriers -- for a martingale, barriers do not change the
  expectation, so the benchmark's mean is the drift ('beta') earned by holding that long);
- 1,000 random draws of the whole trade set, seed 0; report: edge mean R, benchmark mean R (mean over draws), excess mean
  R, p-value = share of draws whose mean >= the edge's mean, and the information ratio of the per-trade excess
  (label_r_i - mean benchmark R_i, summed by exit day, annualised with sqrt(365.25), calendar days zero-filled).
- Reading: excess ~ 0 with p >= 0.05 -> the edge's mean is explained by holding the underlying (beta); excess > 0 with
  p < 0.05 -> timing adds value. Diagnostic only; one trial per edge.
Tool: `shared/research/ch14_benchmark_vs_underlying.py`.

### 4N.5 Second full pass of Ch.14 (owner request 2026-10-27: "go through the whole chapter, touch everywhere")

The first pass went deep on s.14.7 and treated the rest as a checklist. Every statistic the chapter names, re-checked in the
code and data (07c second-pass output: logs/ch14_dsr_20261026/ch14_second_pass_07c_*.csv; allocator inputs = 2026-10-17 run):

| § (page) | statistic | Quantara now | result on the 4 deployed edges | status |
|---|---|---|---|---|
| 14.3 (p.196) | time range, "comprehensive number of regimes" | years + HMM regime share, universe vs taken | ~4 years (2022-2026); all 4 HMM states covered in universe and taken trades on every edge | done |
| 14.3 | average AUM | constant 04C sizing balance 100,000 (no external flows) | -- | done (adapted) |
| 14.3 | capacity | broker max lot / largest lot needed; the book's 'highest AUM at target performance' needs a market-impact model -- not available | lot headroom 770-1,670x | partial (flagged) |
| 14.3 | leverage; maximum dollar position | notional at 04C standalone sizing (lots x risk_per_1_lot x price / stop) | avg leverage 0.01-0.07x; peak position 0.16-0.93x AUM | done |
| 14.3 | ratio of longs | per bet | 1.0 (3 long-only edges), 0.0 (HK short) -- side-specialised by design | done |
| 14.3 | frequency of bets; Snippet 14.1 | bets, not trades | 366-529 bets (5-12 trades per bet), 91-128 bets / year | done |
| 14.3 | average holding period; Snippet 14.2 | exact port | 0.6-1.3 days | done |
| 14.3 | annualised turnover | 2 x notional traded / year / AUM | 8-69x | done |
| 14.3 | correlation to underlying | Pearson, daily | +0.45 GBPJPY, +0.37 JPN, +0.29 XAUUSD, -0.33 HK (all p ~ 0) + benchmark test (s.4N.4) | done |
| 14.4 (p.198) | PnL, PnL from longs, hit ratio, avg hit / miss (per bet) | 07c | hit ratio per bet 0.24-0.41; avg hit 6-22 R, avg miss -2 to -5 R | done |
| 14.4.1 | TWRR (geometric linking, external cash flows) | R units; geometric growth = 04C log-growth at f* (no external flows) | -- | adapted (flagged) |
| 14.5.1 (p.199-201) | HHI of + / - bet returns and bets per month; Snippet 14.3 | now on BETS (the daily-active version kept as a proxy) | h+ 0.003-0.012, h- 0.002-0.003, h[t] 0.001-0.003 (no concentration) | done |
| 14.5.2-14.5.3 (p.201-202) | DD, TuW, 95th percentiles; Snippet 14.4 | 07c | -- | done |
| 14.6 (p.202-203) | broker fees / slippage per turnover, $ performance per turnover, return on execution costs | total cost (Moneta) + split from OOF (spread+slippage+commission vs swap) | cost 0.031-0.048 R / trade; return on execution costs 2.8-6.4x | done |
| 14.7.1-14.7.3 (p.203-205) | SR, PSR, DSR, third law | D054 (s.4N.1-4N.3) | Level 1: XAUUSD pass, JPN / GBPJPY / HK fail | done; gate at RECERT |
| 14.7.4 (p.205-206) | annualised SR, information ratio, PSR / DSR > 0.95, on absolute or relative returns | IR vs random-timing benchmark (s.4N.4) | IR 1.35 HK, 1.48 XAUUSD, 1.01 GBPJPY, 0.23 JPN | done |
| 14.8 (p.206-207) | accuracy, precision, recall, F1, neg log-loss of the overlay; Table 14.1 degenerate cases; label switch only when positives dominate | at the live threshold on every primary event; Step 02/03 use zero_division = 0 (Table 14.1 / scikit-learn) | positives 38-48 % (F1 appropriate); **JPN take rate 1.000 at its live threshold 0.36 -> the meta-model passes every event** | done |
| 14.9 (p.207-208) | attribution: disjoint categories, portfolio index vs universe index | universe = all primary events, portfolio = taken; categories session, Event_Vol tercile, HMM regime | excess positive in most categories for GBPJPY / HK / XAUUSD; JPN 0 everywhere (takes everything) | done |
| exercises 14.1-14.7 | | the book prints no answers; MLAM's worked numbers reproduced in tests | -- | n/a |

**Benchmark test results (s.4N.4; per-edge ch14_benchmark_vs_underlying.json; trials logged):**
| edge | edge mean R (mid) | random-timing benchmark (beta) | excess | beta share | p | IR |
|---|---|---|---|---|---|---|
| HK drawdownhazardcontinuation_short | 0.191 | -0.002 | 0.193 | ~0 % | < 0.001 | 1.35 |
| XAUUSD rangestructure_long | 0.231 | 0.084 | 0.148 | 36 % | 0.003 | 1.48 |
| GBPJPY dailyrangecompletionfade_long | 0.184 | 0.048 | 0.136 | 26 % | 0.001 | 1.01 |
| JPN rangestructure_long | 0.182 | 0.154 | 0.029 | **84 %** | 0.285 | 0.23 |
Reading: on HK, XAUUSD and GBPJPY timing adds value beyond holding the underlying; JPN's result is mostly the index's rise
(long Nikkei 2022-2026) and its meta-model takes every event at the live threshold -- consistent findings. JPN used the
3,393 ledger trades that match the re-exported OOF (ledger predates the re-export).
Bug found and fixed in this pass: the sizing statistic built pd.Series from a label-indexed Series (all NaN); unit test added.

**Ch.14 closed (owner 2026-10-27):** legacy review D001 / D004 / D009 / D012 superseded, D002 closed with naming debt; runbook Revision 22 (Step 10J, Appendix 39); ledger addendum AFML_CH14. Next: AFML Ch.15 (owner); the D050 comparison remains queued.

## 4O. AFML Ch.15 — understanding strategy risk (audit 2026-10-27, CLOSED 2026-10-27)

Book pages: Ch.15 = pdf 238-247 = pp.211-220 (read line by line, Snippets 15.1-15.5, exercises 15.1-15.6). MLAM has no
strategy-risk chapter (no revision). Code read: shared/backtest_statistics/afml_ch14_ch15_reference.py (bin_hr, bin_freq,
prob_strategy_failure, strategy_risk_report), portfolio_allocator/07c (applies it), portfolio_live/decommission_rule.py
(strategy_risk_diagnostic).

| § (page) | Book | Quantara | Status |
|---|---|---|---|
| 15.1 (p.211) | PT / SL exits -> model outcomes as a binomial process; every strategy has an implicit stop-loss (margin call / pain); goal: which (frequency, odds, payouts) are uneconomic and how vulnerable the strategy is to small changes | triple-barrier edges (PT / SL / vertical); the prop firm's daily / max loss limits are the implicit portfolio stop | concept matches |
| 15.2 (pp.211-213) | symmetric payouts: theta[p, n] = (2p - 1) / (2 sqrt(p(1 - p))) sqrt(n); Sharpe is a function of PRECISION not accuracy; p = .55 needs 396 bets / yr for theta = 2; p = (1 + sqrt(1 - n / (theta^2 + n))) / 2; weekly bets need p = .6336 | -- | not used (our payouts are asymmetric) |
| 15.3 (pp.213-216) | asymmetric payouts: theta = ((pi+ - pi-) p + pi-) / ((pi+ - pi-) sqrt(p(1 - p))) sqrt(n) (example 1.173); implied precision binHR (Snippet 15.3, quadratic a, b, c; example p = .72 for theta = 2: "very small change in p" -> vulnerable); implied frequency binFreq (Snippet 15.4, check binSR for extraneous root) | bin_hr = Snippet 15.3 (docstring cites the wrong section / snippet number); bin_freq checks via bin_hr instead of binSR (equivalent round trip, not literal); no binSR function | formulas right, citations / literal form to fix |
| 15.4 (pp.216-217) | p is set by the market, {pi-, pi+, n} by the PM; p_theta* = max{p : theta <= theta*}; STRATEGY risk (P[p < p_theta*]) is not PORTFOLIO risk | 07c reports it; nothing monitors precision live against p_theta* | partial |
| 15.4.1 (p.217) | algorithm: pi- = E[pi | pi <= 0], pi+ = E[pi | pi > 0] (or EF3M mixture); n = T / years over a time series of BET outcomes; bootstrap: I draws of floor(n k) bets with replacement, k = years investors use to assess (e.g. 2), p_i = share > 0; KDE of p_i; "for sufficiently large k" approx N[p, p(1 - p)]; strategy risk = integral of f[p] below p_theta* | 07c: snippet's approximation, ACTIVE DAILY returns (not bets), pi- excludes zeros (book <= 0), theta* = own SR and 1.0; decommission diagnostic: spread sqrt(p(1-p)/T), freq = bets in the sample | **deviations** (method, unit, pi-) |
| 15.4.2 (p.217) | "Typically we would disregard strategies where P[p < p_theta*] > .05 as too risky ... In order to be deployed, the strategy developer must find a way to reduce p_theta*" | diagnostic only, no gate | **book rule not applied** |
| Snippet 15.5 (p.218) | probFailure: risk = norm.cdf(thresP, p, p(1 - p)) "approximation to bootstrap" | copied exactly in prob_strategy_failure | **book-internal inconsistency**: the snippet's scale p(1 - p) is not the spread of the bootstrap it approximates (that spread is sqrt(p(1 - p) / floor(n k))); data below |
| p.218 | complementary to PSR: PSR ignores which parameters the PM controls; this method studies viability under {pi-, pi+, n} | PSR in decommission rule | -- |
| ex. 15.1-15.2 (p.219) | viability; required p, n, PT, SL; sensitivity of SR to 1 % changes; lowest-hanging fruit; parameter interactions | not reported | missing |
| ex. 15.3-15.5 | mixture-of-Gaussians moments (EF3M), PSR[1] vs P[p < p_theta*=1], which is more accurate | -- | -- |
| ex. 15.6 | revisit Ch.13 OTR asymmetry and implied p | D053 (no OTR) | covered |

**Audit evidence (2026-10-27, representative ledger of the 10-17 allocator run, Moneta executed_r; logs/ch15_audit_evidence_20261027.txt):**
P[p < p_theta*] at theta* = 1 (annualised), k = 2 years:
| edge | unit | p | p*(theta* = 1) | snippet p(1 - p) | decommission sqrt(p(1 - p)/T) | book bootstrap floor(nk) + KDE | normal sqrt(p(1 - p)/nk) |
|---|---|---|---|---|---|---|---|
| XAUUSD rs | bets (459) | 0.362 | 0.283 | 0.366 | 0.0002 | **0.005** | 0.005 |
| GBPJPY drc | bets (529) | 0.405 | 0.385 | 0.468 | 0.184 | **0.271** | 0.265 |
| HK dhc | bets (400) | 0.255 | 0.236 | 0.460 | 0.193 | **0.270** | 0.268 |
| JPN rs | bets (366) | 0.238 | 0.232 | 0.487 | 0.398 | **0.437** | 0.428 |
| all four | trades (2,251-4,428) | 0.39-0.49 | 0.36-0.42 | 0.39-0.46 | <= 0.0005 | **<= 0.009** | <= 0.010 |
Readings: (1) the snippet's shortcut gives ~0.4-0.5 for every edge (uninformative); the book's own bootstrap agrees with
the normal approximation that uses sqrt(p(1 - p)/floor(nk)); (2) the unit decides the verdict: on bets (the book's IID
unit, Ch.14 p.196) only XAUUSD passes the .05 rule; on trades (5-12 overlapping trades per bet, not independent) all pass;
(3) the annualised theta on trades (2.6-4.5) is inflated vs bets (1.1-2.75) for the same reason.

### 4O.1 Owner decisions (2026-10-27) and implementation

Approved: the book's s.15.4.1 algorithm replaces the Snippet 15.5 shortcut (07c and the decommission diagnostic); unit =
bets (trades alongside); the .05 rule gates at the RECERT (nothing removed before); live precision vs p_theta* in the
decommission report (diagnostic); exercise 15.1-15.2 sensitivity report; citation / binSR / binFreq / pi- fixes.
theta* and k: owner asked for a recommendation. Proposed (config/strategy_risk.json, awaiting confirmation): theta* = 0 --
active_account_rules.json (phase 1: +5 % target, -10 % static, -5 % daily, 3 minimum days, NO time limit): with no
deadline an edge fails the challenge regardless of sizing only if its true SR <= 0 (book p.216 uses p_theta*=0 as its own
example); how FAST it passes is the 08 optimizer's portfolio question (strategy risk is not portfolio risk, p.217);
k = 2 (the book's example; viability is judged over the funded account's life; one edge's ~10 bets in a few weeks cannot
inform a precision estimate). theta* = 1 and k = 1 reported alongside.

Implemented: reference module rewritten (bin_sr, bin_hr with relative-tolerance discriminant, literal bin_freq,
sr_symmetric, implied_precision_symmetric, bet_outcome_parameters with pi- over <= 0, prob_precision_below,
prob_strategy_failure, strategy_sensitivity, psr_vs_annual_target, mix_gaussians; the snippet shortcut kept only as a
flagged diagnostic); 07c strategy risk on bets + trades with the config; decommission_rule.strategy_risk_diagnostic (live
bet precision vs p_theta* from the backtest bets; to_bets groups overlapping trades; allocation logic unchanged).
Tests: tests/test_afml_ch15_strategy_risk.py reproduces every number the chapter prints (0.1005, 396 bets, p = .6336,
theta = 1.173, p = .72, p_theta*=0 = 2/3) plus Snippets 15.1 (Monte Carlo), 15.2 (variance factorisation), 15.5
(mixture through the algorithm); tests/test_decommission_rule.py updated. Two bugs found and fixed while testing: the
theta* = 0 discriminant (floating point) and sensitivities that are trivial at theta* = 0 (theta* = 1 reported too).

**Results (pre-retrain, 10-17 allocator ledger, bets):**
| edge | bets | n / yr | p | pi+ | pi- | theta | p_theta*=0 | margin | P[fail] theta*=0, k=2 | PSR[0] same bets | P[fail] theta*=1 | P[fail] k=1 | trades P[fail] |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| GBPJPY drc | 529 | 128 | 0.405 | 8.20 | -4.27 | 1.44 | 0.342 | 0.062 | 0.023 | 0.988 | 0.271 | 0.076 | 0.0001 |
| HK dhc | 400 | 102 | 0.255 | 13.37 | -3.22 | 1.41 | 0.194 | 0.061 | 0.022 | 0.989 | 0.271 | 0.077 | 0.0000 |
| JPN rs | 366 | 91 | 0.238 | 21.68 | -5.01 | 1.12 | 0.188 | 0.050 | **0.057** | 0.964 | 0.437 | 0.135 | 0.0000 |
| XAUUSD rs | 459 | 121 | 0.362 | 6.20 | -1.97 | 2.75 | 0.242 | 0.120 | 0.000 | 1.000 | 0.005 | 0.003 | 0.0000 |
Exercise 15.2 sensitivities (theta* = 1): +1 % precision raises theta 2.8-6.3 %, +1 % PT or 1 % tighter SL 1.5-3.6 %,
+1 % frequency 0.5 % -> precision is the lowest-hanging fruit on every edge. Live diagnostic on the pre-stop closed trades:
6-9 live bets per edge -> uninformative yet (as expected). New finding D056: the live decommission PSR counts overlapping
trades as bets (HK 60 trades = 9 bets) -- owner decision.

### 4O.2 Owner decisions (2026-10-27, second round) and Ch.15 close

theta* = 0 and k = 2 approved (config/strategy_risk.json). D056 approved and implemented: the live decommission rule's
PSR[0], PSR[SR_bt], 30-bet minimum and backtest benchmark use Snippet 14.1 bets (backtest: GBPJPY 2,738 trades -> 529
bets, HK 2,571 -> 400, JPN 4,428 -> 366, XAUUSD 2,251 -> 459; SR per bet 0.096 / 0.100 / 0.084 / 0.172 vs per trade
0.103 / 0.106 / 0.077 / 0.165); shadow bets keep entry / exit / side; verdicts report bets and trades. Effect: an edge needs
30 independent bets, not 30 overlapping trades, before any verdict.
Owner question on "entry signal": the wording was loose -- precision comes from the information at the event (the
thesis / primary model, AFML s.3.6, and the meta-model's features), not from timing the fill after it; waiting for a
better price was tested (Ch.10 limit price neutral, D049; Ch.13 in-trade random walk; 5.7 s latency not eroding);
confirmation-style entries are legitimate only as new pre-registered theses / meta features, counted as trials.
Ch.15 closed: runbook Revision 23 (Step 10K, Appendix 40); ledger addendum AFML_CH15.

## D050 decision analysis and decision (2026-10-29)

D050 (recorded AFML Ch.11): Gates A-C search a 405-cell geometry grid and keep the best cell. The book's two paths for
the trading rule: (1) avoid calibrating it on history -- its preference (AFML Ch.13 p.170, via theory or synthetic-data
OTR; Ch.13 found no OTR for our edges, D053); (2) search, then measure and deflate the search -- PBO over all N trials
(AFML s.11.6), DSR with all trials and the third law (AFML s.14.7.3 p.205, MLAM s.8.7).

Evidence from each deployed edge's full Gate C run (logs/d050_comparison_20261029.txt; exec_mean_R per cell, Moneta costs):
| edge | cells | winner | winner mean R | median cell | share of cells > 0 | PSR[0] if no search (K = 1) | DSR with the 406 trials |
|---|---|---|---|---|---|---|---|
| GBPJPY drc | 405 | H6_E100_V168_PT2_SL1 | 0.087 (rank 1) | -0.008 | 34 % | 0.955 | 0.685 |
| HK dhc | 405 | H8_E100_V168_PT2_SL1 | 0.074 (rank 1) | -0.042 | 8 % | 0.945 | 0.650 |
| JPN rs | 405 | H8_E100_V96_PT2_SL1 | 0.099 (rank 1) | +0.008 | 68 % | 0.981 | 0.938 |
| XAUUSD rs | 405 | H8_E100_V16_PT2_SL1 | 0.135 (rank 26) | +0.050 | **100 %** | 1.000 | 0.998 |
The search's price is the gap between the last two columns.

Decision history (all 2026-10-29): option B (geometry fixed on the card) approved, then option C (2-3 pre-registered
geometries) preferred; the owner then asked whether the book allows searching and paying for it -- it does (path 2) --
and chose **option A**: "I want us to go back and use this option 2, i prefer to search the 405 grid cells and pay for it
... as long as the book says we can still follow this path". The B / C drafts were reverted (commit d9f7d230 reverted;
C never committed).

**GEOMETRY RULE D050-A is IN FORCE.** Canonical text: `docs/mldp/rules/GEOMETRY_RULE_D050A.md` (the runbook editor
`docs/tools/edit_runbook_rev24.py` reads it into the `[RULE D050-A]` block of Section 6A Step 7). In short: thesis card
first (pre-registered, without the geometry); search the runbook's 405 cells; pay for every trial -- PBO over every
replayed configuration (D051), all-trials DSR > 0.95 and FWER < 0.05 (D054), strategy risk <= .05 on bets (D055); trials
accumulate across re-selection runs and are never reset.

**D057 (owner: do not forget):** the allocator's 04c selects one probability threshold out of the 04b grid per edge -- a
selection that PBO can audit and that adds trials to the DSR record. Raise it when the walkthrough reaches the
portfolio_allocator (AFML Ch.16 onwards). Register D057; backlog.

## 4P. AFML Ch.16 — machine learning asset allocation, with MLAM Ch.7 (audit 2026-10-29, CLOSED 2026-10-30)

Book pages: AFML Ch.16 = pdf 248-272 = pp.221-245 (sections 16.1-16.8, Snippets 16.1-16.5, Appendices 16.A.1-16.A.4,
exercises 16.1-16.5; read line by line). MLAM Ch.7 "Portfolio construction" = pdf 98-110 = pp.92-104 (with the Ch.2
denoising it uses). **MLAM revises AFML here** (owner rule: MLAM governs where it revises): p.95 "The purpose of HRP was
not to deliver an optimal allocation, but merely to demonstrate the potential of ML approaches"; s.7.6 introduces NCO;
s.7.8 p.104: run a Monte Carlo on your own inputs and "apply opportunistically whatever optimization method is best
suited in a particular setting"; s.7.6.1: denoise before clustering "for large matrices, where T/N is relatively low".
Code read in full: portfolio_allocator/07_hrp_linkage_robustness.py; consumers 08 / 09 / 10 (SIMULATE_LINKAGE_METHODS =
["single"]), portfolio_live/live_manifest.json (final_allocator_selection linkage_method single; portfolio_weight = the
HRP weight, used as the edge's share of the risk budget); inputs 06 (DATE_AGGREGATION = exit_date).

| § (page) | Book | Quantara | Status |
|---|---|---|---|
| 16.1-16.3 (pp.221-223) | quadratic optimisers (CLA) are unstable, concentrated, underperform OOS; Markowitz's curse (condition number); need >= N(N+1)/2 IID observations | 6 edges, 1,511 days (T/N ~ 250); raw-corr condition number 4.9 (low: the curse is mild for us) | context |
| 16.4.1 (pp.224-229), Snippet 16.1 | rho -> d = sqrt((1 - rho)/2) (proper metric, 16.A.1); THEN d~ = Euclidean distance between COLUMNS of D (a distance of distances); single linkage on d~ (Snippet 16.1 passes D as observations to sch.linkage, i.e. clusters on d~) | `linkage(squareform(dist), method)` clusters on d itself, not d~ | **deviation** |
| 16.4.1 linkage | single (nearest point); alternatives allowed (s.16.7) | 5 methods computed; live and 08 use single; a robustness_preference_score (in-sample Calmar / total R / concentration) ranks methods -- used only by reports | ok (live = single); the ranking is a performance-based selection (diagnostic) |
| 16.4.2 (p.229), Snippet 16.2 | quasi-diagonalisation getQuasiDiag | get_quasi_diag, same logic (pd.concat for py3) | done |
| 16.4.3 (pp.229-230), Snippet 16.3 | recursive bisection by halves; cluster variance = IVP-weighted; alpha = 1 - V1/(V1 + V2); constraints may replace 3c-3e | recursive_bisection, get_cluster_variance, IVP: identical | done |
| 16.5 (pp.231-233) | numerical example (CLA vs HRP vs IVP, Table 16.1) | no reproduction test | missing (test) |
| 16.6 (pp.234-236), Snippet 16.5 | OOS Monte Carlo: HRP vs CLA vs IVP, 260-obs lookback, 22-obs rebalance, 10,000 runs, compare OOS variance | none on our inputs | missing |
| 16.7 (pp.236-238) | variations: other distances / clustering / weights, top-down split by stage-1 clusters, forecasted returns, Ledoit-Wolf, Black-Litterman | denoise + detone (MLAM Ch.2) before HRP | see MLAM |
| MLAM 7.6.1 (p.98) | denoise before clustering only for large matrices with low T/N | denoise + detone always applied; with N = 6 the Marcenko-Pastur fit sits on its boundary (fitted noise variance 0.99999), i.e. not meaningful | **deviation from MLAM's guidance** |
| MLAM 7.4 / 7.8 (pp.95, 103-104) | HRP was a demonstration; choose the allocation method by Monte Carlo on your own inputs (NCO, Markowitz, HRP, ...) | HRP is fixed by design | **revision not applied** |
| ex. 16.1(a) (p.244) | align PnL series to the average bet frequency (downsampling) before the covariance | daily R booked on the EXIT day: 55-83 % zero days per edge; overlapping open trades show no co-movement until they close | **deviation** |
| ex. 16.2-16.3 | HRP / CLA / IVP on V; spectral re-scaling stress (which method is most affected) | none | missing (test) |
| ex. 16.4 | long-short HRP (weights sum to 0) | n/a: side-specialised edges, all weights >= 0 | n/a |
| ex. 16.5 / s.16.7 | expected returns in HRP | HRP ignores mu; MLAM's NCO max-Sharpe uses mu | open (test) |
| reports | -- | 07b / 07c evaluate the AVERAGE-linkage portfolio file as "the portfolio", while live trades SINGLE | **inconsistency** |
| D057 (owner) | -- | 04c's threshold selection per edge is a selection that PBO can audit | raise now (allocator) |

Inputs used above are the 2026-10-17 allocator outputs (6 edges incl. JPN / XAUUSD volatilitycarry, since removed).

### 4P.1 Pre-registration of tests T1-T4 and D057 (owner approved 2026-10-29: "run tests 1-4 and include D057"; written before any result)

All diagnostic: nothing in the allocator or live changes until the owner decides on the results. Edge set = the 4 deployable
edges of config/edge_universe.json (GBPJPY drc_long, HK dhc_short, JPN rs_long, XAUUSD rs_long); trades = the deployed
representative threshold of each (portfolio_allocator/outputs/v2_representative_trade_ledger_core_plus_watchlist.csv,
2026-10-17 run), raw R (the live return mode). Tools: shared/research/ch16_allocation_tests.py,
shared/research/allocator_threshold_pbo.py (in the repo, not the scratchpad). Seeds fixed (0).

**S0 sanity (must pass before any result is read):** the exit-day matrix rebuilt from the ledger equals 06's
profile_return_streams_daily_core_plus_watchlist_compound_velocity.csv for the 4 columns; 07's hrp_allocation (single) on
06's 6 columns reproduces the live manifest portfolio_weight values (|diff| < 1e-9).

**Methods compared** (all long-only except where the book's method is unconstrained):
EW (1/N); IVP (Snippet 16.4 getIVP); HRP-book (Snippet 16.4: raw cov / corr, d = sqrt((1 - rho)/2), `sch.linkage(dist,
'single')` on the square matrix, i.e. single linkage on d~ = Euclidean distance between columns of D, getQuasiDiag,
getRecBipart); HRP-current (07 hrp_allocation, single: denoise + detone, linkage on d); CLA min-variance with 0 <= w <= 1
(Snippet 16.5 getCLA = the last turning point; solved as the equivalent QP); Markowitz min-variance unconstrained (MLAM
optPort, mu None); NCO min-variance (MLAM Snippet 7.6, clusterKMeansBase, maxNumClusters = N/2, n_init 10). For exercise
16.5 (expected returns): Markowitz max-Sharpe and NCO max-Sharpe (MLAM s.7.7.2, optPort with mu).

**T1 book-exact HRP vs current (s.16.4).** On the current input (exit-day daily R): weights of HRP-book and HRP-current, plus
the 2 x 2 factorial {d, d~} x {denoise + detone on, off} to attribute any difference; report weights, quasi-diagonal order,
max |dw|.

**T2 return alignment (exercise 16.1a).** A exit-day daily (current 06); B daily prorated (each trade's R spread evenly over
the calendar days entry..exit, as gate_c_pbo.py does); C weekly (A summed by week); D weekly prorated (B summed by week).
Report per alignment: T, zero-day share per edge, bets per week, correlation matrix, condition number, and every method's
weights. Exercise 16.1(c)-(d): clusters and the quasi-diagonal order per alignment (table, not plot).

**T3 Monte Carlo on our own inputs (AFML s.16.6 Snippet 16.5; MLAM s.7.7-7.8 and footnote 24 p.101).** Run on alignments A
and B.
- M1 (AFML Snippet 16.5 design): 10,000 iterations of nObs 520 days, sLength 260, rebal 22 (pointers 260, 282, ..., 502).
  Each window: cov / corr of the last 260 rows -> weights -> next 22 rows out of sample. Statistic per iteration = terminal
  OOS PnL = sum of the daily portfolio R (our unit is R, which is additive; the book's (1 + r).cumprod - 1 assumes fractional
  returns -- noted adaptation). Report std and variance across iterations and variance / variance(HRP-book) - 1 (the
  book's printout), 95 % bootstrap CI of each variance ratio (1,000 resamples of the iterations). Two generators: G1
  circular block bootstrap of our actual daily rows (block 22; keeps fat tails, zero days and cross-dependence); G2
  multivariate normal with the full-sample (mu, V) (the book's Gaussian setting).
- M2 (MLAM Snippets 7.8-7.9): true (mu0, V0) = full-sample estimates; 1,000 simulations of nObs in {260, T} Gaussian draws;
  RMSE of each method's weights vs the true min-variance weights (MLAM's measure; max-Sharpe methods vs the true max-Sharpe
  weights) and, as our addition, RMSE vs each method's own weights on the true V0 (estimation stability).
- **Decision rule:** a method is called better than HRP-current only if its M1 OOS variance is lower under BOTH generators
  with the 95 % CI of the variance ratio excluding 0, on alignment B or A, and its M2 own-RMSE is not higher. Otherwise
  HRP stays and only book-fidelity fixes are proposed.

**T4 spectral rescaling stress (exercise 16.3).** V = full-sample cov (alignments A and B); V = W Lambda W'; 1,000 draws (seed
0) of eps ~ U[0, 1]^N, Lambda~ = N eps Lambda / sum(eps), V~ = W Lambda~ W^-1, corr from V~. Per method: mean over draws of
the RMSE and of the max |w(V~) - w(V)|. Answer: which method is most affected.

Exercise 16.4 (long-short HRP): not applicable (all edges side-specialised, weights >= 0) -- recorded, not tested.

**D057 threshold-selection PBO (AFML s.11.6) and DSR record (s.14.7.3, MLAM s.8.7).** Per edge: trials = every threshold 04b
evaluated (v2_threshold_grid_summary_all.csv, including the ones its grid gates rejected; their trades are rebuilt from the
lowest threshold's trade set filtered by calibrated_probability >= t -- verified 2026-10-29: the sets nest exactly and
executed_r is identical; afml_bet_size of a rebuilt threshold is taken from the lowest threshold, max observed difference
0.011). Matrix = daily-prorated log(1 + 0.01 afml_bet_size exec_r), calendar days, zero-filled; statistic =
compound_velocity_stat (04c's drawdown-adjusted compound velocity at a fixed fraction); CSCV S = 16. Pass mark (project
convention, as Gate C): PBO < 0.5 AND Prob[OOS <= 0] < 0.5. Sensitivities: unsized log(1 + 0.01 exec_r); exit-day booking;
gate-passing thresholds only. Selected = the deployed threshold (live_manifest final_live_threshold; checked against 04c's
argmax). DSR record: Gate C trials (the 406 of ch14_dsr_all_trials.json, same stamps, gate_c_pbo units) plus the threshold
trials (unsized, same units), ONC E[K] on the combined correlation, Lopez de Prado & Lewis cluster weights, selected = the
deployed threshold's series; DSR > 0.95 and FWER < 0.05 reported (D054 marks); threshold-only DSR alongside.

### 4P.2 Results of T1-T4 and D057 (2026-10-29; read against s.4P.1 as written)

Outputs: artifacts/ch16/ch16_allocation_tests.json (7,494 s; renamed ch16_allocation_tests_20261029_v1rule.json on 2026-10-30), artifacts/ch16/d057_threshold_pbo.json (6,182 s); logs
logs/ch16_allocation_tests_20261029.log, logs/d057_threshold_pbo_20261029.log; unit test tests/test_afml_ch16_hrp.py (8 pass).

**Environment finding.** 07 does not run on venv312 (pandas 3.0.2): clean_corr's `np.fill_diagonal(corr.values, ...)` raises
"underlying array is read-only" under copy-on-write. Every Ch.16 tool and the allocator must run on venv39 (pandas 2.3.3).

**Book reproduction.** Table 16.1 (p.233) CLA / HRP / IVP reproduced to <= 0.004 pp, condition number 150.9324 (p.231),
sigma_HRP 0.4640 and sigma_CLA 0.4486 (p.232), Figure 16.5 clusters. On the same data, linkage on d (07's way) instead of
d~ (Snippet 16.4) moves HRP weights by up to 7.83 pp: 07's clustering input is not the book's.

**S0 passed.** Exit-day rebuild vs 06: max |diff| 0.0; 07 single linkage on 06's 6 columns vs live manifest: 3.5e-17.

**T1.** On our 4 edges d vs d~ gives identical weights and order (the factorial cells differ only by denoise). Denoise +
detone on vs off: max |dw| 0.0232 (XAUUSD 0.488 vs 0.511, GBPJPY 0.213 vs 0.196, HK 0.212 vs 0.209, JPN 0.087 vs 0.085);
quasi-diagonal order XAU, JPN, GBPJPY, HK (on) vs HK, XAU, GBPJPY, JPN (off).

**T2 (exercise 16.1).** The correlation matrix is near the identity in every alignment: condition number 1.24 (A), 1.32 (B),
1.43 (C), 1.62 (D); eigenvalues 0.78-1.26. Zero-day share A 0.55-0.65, B 0.32-0.55, weekly 0.04-0.15. Bets per week 10.4
(XAU) - 20.5 (JPN); mean holding 0.60 (XAU) - 1.30 (HK) days. NCO clusters {GBPJPY, JPN}, {HK, XAU} in all four.
HRP-current XAUUSD weight: A 0.488, B 0.370, C 0.479, D 0.457 (other methods move the same way). The weekly alignments,
free of day-booking choices since holding << 1 week, agree with A; B is the outlier (prorating a trade's R evenly over its
calendar days smooths multi-day edges' daily variance, so they look less risky than XAU's intraday trades).

**T3 M1 (10,000 iterations, nObs 520, sLength 260, rebal 22).** Variance ratio vs HRP-current - 1, 95 % CI:

| method | A G1 | A G2 | B G1 | B G2 |
|---|---|---|---|---|
| IVP | [-0.0003, 0.0034] | [-0.0008, 0.0023] | [-0.0114, -0.0065] | [0.0003, 0.0045] |
| HRP-book | [0.0030, 0.0096] | [0.0008, 0.0058] | [-0.0113, -0.0035] | [0.0033, 0.0096] |
| CLA min-var (long-only) = Markowitz min-var | [0.0029, 0.0107] | [0.0010, 0.0071] | [0.0001, 0.0081] | [0.0001, 0.0060] |
| NCO min-var | [0.0074, 0.0150] | [0.0012, 0.0072] | [-0.0070, 0.0007] | [-0.0003, 0.0059] |
| EW | [0.48, 0.57] | [0.40, 0.46] | [0.43, 0.50] | [0.29, 0.34] |

Max-Sharpe (exercise 16.5): Markowitz +639 to +79,412 and NCO +255 to +6,049 times HRP's variance (unconstrained weights
on noisy mu: large shorts / leverage). Unconstrained min-var equals long-only CLA here (all weights positive).
**Decision rule: no method beats HRP-current** (none is lower under both generators with the CI excluding 0 on A or B).
All risk-based methods are within ~1 % of each other in OOS variance: with a near-identity correlation matrix every
risk-based method collapses towards IVP, as s.16.4.3 implies (HRP's bisection is IVP when there is no structure).

**T3 M2 (1,000 sims).** Own-RMSE at nObs 260, A: IVP 0.0184, HRP-current 0.0203, HRP-book 0.0228, NCO 0.0242, CLA 0.0256,
max-Sharpe 0.95-1.77; at T: IVP 0.0076, HRP-current 0.0088, HRP-book 0.0093, CLA 0.0104, NCO 0.0115. B ranks the same.
HRP-current (denoised) is more stable than HRP-book; IVP is the most stable; RMSE vs the true min-variance target at T is
lowest for CLA (0.0104) as expected from an unbiased estimator of its own target.

**T4 (exercise 16.3, 1,000 draws).** Most affected first: Markowitz max-Sharpe, Markowitz min-var, CLA, NCO, HRP, IVP, EW
(mean RMSE 0.161 -> 0.125 on A). Matches the chapter's claim: the inverse-based methods react most to spectral changes.

**D057 (threshold selection, main = sized prorated, S = 16).** Live threshold = 04c argmax in all 4 (no wiring error).

| edge | thresholds | PBO | Prob[OOS<=0] | pass | unsized PBO | DSR all trials | DSR thresholds only |
|---|---|---|---|---|---|---|---|
| XAUUSD rs_long | 9 | 0.230 | 0.005 | yes | 0.237 | 1.000 (415) | 1.000 |
| GBPJPY drc_long | 14 | 0.510 | 0.036 | no | 0.268 | 0.866 (420) | 0.992 |
| HK dhc_short | 19 | 0.567 | 0.106 | no | 0.346 | 0.263 (425) | 0.997 |
| JPN rs_long | 14 | 0.815 | 0.434 | no | 0.799 | 0.951 (420) | 0.978 |

JPN fails in every variant (0.80-0.82). GBPJPY and HK pass unsized: bet sizing interacting with the threshold drives the
failure. The threshold step alone costs little DSR; the Gate C geometry trials (D050-A) dominate. HK's combined DSR fails
because the 406 Gate C trials' SR variance (11.7 annualised) puts SR* at 1.78 > 1.50. Combined and Ch.14 level-1 DSRs are
not comparable (different selected series: deployed meta-filtered threshold vs Gate C winner).

**Other finding.** 07b (line 68) and 07c (line 87) default to the *average*-linkage portfolio returns file
(hrp_portfolio_returns_raw_r_core_plus_watchlist_compound_velocity_average.csv) while live trades single linkage: the
DSR/PSR validation is run on a portfolio that is not the deployed one.

**Proposals (owner decision; nothing changed):** (1) 07 linkage on d~ per Snippet 16.4 (no weight change on current data,
verified); (2) keep denoise + detone (more stable in M2, OOS variance equal) and register it as a deviation from Snippet
16.4 under D010; (3) keep exit-day alignment A (weekly agrees; B is a smoothing artefact); (4) keep HRP, reject max-Sharpe
methods; (5) 07b/07c default to the single-linkage file; (6) D057 threshold PBO + threshold trials in the D054 DSR become
RECERT gates; JPN's threshold selection fails now; (7) allocator pinned to venv39 / pandas < 3 with a version guard.

### 4P.3 Owner decisions, corrections, and the exit-day vs mark-to-market comparison (2026-10-30)

**Decisions (owner, 2026-10-30).** (1) 07 links on d~ (D058, done). (2) Denoising: follow the book, with a >= 20-edge book
in mind (1 long + 1 short per asset) -- rule (a')/(b') below (D010, done). (3) Alignment: "use the book" -- frequency rule
automatic; build daily mark-to-market (MTM) and compare before switching (D059, decision pending). (4) Keep HRP, reject
max-Sharpe for now; T1-T4 re-run as a diagnostic at every universe change (D060). (5) 07b / 07c on the single-linkage file
(D058, done). (6) D057 is a diagnostic, not a gate; the D054 DSR gate keeps its Gate C-only trial count. (7) No version
pin: 07 runs on venv39 (noted in RECERT).

**Corrections to s.4P.2 (found by testing before implementation; disclosed to the owner).** (i) A Marcenko-Pastur fit
whose variance sits at the bound (0.99999) is not a failed fit: var = 1 is the pure-noise value and the book's own fallback
(Snippet 2.4 p.27 `else: var=1`); the book still denoises. The s.4P table line "not meaningful" is withdrawn. (ii) A
single-sample "all first-eigenvector loadings one sign" test calls two equal-strength clusters a market (the top two
eigenvectors mix); a row bootstrap separates them (share of resamples all-one-sign: market 1.00, two clusters 0.69, live 6
edges 0.011). (iii) The 4 current edges do get denoised (fitted var 0.961, lambda+ 1.062, lambda1 1.128 -> nFacts 1).
(iv) "Bets per week 10.4-20.5" in s.4P.2 T2 were trades per week; bets (AFML p.196, D056) are 529 / 400 / 366 / 459.

**07 rule implemented (mlam_ch2_book_v2, owner approved (a')/(b')).** (a') Denoise exactly as Snippets 2.4-2.5: fitted
variance (fallback 1), nFacts = eigenvalues above lambda+ with no floor (Snippet 2.4 searchsorted formula), constant residual
eigenvalue; nFacts = 0 gives the identity, so HRP = IVP. (b') Detone the clustering matrix only if lambda1 > lambda+ and the
first eigenvector is all one sign in >= 95% of 1,000 iid row-bootstrap resamples (seed 0) -- project operationalisation of
p.30's "hypothesis that can be tested statistically". Live 6 columns: nFacts 2, share 0.011, no detone; weights GBPJPY
0.098, HK 0.111, JPN rs 0.033, JPN vc 0.194, XAU rs 0.195, XAU vc 0.369. Current 4: XAU 0.509, GBPJPY 0.197, HK 0.208, JPN
0.086. Tests: tests/test_afml_ch16_hrp.py (13; market -> detone, two clusters -> no detone, noise -> identity / IVP,
vectorised bootstrap = explicit resampling, linkage(pdist(D)) = Snippet 16.4). Lambda+ is asymptotic: pure-noise samples
at T = 2000, N = 10 show one eigenvalue above it in 1 of 10 seeds. Takes effect at the RECERT allocator re-run.

**Frequency rule (exercise 16.1(a)).** Average over edges of the s.14.3 bet frequency = 110.6 bets/year -> one bet every
3.30 days -> observation period = the largest of daily / weekly / monthly not longer than that = **daily**.

**MTM build.** E = open trades marked at the last bar close of each UTC day (research mid bars), first mark = entry price,
exit day = move to the exit price plus the cost residual (executed_r - gross price R); each trade sums to its executed_r
(max total difference 5.7e-14 R). F = E summed by week. Daily correlation A vs E per edge 0.84-0.90; zero-day share A
0.55-0.65 -> E 0.42-0.59.

**T1-T4 re-run with the new 07** (artifacts/ch16/ch16_allocation_tests_A.json and _E.json, 10,000 M1 iterations each; the
2026-10-29 run kept as ch16_allocation_tests_20261029_v1rule.json; B was dropped from T3 -- judged a smoothing artefact).
S0 passed in both (tool = 07 exactly; exit-day rebuild = 06). T1: book vs current max |dw| 0.0019.

| alignment | cond. no. | HRP-current GBPJPY / JPN / HK / XAU |
|---|---|---|
| A exit-day daily | 1.24 | 0.197 / 0.086 / 0.208 / 0.509 |
| E MTM daily | 1.28 | 0.218 / 0.079 / 0.204 / 0.499 |
| C weekly (exit-day) | 1.43 | 0.196 / 0.079 / 0.225 / 0.499 |
| F weekly MTM | 1.60 | 0.193 / 0.079 / 0.231 / 0.497 |

T3 M1, variance ratio vs HRP-current - 1 (95 % CI): IVP A [-0.0015, 0.0011] / [-0.0012, 0.0004], E [-0.0005, 0.0025] /
[-0.0015, 0.0005] (G1 / G2); CLA, NCO, HRP-book and EW higher or not lower; max-Sharpe +261x to +7.9e6x. **No method beats
HRP-current under either alignment.** OOS daily variance is higher under E (HRP 5.13 vs 4.79 G1): MTM records open-trade
swings that exit-day booking hides; levels are not comparable across alignments as a preference. M2 own-RMSE at T: IVP
0.0076, HRP-current 0.0085 (A) / 0.0083 (E), HRP-book 0.0093 / 0.0088. T4 most affected first: Markowitz, CLA, NCO, then
HRP / IVP (both alignments).

**Owner decision (D059, 2026-10-30): "Approved, switch 06 to mark-to-market".** Implemented in
`portfolio_allocator/06_build_representative_return_streams.py`:
- `DATE_AGGREGATION = "mark_to_market_daily"`. `mark_to_market_bookings` ports the tool's `stream_mtm` and books
  one row per trade per UTC day open. It fails closed on bad trade rows, and again if any trade's daily R does not
  sum to executed_r (tolerance 1e-9).
- `afml_weighted_r` is the daily R × afml_bet_size.
- `bet_frequency_alignment` applies the exercise 16.1(a) rule to the profile edges on every run, and stops 06 if
  it selects a period other than daily (only daily streams are built); the result goes in the manifest.
- `"exit_date"` is kept only to reproduce pre-D059 outputs.

Verification (venv39, outputs redirected to a scratch folder, so the live outputs are untouched):
- `exit_date` reproduces all 12 current daily matrices bit-for-bit.
- The MTM profile matrix equals the tool's E stream to 1.4e-14.
- Trade sums match executed_r to 6.7e-16 (14,662 trades → 30,139 booking rows).
- The index starts one day earlier (2022-05-11, the first entry day).
- Rule on the 6 profile edges: 107.2 bets/year, interval 3.41 days → daily.
- 07 on the MTM stream, 4 edges: GBPJPY 0.218, JPN 0.079, HK 0.204, XAU 0.499 (= T2 E).

The tool's S0 now rebuilds with the booking named in 06's manifest; it passes under both. Test:
`test_06_mark_to_market_booking_by_hand`. The outputs change at the RECERT allocator re-run (06 → 07 → 07b/07c).
08 does not read 06's streams.

## 4Q. AFML Ch.17 — structural breaks (audit 2026-10-30, CLOSED 2026-10-30)

Book pages: AFML Ch.17 = pdf 276-289 = pp.249-262 (sections 17.1-17.4.3, Snippets 17.1-17.4, Table 17.1, Figures
17.1-17.3, exercises 17.1-17.5; read line by line, every formula checked on the rendered page). Purpose (s.17.1 p.249):
measure the likelihood of structural breaks "so that informative features can be built upon them" -- a FEATURE
chapter; no method is mandated. Code read in full: shared/features/afml_ch17_structural_breaks.py, tests/
test_afml_ch17_sadf.py, the SADF block of all 6 feature_expansion.py, shared/edges/native_one_row_feature_engine.py
(SADF_* constants, _compute_one SADF block), portfolio_live/phase_c_02_certify_incremental_features.py (SADF parity).
Evidence: logs/ch17_audit_evidence_20261030.txt (tool shared/research/ch17_sadf_lag_audit.py).

| § (page) | Book | Quantara | Status |
|---|---|---|---|
| 17.2 (pp.249-250) | two families: CUSUM tests; explosiveness tests (right-tail unit-root; sub/super-martingale) | SADF only | context |
| 17.3.1 (p.250) | Brown-Durbin-Evans CUSUM on recursive residuals; S_t ~ N[0, t-k-1] under H0; arbitrary start point is a caveat | none | not used (optional feature) |
| 17.3.2 (p.251) | Chu-Stinchcombe-White CUSUM on log-price levels, S_n,t; c_alpha = sqrt(b_alpha + log[t-n]), b_0.05 = 4.6; S_t = sup over n | none (the Ch.2 CUSUM *filter* is an event sampler, not this test) | not used (optional feature) |
| 17.4.1 (pp.251-252) | Chow-type DF: DFC_tau*, SDFC = sup over tau* in [tau0, 1-tau0]; assumes one break, bubble to the end of the sample | none | not used (book: superseded by SADF for 3+ regimes) |
| 17.4.2 (pp.252-253) | SADF_t = sup over t0 in [1, t-tau] of ADF_t0,t, computed at EACH t in [tau, T]; regression Delta y_t = alpha + beta y_t-1 + sum gamma_l Delta y_t-l | SADF_ct_50 in all 6 feature_expansion.py; model input in HK dhc, JPN rs, XAU rs, XAU vc | **see findings 1-2** |
| 17.4.2.1 (pp.253-254) | use log prices | log(close + 1e-12) in research and live | done |
| 17.4.2.2 (pp.254-255), Table 17.1 | O(n^2) ADF tests; FLOP counts; HPC / Ch.20 for full SADF | bounded window instead | see finding 2 |
| 17.4.2.3 (p.255) | steady (beta < 0, half-life), unit root (beta = 0), explosive (beta > 0) | -- | context (ex. 17.3) |
| 17.4.2.4 (pp.255-256) | QADF: q-quantile of s_t (e.g. q 0.95) and dispersion Q_t,q+v - Q_t,q-v (e.g. v 0.025); robust to SADF's sensitivity to the extreme value | none | not used (book's robustness fix to SADF) |
| 17.4.2.5 (p.256), Figs 17.2-17.3 | CADF: conditional mean / std of s_t above Q_t,q (e.g. q 0.95); outliers bias SADF upwards | none | not used (book's robustness fix to SADF) |
| 17.4.2.6 (pp.258-259), Snippets 17.1-17.4 | get_bsadf / getYX / lagDF / getBetas | ported; verified vs the verbatim snippets on 200 random cases: identical designs, sup equal in 199; the 1 difference is a 0-degree-of-freedom final regression the book divides by zero on (cannot occur with our ct / lags 1 / minSL 10: 9 rows x 4 columns) | done |
| 17.4.3 (pp.259-261) | SMT_t on 4 trend specs (Poly1, Poly2, Exp, Power), |beta| / sigma, penalised by (t - t0)^phi; "features ... may include SMT_t estimated from a wide range of phi values" | none | not used (optional feature) |

**Finding 1 -- SADF_ct_50 research/live skew (defect).** Research and live compute SADF on a 50-bar window every 10th
bar and forward-fill (stride 10). The stride grid is counted from the start of whatever array is passed in:
- research (full-history export) therefore uses the window ending 0-9 bars before the row, uniformly (sample of 600
  rows per edge: each lag 0-9 about 10 %);
- live (fixed 6,000-bar window) always uses the window ending 9 bars before (SADF_LAG = (6000-1-50) % 10 = 9), and
  Phase C.2 certifies live against a 6,000-bar batch, so the gap to research was never tested.

Result on the model rows: research equals live on 10.0-10.5 % of rows; correlation 0.43-0.46; mean |gap| 0.50-0.55
against a feature std of 0.69-0.75 (all 4 edges). The models learned a feature live never supplies. Impact today is
small -- SADF_ct_50 carries no importance (MDA within one std of 0 on HK / XAU rs / XAU vc; MDI 0.0063 = 1/157, an even
share) -- but it is wrong. The book computes SADF_t at each t (p.253); stride was added only for speed, and the audit
measured about 4 ms per SADF_t at lookback 50 (about 5 minutes per 70,000-bar asset at stride 1).

**Finding 2 -- bounded window not registered.** The book's SADF searches t0 over [1, t - tau] (the whole sample);
ours searches the last 50 bars only (40 start points, tau = minSL = 10; Snippet 17.1's final regression has
minSL - lags = 9 rows). The module docstring calls this "not a deviation in the formula itself" -- inaccurate: the
supremum is over a different set, so it is a different (rolling-window) statistic. The window is needed (book
pp.254-255 cost; live parity, since live holds 6,000 bars), but it must be a registered deviation.

**Book errata (as printed; no action).**
(i) p.252: H1 : delta > 1 should be delta > 0 (delta = rho - 1, explosive rho > 1).
(ii) p.255: explosive case -infinity "if log[y0] < alpha/beta" -- the derivation on the same page gives -alpha/beta.
(iii) p.258: 'nc' described as "no time trend, only a constant", but Snippet 17.2 adds the constant only when
constant != 'nc' ('nc' = no constant).
(iv) p.254: the full-SADF FLOP total equals sum over t of g(N, t, tau) (reproduced exactly with tau = 1,000), not
g(N, T, tau) as printed; f(3, 356631) = 11,412,245 and g = 2,034,979,648,799 reproduce exactly.
(v) Snippet 17.1 returns key 'gsadf' for the bsadf value and relies on Python 2's None comparison.
(vi) p.250: sigma_omega^2 sums from t = k while omega_t is defined from k+1.
(vii) p.255: QADF's s_t uses t0 in [0, t1 - tau] ("t1" for t).

**Proposed decisions (owner).**
1. Fix finding 1 at the retrain: SADF_t computed at every bar on the window ending at the current bar (stride 1,
   lag 0), the same in feature_expansion.py (all 6) and the live engine; Phase C.2 certifies live against the research
   rule. This changes every edge's features, so it rides the RECERT retrain (re-export, Steps 01-09, Gate D).
2. Register finding 2 as a deviation (bounded window, 50 bars, tau 10, 'ct', 1 lag: kept as is, not tuned) and
   correct the docstring.
3. Optional features (owner choice; uniform schema, judged by Step 01 clustered MDA at the retrain): QADF (q 0.95,
   v 0.025) and CADF (q 0.95), the book's own robustness fixes to SADF, computed from the same 40 ADF values (no extra
   regressions). Further optional: CSW CUSUM sup statistic, SMT over a phi grid, BDE CUSUM.

### 4Q.1 Owner decisions and implementation (2026-10-30)

Decisions 1, 2 and 3 (QADF and CADF) approved as proposed. Register D061, D062, D063; backlog D061, D063.
- `shared/features/afml_ch17_structural_breaks.py`: `get_adf_stats` (s_t, Snippet 17.1's allADF), `get_bsadf`
  (sup s_t), `qadf`, `cadf`, `structural_break_row`, `structural_break_features` (every bar, window ending at t
  inclusive). `sadf_series` and its stride were removed so the skew cannot return. The docstring now states D062.
- All 6 `feature_expansion.py`: SADF_ct_50 plus QADF_ct_50, QADF_Dispersion_ct_50, CADF_ct_50,
  CADF_Dispersion_ct_50 (q 0.95, v 0.025, the book's examples), from one `structural_break_features` call.
- All 6 `contracts/feature_schema.json`: SADF description corrected; 4 entries added; 172 entries, uniform across
  edges apart from each edge's own `computed_by` path.
- `shared/edges/native_one_row_feature_engine.py`: the 49 previous closes plus the current close, the same
  `structural_break_row`; ENGINE_VERSION v1_5 (forces fresh live state).
- `portfolio_live/phase_c_02_certify_incremental_features.py`: SADF is no longer "horizon-sensitive", so it is
  compared with the research batch at every step. A research-rule oracle (`structural_break_features` on the same
  rolling window) checks all 5 structural-break columns the contract lists.

Verification:
- Phase C.2 on the current contract (SADF_ct_50 among the 168 core features): GBPJPY, HK, JPN, XAUUSD PASS, 128
  steps, 0 failures. The 4 new columns enter the contract when it is regenerated at the RECERT; they come from the
  same call on the same window as SADF.
- 73 tests pass, including the verbatim-snippet comparison.

Found on the way (not Ch.17; RECERT blocker in the backlog, owner decision): the FFD state bundles written on
2026-10-23 under venv312 (pandas 3) do not unpickle on venv39 (pandas 2.3.3). Phase C.2 therefore had to run on
venv312 for this check.

### 4Q.2 Line-by-line coverage of the chapter (what was applied, what was not, and why)

| Item | Treatment |
|---|---|
| 17.1 motivation, 17.2 classification, 17.4 intro (single vs multiple bubbles), 17.3 intro (Ch.2 CUSUM filter) | read; context, no code. The Ch.2 CUSUM filter (event sampling, `candidate_continuations/providers/cusum.py`) belongs to Ch.2 |
| 17.3.2 CSW, 17.4.3 SMT | applied (D064, s.4Q.3) |
| 17.3.1 BDE | read, every formula checked; not implemented (owner decision, reasons s.4Q.3) |
| 17.4.1 Chow-type DFC / SDFC | read; not implemented: the book itself says it assumes one break with the bubble running to the end of the sample and moves to SADF for 3+ regimes (p.252); as a rolling feature it would be a weaker duplicate of SADF |
| 17.4.2 SADF; 17.4.2.1 log prices; 17.4.2.6 Snippets 17.1-17.4 | applied (D061, D062); port verified against the verbatim snippets |
| 17.4.2.2 complexity, Table 17.1 | every number reproduced (s.4Q erratum iv) |
| 17.4.2.3 conditions for exponential behaviour | read; derivation checked (erratum ii); a characterisation of beta's sign, not a feature the book proposes |
| 17.4.2.4 QADF, 17.4.2.5 CADF | applied (D063) |
| Figures 17.1-17.3 | not reproduced: E-mini S&P 500 dollar bars (2003-2017) we do not hold. Their claims are tested on our code instead: SADF spikes in an injected bubble (Fig 17.1); C_t,q <= SADF_t on every bar (Fig 17.2) |
| Exercises 17.1-17.4 | not run: all on E-mini S&P 500 dollar bars and their dot-com / 2007-08 episodes; we hold neither the instrument nor those years (our histories start in 2022) |
| Exercise 17.5 | answered and verified: (a) not needed for ADF-type tests on LOG prices -- log(1/y) = -log y leaves every ADF t-value unchanged (verified: max difference 0), so SADF / QADF / CADF / SDFC see bubbles and bursts alike (needed only for raw-price tests); (b) without it: those, SMT (absolute value of beta) and BDE (two-sided CUSUM); CSW as printed is one-sided (sup) and does need it |
| References | read; no action |

### 4Q.3 CSW, SMT, BDE (owner decision 2026-10-30: "Add CSW and SMT, skip BDE")

All three can be computed on our data: they need only a price series (BDE also needs regressors x_t). The
data limit the owner remembers is Ch.19's (Kyle's / Amihud's / Hasbrouck's lambda and VPIN need trade prints and
signed volume; we hold Bid/Ask quote ticks). Assessment:
- **BDE** (17.3.1): not recommended. It needs "an array of features x_t predictive of y_t" that the book does not
  give for a price series (we would invent the specification); the book's own caveat is that results depend on the
  arbitrary start point; with x_t dropped it becomes CSW (p.251).
- **CSW** (17.3.2): computable and cheap; needs a bounded window like D062; one-sided, so bursts need the sign flip
  (exercise 17.5) -- two features, sup on log y and on -log y.
- **SMT** (17.4.3): computable at SADF's cost; phi re-weights the same regressions (phi grid is free); the book
  suggests "a wide range of phi values" (exercises: 1 and 0.5). Choices the book leaves open: the time origin of the
  Power trend (log t), and Poly1 on raw prices (the book's own spec despite s.17.4.2.1). Example: 4 specs x phi
  {0, 0.5, 1} = 12 features.
- Cost of adding: more correlated candidate features for Step 01 (more chances of a spurious selection) and longer
  exports; SADF itself shows no importance today.

Implemented (D064), fixed before any data was looked at:
- **CSW**, on the 50 log closes ending at the bar:
  - sigma_t^2 = the mean squared log change;
  - n < t (the book's n = t is 0 / 0);
  - CSW_Up_50 = sup over n; CSW_Down_50 = the same on -log y = log(1/y) (exercise 17.5).
- **SMT**:
  - start points t0 in [1, t - 10], so samples of 11-50 closes;
  - time counted from the sample start; the book fixes no origin, and it matters only for Power's log t;
  - Exp and Power fitted in their printed linearised log form; Poly1 on raw Close as printed;
  - OLS standard error as Snippet 17.4;
  - phi in {0, 0.5, 1}: 0 is the unpenalised SMT_t, 0.5 and 1 are the exercise values;
  - 12 columns SMT_<spec>_<phi>_50.

Same code path as SADF in research, the live engine and the Phase C.2 oracle. Verification:
- tests: CSW by hand; SMT equal to an independent least-squares fit for every spec and phi;
- cost: 6.7 ms per bar for all 19 Ch.17 columns;
- Phase C.2 on venv39 with all 19 columns added to the compared set (driver: the current contract lists only
  SADF): GBPJPY / HK / JPN / XAUUSD PASS, 128 steps, 0 failures, worst gap 1.6e-10.
- Live update time: p50 318-333 ms, 4 assets in parallel at low priority. The 2026-10-17 venv39 certification
  showed 218 ms, one asset at a time and before SMT. Far inside the ~5.7 s bar-to-fill latency.

### 4Q.4 Fix of the pandas-3 pickles (owner: "fix the FFD files now"; D065)

**Cause.** A pandas object pickled under pandas 3 (venv312) does not unpickle under pandas 2.3.3 (venv39) -- even
with object column names and ns datetimes (tested). Research runs on venv312 rewrite the FFD / HMM bundles and, through
the verifiers, the live continuation states. Before the fix, 31 of 72 artifacts failed on venv39: 10 FFD state
bundles, 10 HMM live bundles, 11 live states. That blocked Phase C (venv39) and would have blocked live.

**Fix.** `shared/io/portable_pickle.py` stores DataFrames, Series, Indexes and Timestamps inside these payloads as
numpy arrays plus metadata:
- datetime unit and time zone kept;
- object columns never re-inferred;
- text columns restored as each pandas' native text dtype;
- old payloads load unchanged.

Every writer and reader of the three artifact types uses it (7 files). The 31 files were rewritten (originals
archived at `../archive_pandas3_pickles_20261030`), and the 20 bundle hashes and sizes updated in their manifests.
`docs/tools/scan_pickles_loadable.py` is now a RECERT step after the retrain.

**Verification.**
- Value + dtype fingerprints are identical to the originals for all 40 files, in every direction: written on venv312
  and read on venv39 / venv312; written on venv39 and read on venv312 (HMM model and scaler parameters included).
- Scan: 76 files, 0 failures on venv39. All 20 manifest hashes match.
- Phase C.2 PASS on venv39 for GBPJPY / HK / JPN / XAUUSD.
- tests/test_portable_pickle.py (4) and the FFD tests pass on both interpreters; 99 tests pass on venv39.

## 4R. AFML Ch.18 — entropy features (audit 2026-10-30; approved and implemented 2026-10-31)

Book pages: AFML Ch.18 = pdf 290-306 = pp.263-279 (s.18.1-18.8.4, Snippets 18.1-18.4, Figures 18.1-18.2, exercises
18.1-18.5). Read line by line from the text and, for every equation, from the page images. The source the book follows,
Gao, Kontoyiannis and Bienenstock (2008, arXiv 0802.4363v1), was read for the match-length definition and the n / k rule.
Evidence: logs/ch18_audit_evidence_20261030.txt; tools: shared/research/ch18_entropy_audit/ (Snippets 18.1-18.4 verbatim).

**What Quantara has.** One Ch.18 feature, `Entropy_Kontoyiannis_100` (shared/features/afml_ch18_entropy.py), in all 6
edges; a selected model input in HK dhc, JPN rs and XAU rs. At bar t: the signs of `Close_FFD.diff()` over bars t-100..t-1
(binary encoding, zeros dropped), Snippet 18.4 with a fixed match window n = len(msg)//4 (about 25). Research and live call
the same functions on the same window, so there is no research/live skew (stored = recompute to 2e-16). Step 01 MDA:
GBPJPY +0.000054 +- 0.000133, HK -0.000423 +- 0.000227, XAU rs -0.000094 +- 0.000235 (no value). `Regime_Entropy` (Shannon
entropy of the HMM state posterior, p.263 definition) is correct.

**Findings.**
1. Bug: `match_length` returns the matched length, not matched length + 1 (Snippet 18.3, p.267; Gao p.6: L = 1 + the longest
   match length). It followed the formula printed on p.266, which is off by one (x_i^{i+l} should be x_i^{i+l-1}). It differs
   from the book in 295 of 300 random cases; on the book's own example (p.269) the book code gives 0.97 and 0.84 as printed,
   the port 1.34 and 1.34. On our data the fix moves the feature mean from 1.05 to 0.85, rank correlation 0.986-0.990.
2. `pmf1` counts all len-w+1 words; Snippet 18.1 counts len-w (drops the last word). Not used by any feature.
3. Window: Snippet 18.4's default is the expanding window; Gao s.3.2 finds increasing-window estimators "generally
   performed better, and always at least as well". The fixed n = len/4 is not from the book. Expanding vs current: rank
   correlation 0.25-0.35 (a different feature).
4. Encoding: the book uses binary encoding for price bars, where |r| is about constant (p.270); our bars are tick bars
   (6-14 min) with |r| p90/p10 = 15-16, where the book says binary discards information and quantile (boundaries from
   the training set) or sigma encoding is needed; Figure 18.1 shows small alphabets underestimate. p.270 also advises
   encoding the fractionally differentiated series because it keeps memory; `Close_FFD.diff()` has order d*+1 (about 1.35),
   which removes that memory. Encoding the FFD level literally (10 quantiles) is degenerate at 100 bars: 29 % (JPN) and
   57 % (XAU) of windows use one letter. Return encodings (binary, quantile-10, sigma) are well spread and nearly
   uncorrelated with the current feature (0.00-0.41).
5. Lag: the window stops at t-1; Ch.17 set lag 0 (D061). Lag 0 vs 1: rank correlation 0.987.

**Book errata / checks.** p.266 L formula off by one (code and Gao correct); Snippet 18.4 redundancy divides by
log2(len(msg)), the p.264 definition by log2 of the alphabet size; Doeblin-modified formula has log2[n], the code log2(n+1);
p.268 example (N = 2^8, n = 198, k = 58) cannot run with Snippet 18.4 (centred window needs 2n + k - 1 <= N; Gao simulates
N = 2n + k); Figure 18.1 does not reproduce from Snippet 18.4 (10/7/5/2 letters: book 1.43/1.21/0.99/0.46, code
2.29/2.03/1.75/0.85) and compares bits of a quantised source with the Gaussian's 1.42 nats (10 equal letters = 3.32 bits);
p.274 N_q[p] = k holds for every q != 1, not only q > 1; exercise 18.5(b)-(c) weights sum to 9, not 1; Easley et al. 2012a
pages printed 1547-1493 (paper: 1457-1493); s.18.8.3 refers to "Chapter 18, Section 18.7" from inside Chapter 18.
Checked correct: MI for normals, Gaussian entropy and the entropy-implied sigma, generalised-mean special cases, N_q
limits, Meucci concentration (exercise 18.5a gives 0), VPIN algebra.

**Not applied, with reasons.** s.18.6 entropy-implied volatility (needs Gaussian returns and a differential entropy; the
estimator returns bits of a quantised message, see Fig 18.1). s.18.8.2 maximum entropy generation (no procedure given).
s.18.8.4 entropy of order-flow imbalance (needs traded volume classified buy/sell; we hold quote ticks; same reason as
PIN/VPIN in shared/features/afml_ch19_microstructure.py). MI / variation of information: MLAM Ch.3, reviewed with AFML
Ch.8. Exercises 18.1-18.2 need E-mini dollar bars (not held); 18.3-18.5 run (18.3: 8 quantiles, plug-in w=1 3.00 bits,
Kontoyiannis 2.10 / 2.33 at windows 10 / 100 vs true 3 bits).

**Proposals put to the owner (2026-10-30).** (a) fix match_length to Snippet 18.3; (b) keep pmf1 counting every word,
registered, or revert to verbatim; (c) expanding window; (d) encodings: replace the FFD-diff binary feature with
return encodings (binary, quantile-10, sigma), quantile boundaries and sigma frozen per asset on the research history
(FFD d policy); (e) lag 0; (f) optional: s.18.8.3 effective number of bets as an allocator report line. All at the RECERT.

### 4R.1 Owner decision (2026-10-31): "Approved, proceed with (a) through (f)"

| Item | Register | What was done |
|---|---|---|
| (a) match length | D066 | `match_length` returns 1 + the longest match (Snippet 18.3; Gao p.6). Implemented letter by letter instead of the snippet's every-length loop; identical pairs on > 10,000 cases (binary, 10- and 16-letter alphabets) in tests/test_afml_ch18_entropy.py; the p.269 example gives 0.968 / 0.843 (book prints 0.96 / 0.84, truncated) and 10000111 = 10000110. About 10x faster (0.8 ms per bar for all three features). |
| (b) pmf1 | D067 | Kept counting every word; registered deviation; not used by any feature. Test shows the only difference is the last word. |
| (c) window | D066 | Snippet 18.4 default (expanding window, `window=None`); odd-length messages lose the first letter (p.269). |
| (d) encodings | D068, D069 | `Entropy_Kontoyiannis_100` (sign of Close_FFD.diff()) REMOVED and replaced by `Entropy_Kontoyiannis_Binary_100`, `_Quantile10_100`, `_Sigma_100` on r_t = diff(log Close). Quantile boundaries (10 letters) and sigma (std of all research bar returns, ex. 18.1(c)) frozen per asset on the FFD manifest's source bars: `assets/{A}/bars/entropy/entropy_encoding_manifest.json` (all 10 assets; in-sample letter shares 0.100 each; zero-return share 0.0001-0.0088), built by shared/features/build_asset_entropy_encoding.py. Not encoding the FFD series (p.270 advice) = registered deviation D068. |
| (e) lag 0 | D066 | Window = the 100 returns ending at the current bar (101 closes), research and live. |
| (f) effective number of bets | D070 | Allocator Step 07 portfolio summary gains `pc_effective_number_of_bets` and `pc_meucci_concentration` (s.18.8.3, sample covariance of the streams, HRP weights); report only, in no score, warning or gate. Current pre-RECERT core streams (6 edges, single linkage): 3.39 bets, concentration 0.44 (1/sum w^2 = 4.26). tests/test_afml_ch18_portfolio_concentration.py (ex. 18.5(a): H = 0). |

**One rule, research and live.** `shared/features/afml_ch18_entropy.entropy_row` is called by every feature_expansion.py
(through `entropy_features`) and by native_one_row_feature_engine (`close_hist[-100:] + [c]`, ENGINE_VERSION v1_6;
`_compute_one` now takes `asset` to load the frozen encoding). portfolio_live/phase_c_02 compares the three columns with
the research batch every step and with an independent oracle (`entropy_research_rule_parity`). All 6 feature_schema.json
are uniform (188 entries). Values on the last 3,000 research bars: binary mean 0.85 (sd 0.03-0.04), quantile-10 2.02-2.27,
sigma 1.11-2.17; Spearman between the three -0.77 to 0.74 (Step 01 decides at the retrain).

**Consequence until the RECERT retrain.** The current models of HK dhc, JPN rs, XAU rs and XAU vc select the removed
`Entropy_Kontoyiannis_100`, so the Phase C.1 contract cannot be compiled for them (`selected features absent from
feature schema`) until they are retrained; live is stopped until the walkthrough ends, and the RECERT retrain + Phase
C.1/C.2 regeneration was already required (D061). The committed portfolio_live/config/phase_c/feature_contract.json is
untouched.

**Verification.** tests/test_afml_ch18_entropy.py 19 pass on venv39 and venv312 (verbatim Snippets 18.1/18.3/18.4 embedded; > 10,000 match-length cases). Phase C.2 on venv39 with a scratch C.1 contract compiled from the current schemas (only GBPJPY drc and JPN vc compile, see above): PASS, 188 core features incl. the 3 entropy columns, 128 steps, entropy_research_rule_parity 0 failures, values finite at every bar. Full venv39 suite: 554 pass, 74 skipped; 8 fail/error because the current live manifest no longer compiles into Phase C (C.1 / C.4 / C.5 / tick-curation tests -- the consequence above, cleared by the RECERT), 1 unrelated clock-dependent failure (test_live_bar_memory_authority: fixture day 2026-07-02 is older than ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS). Evidence appended to logs/ch18_audit_evidence_20261030.txt.

## 4S. AFML Ch.19 — microstructural features (audit 2026-10-31; approved and implemented 2026-10-31)

Book pages: AFML Ch.19 = pdf 308-327 = pp.281-300 (s.19.1-19.7, Snippets 19.1-19.2, Figures 19.1-19.3, exercises
19.1-19.12). Read line by line from the text; every equation page also from the page image. Source paper read for
Snippets 19.1-19.2: Corwin and Schultz (NBER 2009 conference version; eqs 4-16, s.2.1-2.3). Evidence:
logs/ch19_audit_evidence_20261031.txt; tools and per-asset results: shared/research/ch19_microstructure_audit/.

**What Quantara has.** shared/features/afml_ch19_microstructure.py: `Parkinson_Vol_20` (p.284, k1 form), `Corwin_Schultz_Spread_1`
(Snippet 19.1, sl = 1), `Becker_Parkinson_Vol_1` (Snippet 19.2, sl = 1), in all 6 feature_schema.json. Model inputs: HK dhc,
JPN rs, XAU rs (all three), JPN vc (Parkinson, Becker-Parkinson), XAU vc (Corwin-Schultz). Live: native_one_row_feature_engine,
same formulas, certified every step by Phase C.2. Bars are OHLC of the MID quote with a fixed tick count and no volume; raw
ticks carry bid, ask and Dukascopy's quoted sizes. We hold no trade prints, aggressor flags, traded volume, order-book
messages or options.

**Findings.**
1. Port = Snippets 19.1-19.2 as printed to 2.2e-16 on all 10 assets. One difference: rows the book drops (warm-up NaN) the
   port sets to 0 (`.where(x >= 0, 0)` turns NaN into 0; the book's `alpha[alpha<0]=0` keeps NaN): research bar 0 of every
   asset for both C-S and Becker-Parkinson (sl = 1). Live never sees it.
2. Corwin-Schultz on mid bars is not a spread. Its premise is "high prices are almost always matched against the offer, and
   low prices ... against the bid" (p.284); our High/Low are mid quotes. 37-41 % of bars are exactly 0 (alpha clipped); rank
   correlation with the spread actually quoted in the bar -0.03 to +0.11 (still -0.01 to +0.11 when rebuilt from ask-High /
   bid-Low); median 0.6x-6.5x the quoted spread; daily, 0.23-0.98 % vs quoted 0.005-0.07 %. It measures how much a 2-bar range
   falls short of the two 1-bar ranges. MDA within about 1-2 std of 0 in the 4 audited edges (all three features).
3. Snippet 19.2 vs the paper: the paper's eq.(12) uses the squared-range constant (k = 1/(4 log 2)) throughout; the snippet puts
   k2 = sqrt(8/pi) in its place, so with zero spread it returns 1.0435 sigma instead of sigma. A constant factor (no effect on
   tree splits). Becker-Parkinson_1 vs Parkinson_20: rank correlation +0.34..+0.46.
4. Parkinson (k1 form) on our bars = 0.93-1.06 x close-to-close std (bars, days, weeks; exercise 19.3); rank correlation with the
   rolling std of 20 returns +0.93..+0.96 (largely substitutes an existing volatility feature).
5. Everything else in the chapter needs data we do not hold (book's own requirements): tick rule and its transformations and
   Roll (trade prices; on mid quotes Roll c = 0.00-0.38 of the half quoted spread), Kyle / Amihud / Hasbrouck lambdas (signed
   traded volume; tick bars have a constant tick count), PIN / VPIN (buy/sell volume), s.19.6 order sizes, cancellations, TWAP,
   options, signed order flow; s.19.7 phi (a market maker's classifier on market-making labels). An observed-spread feature
   from our quotes would be Dukascopy's spread, not the live broker's (research/live difference; broker-spread decoupling).

**Book errata / checks.** p.287 Kyle alpha printed without its minus (alpha = -p0 sqrt(sigma_u^2/Sigma0) from the book's own
alpha = -mu/(2 lambda)); p.292 E[V^B - V^S] middle line swaps the good/bad-news terms (as printed it sums to
alpha mu (2 delta - 1), not alpha mu (1 - 2 delta)); Snippet 19.2 k2 vs the paper's sqrt(k1); Snippets 19.1-19.2 do not run
as printed (pd.stats.moments removed in pandas 0.23; typeset en-dashes); "Becker" vs Beckers [1983]; "Distibution" (p.293);
Carlin et al. [2007] in the text vs (2005) in the references; "Easley et al. [2012]" without a / b. Checked correct: Roll,
Parkinson constants, C-S alpha and S, Kyle x*, beta, lambda, E[pi], PIN bid/ask/spread and mixture, VPIN.

**Proposals for the owner.** (a) keep NaN on warm-up rows as the book does (`x[x<0]=0`); (b) `Corwin_Schultz_Spread_1`:
remove (recommended; not a spread on mid bars, MDA ~0, one fewer candidate for spurious selection) or keep as a registered
data-constrained deviation with corrected descriptions; (c) `Becker_Parkinson_Vol_1`: keep Snippet 19.2 as printed, k2 vs paper
registered as a note; (d) `Parkinson_Vol_20`: keep; (e) no other Ch.19 feature (no data); correct the module docstring (the
book does not single out Parkinson / C-S as the only High/Low features; Roll needs only trade prices). All at the RECERT.

### 4S.1 Owner decision (2026-10-31): "Approved, proceed with (a) through (e)"

| Item | Register | What was done |
|---|---|---|
| (a) warm-up NaN | D071 | `_get_alpha` and `becker_parkinson_volatility` use `.mask(x < 0, 0)` (NaN kept) instead of `.where(x >= 0, 0)` (NaN -> 0), as the book's `x[x<0]=0`. Test: equal to Snippets 19.1-19.2 as printed for sl = 1, 2, 20, and NaN on every row the book drops. |
| (b) Corwin-Schultz | D072 | `Corwin_Schultz_Spread_1` REMOVED from all 6 feature_expansion.py and feature_schema.json (uniform, 187 entries) and from native_one_row_feature_engine (ENGINE_VERSION v1_7). `corwin_schultz_spread` stays in the module as the book's code, feeding no feature. Registered deviation. |
| (c) Becker-Parkinson | D073 | Snippet 19.2 kept as printed; note: k2 where the paper's eq.(12) has sqrt(4 log 2) (1.0435 sigma at zero spread; test). Schema description updated. |
| (d) Parkinson | — | Kept unchanged. |
| (e) docstring | — | Module docstring rewritten: the two High/Low estimators are features, C-S is book code only, and every other Ch.19 feature needs data we do not hold (trade prices, traded volume, order book, options). No other Ch.19 feature added. |

**Consequence until the RECERT retrain.** The current models of HK dhc, JPN rs, XAU rs and XAU vc select
`Corwin_Schultz_Spread_1` (and the Ch.18 `Entropy_Kontoyiannis_100`), so Phase C.1 still cannot compile them until they are
retrained (same retrain as D066). The committed portfolio_live/config/phase_c/feature_contract.json and generated model
artifacts are untouched.

**Verification.** tests/test_afml_ch19_microstructure.py (10) and tests/test_afml_ch18_entropy.py (19) pass on venv39 and
venv312. Scratch Phase C.1 from the new schemas: GBPJPY drc and JPN vc compile with 187 core features; the other 4 fail
as above. Phase C.2 (venv39, that scratch contract, status forced PASS for the 2 assets): PASS, 187 core features incl.
Parkinson_Vol_20 and Becker_Parkinson_Vol_1 compared with the research batch every step, 128 steps. Full venv39 suite:
562 pass, 74 skipped; the same 8 Phase C failures/errors as after Ch.18 (cleared by the RECERT) and the clock-dependent
test_live_bar_memory_authority. The suite also caught a Ch.18 records gap (tests/test_mldp_backlog): D068 added to the
backlog and D067's status marked "no further work" (it needs none); D071 added to the backlog.

## 4T. AFML Ch.20 — multiprocessing and vectorization (audit 2026-10-31; CLOSED 2026-10-31, no change)

Book pages: AFML Ch.20 = pdf 330-344 = pp.303-317 (s.20.1-20.6, Snippets 20.1-20.14, Figures 20.1-20.2, exercises
20.1-20.6). A chapter on computation, not method. Audit questions, from the chapter's own rules: (1) a serial path for
debugging that gives the parallel result (p.310); (2) output put back in order after asynchronous collection (Snippets
20.7 / 20.12 sort); (3) load balance (p.307, p.310); (4) pickling (s.20.5.4); (5) memory-aware reduction (s.20.5.5-20.6).
Evidence: logs/ch20_audit_evidence_20261031.txt; tool: shared/research/ch20_multiprocessing_audit/book_snippets_check.py.

**What Quantara has.** No port of the book's engine (mpPandasObj, linParts, nestedParts). Step 01 runs its purged folds
in a ProcessPoolExecutor (`map`, seed per job, workers = 1 serial); allocator 08 / 08c / 09 / 10 / 11 run chunked
simulations (25 starts per task) in ProcessPoolExecutors with a `--workers 1` serial path; research tools use ordered
`map`; the live orchestrator uses threads (live stopped). Edges run as separate processes; same-asset exports serially
(shared HMM continuation cache; already in the runbook, Step 10).

**Findings.**
1. Step 01 serial and parallel (3 workers) outputs are byte-identical (synthetic MLAM data). No test compares serial with
   parallel output anywhere.
2. Allocator 08 and 09 return rows in completion order when workers > 1 (10 and 11 restore input order). Every decision
   downstream is order-independent (groupby statistics; 08b pivots and sorts by start; 10 keeps its own order; 11 sorts
   its starts), but the run CSVs change row order from run to run, and 09's `bootstrap_top` (fixed seed, positional
   draws) would give different confidence intervals with workers > 1 than serially -- the Heisenbug of p.310. Latent:
   the bootstrap is switched off (RUN_BOOTSTRAP_ON_TOP_N = 0, BOOTSTRAP_SAMPLES = 0).
3. Step 01 queues the heaviest job (full-sample fit) last; p.307 front-loads heavy molecules. Speed only.
4. Our triple-barrier labeler stops at the FIRST touch in all 6 edges (the book's own Snippet 20.3 does not, below).
5. Ch.17/18 per-bar features add about 9 minutes to a 70,000-bar export; CPCV takes 53-78 minutes and the Step 01 audit
   2.2-4.8 hours per edge, so the export is not the RECERT bottleneck.

**Book errata (run with Python 2 -> 3 edits only).** Snippet 20.3 keeps `continue` and returns the LAST touch, not the
earliest (differs on 96.8 % of touching paths); Snippet 20.4 partitions the 1,000 rows but slices columns, so only 10 %
of the 10,000 paths run; Snippet 20.7 uses undefined `argList` (NameError) and DataFrame.append (removed in pandas 2.0);
most snippets are Python 2; p.304 GIL is one lock per process, not per core; p.305 "1/5000 on 5000 CPUs" with 24 jobs;
nestedParts "deviates slightly" understates small N (N = 100, M = 24: 145-279 tasks vs 210). Checked correct: the r_m
formulas (p.308), Snippets 20.5-20.6, Figure 20.1.

**Proposals for the owner.** (a) Allocator 08 and 09 return rows in task order, as 10 and 11 already do (the book's sort
after unordered collection); outputs then equal the serial run, and the bootstrap becomes order-independent if ever
switched on. (b) Add serial = parallel tests (Step 01; allocator 08 / 09 / 10 / 11 runners on a small input).
(c) Optional: Step 01 submits the full-sample job first (speed only, identical results). Not proposed: porting the
book's engine, parallelising the per-bar feature loops, any live threading change.

**Owner decision (2026-10-31): "Nope, nothing should be done here".** None of (a)-(c) is implemented; no register
entry (no deviation from a method). Known and accepted: allocator 08 / 09 run CSVs change row order between parallel
runs (no decision depends on it), 09's bootstrap would be order-dependent if switched on, and no serial = parallel test
exists. Next: AFML Ch.21.

## 4U. AFML Ch.21 — brute force and quantum computers (audit 2026-10-31; CLOSED 2026-10-31, no change)

Book pages: AFML Ch.21 = pdf 346-355 = pp.319-328 (s.21.1-21.6, Snippets 21.1-21.7, Figure 21.1, exercises 21.1-21.5).
One worked problem: the N x H trading trajectory that maximises a multi-horizon Sharpe ratio net of square-root
transaction costs under sum |omega| = 1, discretised (pigeonhole partitions x signs) and searched exhaustively, or by a
quantum annealer. Evidence: logs/ch21_audit_evidence_20261031.txt; tool: shared/research/ch21_brute_force_audit/.

**What Quantara has.** Nothing of this kind: no rebalanced weight trajectory, no integer / quantum solver. Bets are sized
once at entry and held to their barriers, costs are per trade (broker cost model, Gate C), HRP weights are fixed per
allocator run. Our discrete searches (Gate A 405-cell grid, Step 04b / 04c thresholds, allocator 08 / 08c / 09 risk grids)
are small finite sets enumerated in full -- the chapter's brute force -- and the searching is paid for (D050-A; Ch.11 /
Ch.14 full-N PBO, all-trials DSR / FWER).

**Checks of the chapter (snippets run).** pigeonHole yields each ordered composition once (C(K+N-1, N-1) already counts
order, so the p.321-322 permutations passage adds nothing); getAllWeights repeats vectors wherever a weight is 0 (3 assets:
42 of 80 duplicates; 4 assets: 368 of 560); the "global" dynamic optimum is global over the 1/k grid only (static
continuous solution better in 1 of 200 seeds); evalTCosts fixes the initial allocation at 0.

**Book errata.** "non-continuous transaction costs" (sqrt|dw| is continuous); time-varying mu / V do not by themselves make
the problem non-convex (the L1 equality, concave costs and ratio objective do); "evaluating multiple solutions at once"
overstates quantum computation (annealers are heuristic); Garleanu-Pedersen is JF 68(6), December 2013 (cited 2012);
typos (pp.319, 320, 324; Snippet 21.6).

**Recommendation.** No change: the chapter's problem does not exist in Quantara, and its principle (enumerate a small
discrete set when no closed form exists, and pay for the search) is already how our grids work.

**Owner decision (2026-10-31): "Agreed"** -- no change, no register entry. Next: AFML Ch.22.

## 4V. AFML Ch.22 — high-performance computational intelligence (audit 2026-10-31; CLOSED 2026-10-31, no change)

Book pages: AFML Ch.22 (Wu and Simon) = pdf 356-379 = pp.329-352 (s.22.1-22.8, Figures 22.1-22.10; no snippets or
exercises). A description of LBNL's CIFT project: HPC vs cloud, MPI, HDF5, in situ processing, and use cases (supernovae,
fusion plasma, electricity baselines, the 2010 Flash Crash with VPIN / HHI and a non-uniform FFT). Evidence:
logs/ch22_audit_evidence_20261031.txt.

**What applies.** VPIN, the fragmentation HHI and the FFT / TWAP evidence need trade prints and traded volume (Ch.19 data
limit). The LTAP lesson (recursive predictions accumulate error) is met: no prediction is fed back as a feature. Calibrated
confidence (supernova workflow) is met by Step 04. HDF5 vs ASCII: our ticks and bars are CSV; measured one weekly tick
file 1.31 s as CSV with timestamp parsing vs 0.007 s as a numpy binary (about 190x), but a full pass over one asset's ticks
is about 5 minutes while CPCV takes about an hour per edge and the Step 01 audit 2-5 hours -- not a bottleneck.

**Book checks.** The chapter's speed-up arithmetic re-done: all correct (21x, 234x, 3.7x, 720x, 527,040 = 366 x 24 x 60).
Errata are citation slips (Figure 1-3 for 22.1-22.3; Yelick 2010 / 2011; Armburst, Abbot, Yen, Shoshami, Snir 1988;
"Nicholas" for Law, N. M. et al. 2009; GBT / GTB; Moore's law as 18-month performance doubling).

**Recommendation.** No change.

**Owner decision (2026-10-31): "Agreed"** -- no change. AFML Ch.1-22 complete. Next (owner): bring
Quantara_Runbook.docx fully up to date, then MLAM (Ch.1, Ch.5, Appendix A not yet walked through on their own).

### 4V.1 Runbook review (2026-10-31, Revision 29)

Every paragraph of Quantara_Runbook.docx Revision 28 (949) was re-read against the code, config/mldp_deviation_register.json
and config/mldp_implementation_backlog.json. Revision 29 (editor docs/tools/edit_runbook_rev29.py; Revision 28 backup in
logs/) corrects: branch v2 -> v3; Gate E order (holdout_replay reads final_live_threshold from the live manifest, so Gate E
runs after the allocator and Section 8, not before); Step 7B / 10I wording after D050-A; feature-schema counts (187); the
Step 6 schema-validator note (validate_edge_feature_schema.py exits 1 on 24 computed_by entries, 5 of them since
2026-09-11); lifecycle driver path and pre-walkthrough timing; Step 13 / KEY RULES gates (D054, D055, Gate E after the
allocator); Section 6A STATE (4 deployable edges, live stopped). Adds: owner standing rules (Section 2);
07b_dsr_psr_validation.py and 07c_backtest_statistics_validation.py to the Section 7 list (were missing;
00c_capture_broker_spread_profile.py superseded); the venv39 pickle scan before Phase C (11B.2); known pre-RECERT test
failures (12.3: 8 Phase C + 1 date-dependent); the RECERT sequence (16C); Appendix 46 (Ch.20-22). Ledger addendum
RUNBOOK_v5.3_ADDENDUM_2026-10-31_AFML_CH20_22_REV29_REVIEW.

Found, not changed (owner decisions): register statuses D011 ("OPEN", now met by D051 / D054 / D055) and D006 ("rescan
required", met by D050-A) are stale; D005 / D011 / D028 are scheduled "at Ch.14", which is closed; test_live_bar_memory_authority
needs a fresh fixture before C5; validate_edge_feature_schema.py vs the computed_by convention; feature_groups.py (batch
tool) lives only in Desktop/Quantara_tools_backup_2026-10-22.

Owner 2026-10-31 ("Yes, fix 1 to 3"; feature_groups.py stays outside the repo), done as Revision 30 (Appendix 47; ledger
addendum RUNBOOK_v5.3_ADDENDUM_2026-10-31_REV30_FIXES): (1) register D005 / D006 / D011 superseded (by D054 / D050-A,
D050-A, D051 / D054 / D055), D028 stays open with its approved test (s.4N.1) moved to the RECERT; backlog D005 / D006 /
D011 removed; (2) test_live_bar_memory_authority's periodic-threshold test freezes utc_now_ts at 2026-07-03 12:00 UTC
(fixture day + 1; ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS = 120 had aged the fixture out), 10 / 10 pass; (3) the norm since
2026-09-11 (commit 4b58127f) names feature_expansion.py::add_v1_feature_expansion in computed_by for the features it adds,
and build_features calls it; no pipeline step runs the validator, so it had failed unnoticed since. The validator now
accepts that path when runtime.py calls add_v1_feature_expansion; all 6 schemas pass; schemas unchanged.

### 4V.2 Grid-dimension study and EWM span test (2026-10-31; owner decision: grid A, register D074)

Owner question: are some of the 405-cell grid's dimensions redundant (e.g. the EWM span)? Study:
`shared/research/grid_dimension_sensitivity/` (392 recovered Gate A runs on 196 sides; 12 full Gate C replays). EWM 100
won 12 / 12 Gate C replays; 20 / 50 never won. Pre-registered test (`logs/ewm_span_test_20261031/PREREGISTRATION.md`):
Gate A + Gate C over EWM 100,150,200,300 on the 6 Gate C edges (540 cells each, feature schema at 63f4a570 = the stored
datasets'), EWM 100 cells reproduce the old replays (XAUUSD within 0.001 R: cost model refitted 2026-10-17 after them).

| Edge | grid A winner | vs runbook winner | PBO / Prob[OOS<=0] runbook | grid A {100,150,200} |
|---|---|---|---|---|
| HK dhc short | E150 V168 2:1 | x1.46 | 0.013 / 0.338 | 0.008 / 0.273 |
| GBPJPY drcf long | E200 V168 2:1 | x1.17 | 0.153 / 0.430 | 0.164 / 0.344 |
| JPN rs long | E100 V96 2:1 (same cell) | x1.00 | 0.349 / 0.400 | 0.360 / 0.323 |
| JPN vc long (killed) | E200 V168 2:1 | x1.19 | 0.119 / 0.527 | 0.145 / 0.524 (FAIL) |
| XAU rs long | E200 V16 2:1 | x1.23 | 0.085 / 0.058 | 0.073 / 0.043 |
| XAU vc long | E200 V16 2:1 | x1.22 | 0.288 / 0.171 | 0.422 / 0.155 |

Pre-registered rule: not met (higher spans best on 5 / 6, but PBO worse on JPN vc and XAU vc). Above span 100 the paired
trade-level gains are small (about 2-14 % of matched pairs significant); gross R rises with span on HK and XAUUSD, flat
on JPN and GBPJPY (the gain there is cost in R).

Owner 2026-10-31: "Go with option 2" -> register D074 (recorded deviation from the pre-registered rule): the runbook grid's
EWM spans become 100,150,200 (405 cells; D050-A unchanged). Caveat recorded: XAU vc PBO 0.288 -> 0.422 (still < 0.5). No
deployed edge changes now; every edge re-selects on the new grid at the RECERT, and the test runs above join each edge's
trial record. Runbook Revision 31 (Appendix 48); ledger addendum RUNBOOK_v5.3_ADDENDUM_2026-10-31_EWM_SPANS_D074.

## 4W. MLAM Ch.1 — Introduction (audit 2026-10-31; CLOSED 2026-11-02, D075)

Book pages = pdf - 6 (pdf 7-29 = pp.1-23). No code snippets. Read line by line; the operative statements and our status:

| MLAM | Book | Quantara | Status |
|---|---|---|---|
| s.1.1 p.2 | an edge is justified only by someone else's systematic mistake; "Without a testable theory that explains your edge, the odds are that you do not have an edge at all"; the theory explains facts AND counterfactuals (x implies y, absence of y implies absence of x) | thesis card per edge: mechanism, who_loses_and_why, regimes, counterfactuals, falsifiers (edge_yaml_contract.py; owner-approved 2026-10-23) | matches on paper |
| s.1.2.1 p.3, s.1.2.2 p.4, s.1.4.2 p.9 | backtests cannot prove a true positive; a theory has testable implications beyond the backtest; it must explain negatives too; implications tested independently | **the 12 counterfactual tests on the 6 cards were never run** (C1/C2 per edge: inverted trigger gate -> no edge; branch split; GBPJPY swap removed). Only the falsifiers (holdout / live decommission rule) have been exercised (Gate E 2026-10-23) | **gap** |
| s.1.2.2 p.4 footnote 2 | test the theory with non-price evidence where possible (e.g. who lost money) | price/tick data only | backlog "information sources beyond price" (not started) |
| s.1.3 p.5 | ML uses: existence, importance (MDA), causation, reductionist, retriever | importance: clustered MDA/MDI/SFI (Ch.8 / MLAM Ch.6, D041-D042); the meta-model is used as a forecaster | allowed (p.4: "You may use ML successfully for making financial forecasts") |
| s.1.4.1 pp.6-8, Fig 1.1 | train-set overfitting: generalization error (resampling + Monte Carlo on synthetic data), regularization (number of variables; structure), ensembles (RF: CV, limited depth, more trees) | purged CV / CPCV (Ch.7, D052 at retrain); feature surgery (Ch.8); depth / min_weight_fraction_leaf by Optuna (Ch.9); sequential-bootstrap bagging 1000 trees (D034); synthetic-data check only for the importance code (Ch.8) | covered except a synthetic-data generalization test of the model pipeline -- MLAM Appendix A gives the procedures; audit there |
| s.1.4.2 pp.8-9, Fig 1.1 | test-set overfitting: report all trials (number, variance) -> DSR / FWER; CPCV; Monte Carlo synthetic data; "my advice is that you apply all of them"; backtest-tweak-backtest is futile -- a bad backtest is a reason to fix the research process | D050-A (all trials), D051 (PBO all trials), D054 DSR / FWER (RECERT), D052 CPCV paths (retrain), Ch.13 synthetic OTR (D053) | covered / scheduled |
| s.1.4.2 p.8 vs D074 | -- | D074 (EWM spans 100,150,200) was chosen on the same 6 edges' replays: a research-process change selected on the data it will be judged on. Its test cells stay in each edge's trial record (D050-A rule 4) | noted; no further action |
| s.1.9 p.21 + footnote 7 | prefer classifiers (sign) over regression; sign and size often depend on different features -> meta-labeling | primary rule (side) + meta-label classifier (size via AFML Ch.10) | matches |
| s.1.9 p.21 | bet sizing at least as important as prediction; portfolio construction more | Ch.10 / Ch.16 closed | matches |

Findings for the owner:
1. Counterfactual tests never run (gap vs s.1.1 p.2, s.1.2.2 p.4). Proposal: pre-register and run the 12 card tests on
   RESEARCH data now (holdout sealed: the card wording "research+holdout" becomes research now, holdout at the final
   one-look). Method per gate-inversion test: regenerate the edge's candidate events with that gate inverted, label them
   with the edge's locked geometry on mid prices (D016), report mean R with week-clustered CI next to the edge's own
   events; PASS = the inverted set's mean R is below the edge's and its CI includes 0 or is below 0 (the card's own test
   text). Branch tests: split the edge's Gate C trades by Range_Structure_Event_Type. GBPJPY C2: the edge's Gate C trades
   re-priced without swap. Theory tests, not strategy trials (nothing is selected on them); every result reported. A
   failed counterfactual means the card's mechanism is wrong -> owner decides (rewrite card post hoc is NOT allowed;
   the edge is flagged at the RECERT).
2. Synthetic-data generalization test of the model pipeline -> audit with MLAM Appendix A (no change now).
3. D074 recorded against s.1.4.2 p.8 (above); no change.

### 4W.1 Counterfactual tests (owner 2026-10-31: "Yes, pre-register and run the 12 tests in parallel")

Pre-registration `logs/counterfactual_tests_20261031/PREREGISTRATION.md` (written before any result); driver
`shared/research/counterfactual_tests/run_counterfactuals.py` (+ `gbpjpy_swap_test.py`); results
`logs/counterfactual_tests_20261031/*.json`. Research data only (holdout sealed). Reproduction: on all 6 edges the
uninverted path reproduces the stored candidate events exactly and every stored model row's realized_r (max diff 4e-16).
Gross mid R, 95 % week-clustered CI. Inverted sets never overlap the edge's events.

| Edge (edge mean R, CI) | Test | Inverted / split set | Verdict |
|---|---|---|---|
| GBPJPY drcf (+0.117, +0.001..+0.233) | C1 completion < 0.50 | n 338, +0.212 (-0.115..+0.540) | FAIL (higher than the edge) |
| | C2 swap added back (Gate C net, H6_E100_V168_PT2_SL1) | +0.086 -> +0.078 (-0.042..+0.198) | INCONCLUSIVE (swap ~10 % of net; certified net CI also includes 0 week-clustered) |
| HK dhc (+0.146, +0.035..+0.257) | C1 hazard < 0.48 | n 2205, +0.090 (-0.048..+0.228) | PASS |
| | C2 recovery > 0.40 | n 38, -0.441 (-0.980..+0.097) | INCONCLUSIVE (< 100 events; direction as the card) |
| JPN rs (+0.159, +0.043..+0.276) | C1 0 < t < 0.5 | n 1001, +0.171 (+0.029..+0.313) | FAIL |
| | C2 branch split | near-high 4844 events +0.159; near-low 1 event | INCONCLUSIVE (one-branch edge) |
| JPN vc (+0.104, -0.001..+0.210) | C1 not calm | n 4467, +0.116 (+0.010..+0.223) | FAIL |
| | C2 calm, 0 < t < 0.5 | n 3386, +0.127 (+0.023..+0.231) | FAIL |
| XAU rs (+0.154, +0.078..+0.230) | C1 0 < t < 0.5 | n 934, +0.113 (-0.003..+0.230) | PASS (marginal: CI lower bound -0.003) |
| | C2 branch split | near-high 4766 events +0.154; near-low 3 events | INCONCLUSIVE (one-branch edge) |
| XAU vc (+0.124, +0.056..+0.192) | C1 not calm ("weaker") | n 4536, +0.120 (+0.053..+0.187) | PASS by the rule, but only 0.004 R weaker: calm is not the cause |
| | C2 calm, 0 < t < 0.5 | n 3251, +0.144 (+0.064..+0.224) | FAIL |

Tally: PASS 3 (HK C1; XAU rs C1 marginal; XAU vc C1 by the letter only), FAIL 5, INCONCLUSIVE 4. Reading: on 4 of 6
cards the stated cause does not separate the edge's events from the inverted ones -- the inverted sets earn about the
same or more gross mid R. That fits a generic long drift of these instruments over 2020-26 (the 5/6 long bias found
2026-10-22) more than the cards' mechanisms. Only HK's hazard gate behaves as its card says. Per the pre-registration the
failing edges are flagged for the owner at the RECERT; cards are not rewritten. Owner decision pending.

### 4W.2 Drift baseline (owner 2026-11-02: "Yes, run the drift baseline in parallel")

Pre-registered in logs/counterfactual_tests_20261031/PREREGISTRATION_DRIFT_BASELINE.md before any result; driver
shared/research/counterfactual_tests/run_drift_baseline.py; outputs `<ASSET>_<edge>_drift.json` there. Per edge: 20,000
random bars (seed 20261102, same period, same side, same locked geometry, the edge's own labeler; gross mid R) versus the
edge's own events. All 6 reproduce the stored events and model rows exactly. Primary = month-matched baseline (each edge
event replaced by a random bar from the same calendar month); difference CI uses the edge's week-clustered SE.

| Edge | Edge R | Random, all months | Random, month-matched | Edge minus month-matched (95 % CI) | Verdict |
|---|---|---|---|---|---|
| GBPJPY drcf long | +0.117 | +0.097 | +0.060 | +0.057 (-0.066..+0.181) | NOT DISTINGUISHABLE FROM DRIFT |
| HK dhc short | +0.146 | +0.094 | +0.146 | +0.000 (-0.118..+0.118) | NOT DISTINGUISHABLE FROM DRIFT |
| JPN rs long | +0.159 | +0.085 | +0.133 | +0.027 (-0.097..+0.150) | NOT DISTINGUISHABLE FROM DRIFT |
| JPN vc long | +0.104 | +0.083 | +0.122 | -0.017 (-0.130..+0.095) | NOT DISTINGUISHABLE FROM DRIFT |
| XAU rs long | +0.154 | +0.084 | +0.137 | +0.018 (-0.065..+0.100) | NOT DISTINGUISHABLE FROM DRIFT |
| XAU vc long | +0.124 | +0.074 | +0.113 | +0.011 (-0.065..+0.086) | NOT DISTINGUISHABLE FROM DRIFT |

Reading: 6/6 not distinguishable from random same-side entries in the same months. Random entries alone earn +0.07..+0.10
R on every asset; the edges add +0.02..+0.07 R over all-months random, and about 0 over month-matched random (the edges
fire more in the months the instrument moved their way). The iid permutation p-values (in the JSONs; e.g. XAU rs vs
all-months p=0.0001) ignore event clustering in time and overstate significance; the week-clustered CI is the
pre-registered verdict. Gross mid R only (costs would lower edge and random alike). Flagged for the owner at the RECERT
with 4W.1; nothing rewritten, retired or re-selected on this test.

### 4W.3 Model-filtered drift test (owner 2026-11-02: "yes you can so we can get the full picture")

Pre-registered as the addendum to PREREGISTRATION_DRIFT_BASELINE.md before any filtered result. The random pools were
re-labeled with the same seed and saved (`*_drift_pool.csv`); all 6 reproduce the first run's pool and baselines exactly.
Taken trades = calibrated OOF rows with live_calibrated_probability >= final_live_threshold (portfolio_live/live_manifest.json).
Driver shared/research/counterfactual_tests/run_drift_filtered.py; outputs `*_drift_filtered.json`.

| Edge | Threshold | Taken / OOF | Taken R | Month-matched random | Taken minus random (95 % CI) | Verdict | Taken minus not-taken (95 % CI) |
|---|---|---|---|---|---|---|---|
| GBPJPY drcf long | 0.38 | 2738 / 4049 | +0.191 | +0.064 | +0.127 (-0.020..+0.274) | NOT DISTINGUISHABLE | +0.211 (+0.033..+0.390) |
| HK dhc short | 0.38 | 2571 / 4205 | +0.216 | +0.132 | +0.084 (-0.057..+0.224) | NOT DISTINGUISHABLE | +0.208 (+0.001..+0.415) |
| JPN rs long | 0.36 | 4461 / 4461 | +0.169 | +0.139 | +0.030 (-0.099..+0.159) | NOT DISTINGUISHABLE | n/a (threshold takes every row) |
| JPN vc long | 0.42 | 173 / 4891 | +0.110 | +0.138 | -0.028 (-0.412..+0.356) | NOT DISTINGUISHABLE | -0.006 (-0.346..+0.334) |
| XAU rs long | 0.48 | 2251 / 4379 | +0.236 | +0.143 | +0.093 (-0.009..+0.196) | NOT DISTINGUISHABLE | +0.129 (-0.000..+0.258) |
| XAU vc long | 0.47 | 1708 / 4606 | +0.194 | +0.122 | +0.073 (-0.032..+0.177) | NOT DISTINGUISHABLE | +0.088 (-0.029..+0.204) |

Reading: 6/6 still NOT DISTINGUISHABLE from same-month random entries, even with the threshold chosen on these same rows
(bias in the edges' favour). But the model layer moves every edge with a real filter up: GBPJPY, HK, XAU rs and XAU vc
gain +0.07..+0.13 R over random (raw events: +0.00..+0.06), and the model separates its own taken from not-taken events
on GBPJPY and HK (CI above 0) and almost on XAU rs. So the triggers carry drift; whatever edge exists is in the
meta-model, not in the card's mechanism -- and it is not yet strong enough to clear drift on research data. JPN rs's
threshold filters nothing; JPN vc takes 173 trades (no power). Gross mid R; no costs.

### 4W.4 Owner decision 2026-11-02 -- MLAM Ch.1 CLOSED

Owner: "i agree and approve" (theory check as a pre-registered check at the RECERT and for every new edge; drift baseline
on the model's trades for existing edges; new theses must beat the same-month random baseline before Gate A) and "make
sure your recommendation is saved well as a standing rule across every place that needs it saved". Saved as THEORY CHECK
RULE D075: docs/mldp/rules/THEORY_CHECK_RULE_D075.md (canonical); register D075; backlog D075 (code_pending: one Gate 0
tool; edge.yaml contract; RECERT re-checks); config/research_methodology_standing_rules.json THEORY_CHECK_GATE_0_D075;
ledger RUNBOOK_v5.3_ADDENDUM_2026-11-02_THEORY_CHECK_D075; Quantara_Runbook.docx Revision 32 (Section 2, Section 6A
Steps 4B, 6B, 7, promotion rule, Appendix 49). Counterfactual PASS rule tightened for new theses to (edge - inverted)
95 % CI lower > 0 (XAU vc C1 passed the 2026-10-31 rule by 0.004 R). No deployed edge changes now; the 6 edges'
results go to the owner at the RECERT. Next: MLAM Ch.2.

### 4W.5 Owner decisions round 2 + Gate 0 geometry panel (2026-11-02)

Owner: counterfactual PASS = (edge - inverted) 95 % CI lower > 0 with joint week clustering -- "Approved"; keep both the
raw Gate 0 and the model-filtered promotion check; run the 9-cell geometry panel -- "yes"; and, on data: "we only
literally deal with price data ... be lenient where possible" (memory feedback_price_only_data_leniency).

Panel (logs/gate0_geometry_panel_20261102/PREREGISTRATION.md; driver shared/research/counterfactual_tests/run_gate0_panel.py;
results gate0_panel_results.json): 6 edges x 9 cells (lookback 1/4/8 h x vertical 4/24/96 h, 1:1, EWM 100) + each locked
cell L. Reproduction: cell L gives the stored model rows exactly (max diff 0.0) on all 6.

| Edge | Edge minus same-month random, range over 9 cells | Closest cell | Counterfactuals (joint-clustered) |
|---|---|---|---|
| GBPJPY drcf | +0.012..+0.038 | H4_V96 +0.038 (-0.038..+0.114) | C1 FAIL 9/9 (diff -0.10..+0.02) |
| HK dhc | +0.004..+0.030 | H4_V96 +0.030 (-0.028..+0.089) | C1 FAIL 9/9 (diff -0.02..+0.02); C2 38 events, sign flips |
| JPN rs | +0.011..+0.046 | H1_V4 +0.042 (-0.002..+0.086) | C1 FAIL 9/9 |
| JPN vc | -0.003..+0.017 | H1_V4 +0.017 | C1, C2 FAIL 9/9 |
| XAU rs | +0.004..+0.026 | H1_V96 +0.026 | C1 FAIL 9/9 |
| XAU vc | -0.004..+0.015 | H1_V24 +0.015 | C1, C2 FAIL 9/9 |

Pre-registered decision: STABLE (every edge x test has the same verdict in >= 8/9 cells) -- but trivially: every test
fails in every cell, so the panel shows these 6 edges fail at any symmetric geometry; it cannot show whether a PASSING
thesis would flip with geometry. Other findings: (1) gross R of edge AND random entries rises with lookback and vertical
(bigger barriers, longer holds collect more drift), while edge minus random stays flat (-0.004..+0.046); (2) the locked
2:1 cells inflate both (random +0.07..+0.10 at L vs +0.00..+0.06 at 1:1): asymmetric PT on a long book in a rising
market harvests drift; (3) short cells have the tightest CIs (H1_V4 about +-0.04 R vs H8_V96 about +-0.06-0.08 R) -- more
power, not a weaker bar.

### 4W.6 Owner decision round 3 (2026-11-02): "Approved A + B + C, save it everywhere, then MLAM Ch.2"

D075 amended: A -- the thesis card pre-registers its Gate 0 cell from the panel (1:1, EWM 100, lookback 1/4/8 h x vertical
4/24/96 h, matched to the thesis horizon; default H4_V24; never changed after a result). B -- Gate 0 one-sided 95 %
(diff - 1.645 SE > 0) for the drift baseline, the counterfactuals (joint week clustering) and the post-Gate C drift
confirmation. C -- Gate 0 runs a thesis on all assets at once per side, pooled diff with week clusters shared across
assets, each asset proceeding only if its own diff > 0. The model-filtered promotion check stays two-sided 95 % (1.96).
Saved in: docs/mldp/rules/THEORY_CHECK_RULE_D075.md; register D075; backlog D075 (Gate 0 tool with A/B/C; edge.yaml
gate_0.cell); config/research_methodology_standing_rules.json; ledger RUNBOOK_v5.3_ADDENDUM_2026-11-02_GATE0_LENIENCY_ABC;
Quantara_Runbook.docx Revision 33 (Section 2, Section 6A Steps 4B, 6B, 7, promotion rule, Appendix 50); memory.

## 4X. MLAM Ch.2 — Denoising and detoning (audit 2026-11-02)

Book pp.24-37 (pdf = book + 6). Code: shared/colab_portable/denoise_onc_snippet.py (canonical, re-exported by
shared/models/denoise_onc.py), portfolio_allocator/07_hrp_linkage_robustness.py (rule mlam_ch2_book_v2, AFML Ch.16
walkthrough s.4P), shared/features/clustered_feature_importance.py (Step 01), shared/research/dsr_all_trials.py,
portfolio_allocator/07b_dsr_psr_validation.py, shared/research/allocator_threshold_pbo.py.

| § / snippet (page) | Book | Quantara | Status |
|---|---|---|---|
| 2.1 (p.24) | "Throughout this Element, we assume that empirical covariance and correlation matrices have been subjected to this procedure" | Step 07 HRP denoised; trial-clustering ONC (DSR E[K], 07b portfolio trials, threshold PBO) on RAW correlation | **gap** (T1) |
| 2.2 / Snippets 2.1-2.2 (pp.24-25) | MP pdf, getPCA, fitKDE | mp_pdf (adds nan_to_num at the end points), get_pca, fit_kde | matches |
| 2.4 / Snippet 2.4 (pp.26-28) | findMaxEval: minimize SSE(MP pdf, KDE) over var in (1e-5, 1-1e-5), x0 .5, fallback var = 1; nFacts = N - searchsorted | find_max_eval identical; Step 07 nFacts book-exact; denoise_correlation uses count(eval > lambda+) with a floor of 1 | Step 07 matches; **snippet floor of 1 deviates** (only bites when nFacts = 0) |
| 2.4 (p.26) bWidth | Snippets use bWidth .01; footnote 8 p.35 + Exercise 7 p.37: CV bWidth left as an exercise | 0.01 everywhere in force (legacy Step 01 path had 0.02, no longer called) | matches the book's example; sensitivity T2 |
| 2.5.1 / Snippet 2.5 (p.29) | constant residual eigenvalue, trace preserved, rescaled to unit diagonal | denoised_corr_constant_residual_eigenvalue identical | matches |
| 2.5.2 / Snippet 2.6 (pp.29-31) | targeted shrinkage, alpha | denoised_corr_targeted_shrinkage identical (not used) | matches |
| 2.6 (pp.30-32) | detone the market component(s) "if it exists (a hypothesis that can be tested statistically)"; detoned matrix singular -> clustering only, or optimise on surviving components | detone_corr identical; Step 07 detones the clustering matrix only after the bootstrap market test (owner 2026-10-30); never used for weights | matches |
| 2.7 / Snippets 2.7-2.11 (pp.32-36) | Monte Carlo: denoising beats Ledoit-Wolf for min-var and max-Sharpe portfolios | Ch.16 allocation tests (s.4P) re-ran the comparison on our streams (HRP denoised more stable) | done (AFML Ch.16) |
| 2.8 (pp.36-37) | applies to "any use of the covariance matrix", e.g. X'X before inverting; factor covariances | no regression on a large X'X in the pipeline; DSR cluster weights avoid inversion (getIVP extended, owner 2026-10-26) | n/a |
| 2.9 Exercises (p.37) | 1 detoning; 2-6 efficient frontiers; 7 CV bWidth | 1 done (detone_corr); 2-6 not applicable (no mean-variance optimiser); 7 = T2 | partly n/a |
| Related: MLAM p.38, p.54, p.87 | p.87: "In a nonexperimental setting, the researcher should denoise and detone the correlation matrix before clustering"; p.54 detone a strong common component | Step 01 clusters on the VI matrix (MLAM Ch.3; MP does not apply to VI); trial ONC raw | trial ONC = T1 |

Correction: s.4N.1 recorded "no denoising: the section does not ask for it" for the DSR trial clustering. MLAM p.24
(and p.87) make denoising the default for every correlation matrix in the Element; that line was wrong.

Tests (pre-registered, logs/mlam_ch2_tests_20261102/PREREGISTRATION.md; driver shared/research/mlam_ch2_tests.py):
T1 E[K] / DSR / FWER on the 4 all-trials records with the trial correlation raw (V0), denoised (V1), denoised + detoned
when the market test passes (V2); T2 KDE bandwidth sensitivity (0.005-0.1 and Exercise 7's CV bandwidth) of nFacts.

### 4X.1 Results (2026-11-02; logs/mlam_ch2_tests_20261102/t1_*.json, t2_bandwidth.json)

T1 -- V0 reproduces every stored record exactly. 406 trials per edge, T 1393-1515 days, q 3.4-3.7; Marcenko-Pastur fit
at its bound (var 0.99999) on every matrix, nFacts 7-8; first eigenvalue 308-325 (of 406) and the market test passes
on all 4 (same-sign share 1.000): every trial of an edge moves with the edge -- a "strong common component" (p.54).

| Edge | V0 raw: E[K] / DSR / FWER | V1 denoised | V2 denoised + detoned | Verdict change |
|---|---|---|---|---|
| GBPJPY drcf | 2 / 0.685 / 0.088 | 2 / 0.685 / 0.088 | 4 / 0.370 / 0.168 | none (fails DSR and FWER throughout) |
| HK dhc | 2 / 0.650 / 0.108 | 2 / 0.650 / 0.108 | 2 / 0.738 / 0.108 | none (fails throughout) |
| JPN rs | 2 / 0.938 / 0.038 | 2 / 0.939 / 0.038 | 3 / 0.707 / 0.057 | **FWER pass -> fail** (DSR fails throughout) |
| XAU rs | 2 / 0.998 / 0.0002 | 2 / 0.998 / 0.0002 | 3 / 0.998 / 0.0002 | none (passes throughout) |

Denoising alone changes nothing (same clusters within 1 trial; DSR within 0.001). Detoning -- which p.87 asks for before
clustering and p.54 for a strong common component -- splits the trials into 2-4 clusters and moves the DSR (GBPJPY
lower, HK higher, JPN lower); the DSR > 0.95 verdict is unchanged on all 4; JPN's FWER moves from 0.038 to 0.057.

T2 -- nFacts and lambda+ are identical at bWidth 0.005, 0.01, 0.02, 0.05, 0.1 and at the CV bandwidth (Exercise 7:
0.63 for Step 07, 1.0 for the trial matrices) on all 5 matrices; the fit sits at var = 0.99999 everywhere, so lambda+
is the unadjusted (1 + (N/T)^0.5)^2. Bandwidth does not matter here; 0.01 (the book's example) stays.

### 4X.2 Owner decisions (2026-11-02): "Approved 1, 2 and 3" -- D076, MLAM Ch.2 CLOSED

1. Backtest trials are clustered on the denoised correlation, detoned when the market test passes (MLAM p.24, p.87):
   shared/backtest_statistics/afml_ch14_dsr.py trial_clustering_corr / onc_trial_clusters (rule
   mlam_ch2_denoise_conditional_detone_v1; Step 07's bootstrap market test; T <= N keeps the raw correlation), used by
   shared/research/dsr_all_trials.py (the raw-correlation E[K] kept as a diagnostic), portfolio_allocator/
   07b_dsr_psr_validation.py (portfolio trials) and shared/research/allocator_threshold_pbo.py. Verified 2026-11-02: the
   new dsr_all_trials reproduces T1 V2 exactly on all 4 edges (E[K], DSR, FWER) and its raw diagnostic reproduces the
   stored E[K] = 2 (logs/mlam_ch2_tests_20261102/d076_verify/). The stored ch14_dsr_all_trials.json records are NOT
   overwritten: every edge is re-run at the RECERT (backlog D076), then 07b and the D057 threshold PBO.
2. denoise_correlation (canonical snippet + the 6 verbatim Step 01 copies) has no floor of 1 signal factor (Snippet 2.4);
   nFacts = 0 gives the identity and skips detoning.
3. KDE bWidth .01 kept.
Tests: tests.test_afml_ch14_statistics (3 new D076 cases), test_denoise_onc, test_step01_colab_portable_snippet_sync,
test_afml_ch16_hrp, test_afml_ch8_clustered_importance, test_mldp_backlog -- pass on venv39 and venv312. Record:
register D076; backlog D076 (code_done_awaiting_retrain); standing rule DENOISE_BEFORE_CLUSTERING_D076; runbook Revision
34 (Section 2, Section 6A Steps 7 and 10L, Appendix 51); memory.

## 4Y. MLAM Ch.3 — Distance metrics (audit 2026-11-02)

Book pp.38-51 (pdf = book + 6). Code: shared/features/clustered_feature_importance.py (num_bins, var_info, vi_matrix;
Step 01 observation matrix, STEP01_METHOD = "vi"), portfolio_allocator/07_hrp_linkage_robustness.py (correl_distance),
shared/colab_portable/denoise_onc_snippet.py (ONC distance), Step 02 scoring (AFML Ch.9 neg log loss).

| § / snippet (page) | Book | Quantara | Status |
|---|---|---|---|
| 3.2 (pp.38-40) | d = sqrt((1 - rho)/2) is a metric; signed for long-only (negative correlation offsets risk), d_abs = sqrt(1 - abs(rho)) for long-short (position sign overrides correlation sign) | Step 07 HRP d = sqrt((1 - rho)/2) on edge P&L streams (each stream already carries its side, weights are long-only, so a negatively correlated stream does offset risk -- the book's long-only case); ONC (Snippets 4.1-4.2) uses the same signed distance | matches |
| 3.3-3.8 (pp.40-43), Snippets 3.1-3.2 | entropy, joint, conditional, KL, cross-entropy, MI, VI; normalised VI = VI / H(X,Y) | var_info = Snippet 3.2-3.3 (norm=True), with guards: rho clipped to +-(1 - 1e-9), bins >= 1, VI >= 0, H(X,Y) = 0 -> 0 | matches (guards numeric only) |
| 3.6 (p.41) | cross-entropy is the meaningful classification score (AFML s.9.4) | Step 02 tuning and MDA score = neg log loss (AFML Ch.7/8/9 decisions) | matches |
| 3.9 / Snippet 3.3 (pp.43-46) | discretise with the Hacine-Gharbi optimal bins: univariate and bivariate (rho-dependent) numBins; varInfo uses the bivariate bins for the marginals too | num_bins identical; var_info identical | matches |
| 3.10 (pp.46-47) | VI between two partitions (Meila 2007), for comparing clustering outcomes | not used | n/a (no partition comparison in the pipeline; a diagnostic option only) |
| 3.11 / Snippet 3.4 (pp.47-50) | NMI vs correlation: none 0.0068 / 0.0015, linear 0.8907 / 0.9999, nonlinear 0.6439 / -0.008 (size 5,000, seed 0) | reproduced with our num_bins: 0.0068 / 0.0015, 0.8907 / 0.9999, 0.6439 / -0.008 -- exact | matches |
| 3.12 (p.50) | correlation distance for linear codependence; normalised VI for nonlinear ("more appropriate"), with ML that imposes no functional form | Step 01 clusters features on normalised VI (owner 2026-10-24, AFML Ch.8 walkthrough s.4H); return streams (HRP, trial ONC) on correlation -- portfolio variance is linear in the streams | matches |
| 3.13 Exercises (p.51) | 1-3 binning / sample size; 4 VI vs MI; 5 when correlation distance is preferable; 6-7 KL / cross-entropy code; 8 sqrt(1 - rho^2) metric | 1-3: VI normalised so sample size does not drive Step 01; 4: MI suits relevance to a label, VI suits redundancy between features (Step 01's use); 5: HRP / trial clustering (the item clustered is a P&L whose risk is linear); 6-7: sklearn log_loss / scipy entropy | covered |

No gaps: Chapter 3 needs no code change. Book replication (Snippet 3.4) run 2026-11-02 in the session; numbers above.
CLOSED 2026-11-02 by the owner ("Close Ch.3"): no change; the optional model-free feature-label information test was
not run.

## 4Z. MLAM Ch.4 — Optimal clustering (audit 2026-11-02)

Book pp.52-64 (pdf = book + 6). Code: ONC twice -- shared/colab_portable/denoise_onc_snippet.py cluster_kmeans_base /
_make_new_outputs / cluster_kmeans_top (correlation input; backtest-trial E[K] for DSR / FWER, 07b, threshold PBO, D076)
and shared/features/clustered_feature_importance.py onc_base / onc_top (observation-matrix input; Step 01 feature
clusters on the normalised VI matrix, PCA-reduced). Step 07 HRP uses hierarchical single linkage (AFML Ch.16), not ONC.

| § / snippet (page) | Book | Quantara | Status |
|---|---|---|---|
| 4.2 (p.52) | proximity matrix; standardise inputs so no feature's scale dominates | VI is scale-free; trial matrices are correlations; Step 01 PCA projection standardises (p.55) | matches |
| 4.3 (pp.52-53) | partitional vs hierarchical; curse of dimensionality -> project X (or the proximity matrix) on the signal dimensions of Section 2 | Step 01: VI matrix PCA-reduced to the eigenvalues above lambda+ (floor 1 dimension: a projection needs >= 1); trial ONC: 406 x 406 X NOT reduced | Step 01 matches; trial ONC = T4 |
| 4.4.1 (pp.54-55) | detone a strong common component first; approach (c) X = sqrt((1 - rho)/2) as an observation matrix; fn.11 ideally information-theoretic, ONC agnostic to how X is formed | trial ONC: denoised + detoned (D076) then X = sqrt((1 - rho)/2) inside cluster_kmeans_base; Step 01: VI observation matrix (no detoning: VI is not a correlation) | matches |
| 4.4.2 / Snippet 4.1 (pp.55-57) | double loop: n_init x k = 2..maxNumClusters, KMeans n_init=1, keep max q = E[S]/sqrt(V[S]) of silhouette_samples(x) | both implementations identical; std ddof 0 as the book's numpy/pandas calls; initialisations seeded (book unseeded) | matches (seeding = T2/T3) |
| 4.4.3 / Snippet 4.2 (pp.57-59) | per-cluster t-stat, redo the below-average clusters if >= 2, recurse on the sub-correlation, silhouette of the merged partition on the full X, accept only if the new mean t-stat > the redo clusters' mean | identical in both (onc_top: sub-matrix keys x keys for square X, rows for PCA-reduced X; recursive seed + 1) | matches |
| 4.4.3 maxNumClusters | clusterKMeansTop default N - 1 | trial ONC N - 1; Step 01 N - 1 | matches |
| 4.5 / Snippet 4.3 (pp.59-63) | Monte Carlo: random block matrices, shuffled; E[K]/K near 1 (Figure 4.4) | tests/test_denoise_onc.py has one block-recovery case; full replication = T1 | T1 |
| 4.7 Exercises (p.64) | 1 hierarchical vs partitional; 2 GICS + partition distance; 3 biclustering; 4 elbow; 5 equal-size blocks | 1: HRP needs the tree (AFML Ch.16), ONC gives the partition for E[K] and clustered MDA; 2-5 not applicable to our objects (no securities taxonomy) | n/a |

Tests (pre-registered, logs/mlam_ch4_tests_20261102/PREREGISTRATION.md; driver shared/research/mlam_ch4_tests.py): T1 book
Monte Carlo (both implementations, N 20/40/80, 20 generations); T2 seed stability of trial E[K]/DSR/FWER (seeds 0-9);
T3 seed stability of Step 01 feature clusters (partition VI, MLAM s.3.10); T4 PCA-reduced X for the trial ONC (p.55).

### 4Z.1 Results (2026-11-02; logs/mlam_ch4_tests_20261102/)

T1 (book Monte Carlo, 20 generations per N x K, N 20/40/80): median E[K]/K = 1.00 in every K/N bucket for BOTH
implementations (IQR 1.00-1.00; Figure 4.4 reproduced); exact partition recovered in 84 % of runs (cluster_kmeans_top
83.9 %, onc_top 84.1 %); rare misses are under-splits at small K/N (min ratio 0.13-0.33). Both implementations are the book.

T2 (trial ONC seeds 0-9, D076 correlation): E[K] identical across seeds on every edge (GBPJPY 4, HK 2, JPN rs 3, XAU rs
3); DSR identical (GBPJPY 0.3705-0.3761), FWER identical; every verdict identical. Seed 0 is fine for the trial ONC.

T3 (Step 01 feature ONC on the VI matrix, PCA-reduced to 6 dimensions, 155 candidate features, seeds 0-9): NOT stable.
Number of clusters per seed: GBPJPY 2-9, HK 2-26, JPN rs 2-7, JPN vc 2-8, XAU rs 2-4, XAU vc 2-9; partition VI vs seed 0
(normalised by log 155) up to 0.31 / 0.43 / 0.34 / 0.36 / 0.27 / 0.35 -- above the pre-registered 0.10 on every edge.
Post-hoc (t3_quality_posthoc_*.json, written after the result): the silhouette quality q of the returned partitions
differs widely across seeds (e.g. GBPJPY 0.99-3.05, XAU rs 1.51-3.16), so 10 initialisations do not find the maximum of
ONC's own objective on these matrices: the instability is a search-convergence problem, not a flat optimum. Step 01 keeps
or drops features by cluster (rule B), so the selected feature set can depend on the seed (not measured: MDA not re-run).

T4 (trial ONC on the PCA-reduced X, p.55, 6 dimensions, seed 0): E[K] GBPJPY 5 (was 4), HK 2 (2), JPN rs 16 (3), XAU rs
4 (3); DSR / FWER: GBPJPY 0.364 / 0.205, HK 0.861 / 0.108, JPN rs 0.124 / 0.268, XAU rs 0.998 / 0.0003 -- every DSR and
FWER verdict the same as the unreduced D076 run. Its seed stability was not tested (T3 shows the reduced 6-dimensional
observation matrices are the unstable case).

### 4Z.2 Convergence tests T5 / T6 (owner 2026-11-02: "Yes, run the convergence test in parallel")

T5 (Step 01 feature ONC, n_init 50 and 100, seeds 0-4): the cluster COUNT converges -- 2 clusters on every seed of every
edge at both n_init (was 2-26 at n_init 10) -- but the composition does not: normalised partition VI vs seed 0 up to
0.18-0.21 at n_init 50 and 0.13-0.21 at n_init 100 (pre-registered bar 0.10; HK 4 of 5 seeds identical, the others 2-3
distinct 2-cluster splits). Silhouette q of the returned splits still differs by seed (e.g. JPN vc 2.76 / 2.87 / 4.86 at
n_init 100). Neither n_init meets the pre-registered rule. Runtime per run: ~85 s (50), ~180 s (100).

T6 (trial ONC on the p.55 PCA-reduced X): n_init 10 is unstable (E[K] GBPJPY 5-8, HK 2-13, JPN rs 2-18, XAU rs 2-8;
verdicts flip across seeds on JPN rs FWER and XAU rs DSR); n_init 50: E[K] = 2 on every seed of every edge and every
DSR / FWER verdict identical across seeds -- GBPJPY DSR 0.65-0.78 / FWER 0.088 fail, HK 0.85-0.87 / 0.108 fail, JPN rs
0.92-0.95 / 0.038 (DSR fail, FWER PASS), XAU rs 0.999 / 0.0002 pass. JPN rs FWER here differs from the unreduced D076
run at n_init 10 (E[K] 3, FWER 0.057 fail) -> T7 (unreduced at n_init 50) pre-registered and running.

**T7 (unreduced D076 trial ONC, n_init 50, seeds 0-4; 20/20 jobs rc 0, ~27-37 min each).** Identical to T2 / the D076
verify run at n_init 10 on every seed: E[K] GBPJPY 4, HK 2, JPN rs 3, XAU rs 3; DSR 0.3705 / 0.7383 / 0.7066 /
0.9982-0.9984; FWER alpha_K 0.168 / 0.1075 / 0.0568 / 0.0002. Verdicts unchanged (only XAU rs passes DSR and FWER).
Reading rule met: n_init does not matter for the unreduced trial ONC. The JPN rs FWER pass in T6 comes from the p.55
PCA reduction (which gives E[K] 2 everywhere and is seed-unstable at n_init 10), not from search convergence.

Owner 2026-11-02: "Approved 1 and 2, run the selection test in parallel" -> 1: trial ONC stays D076 unreduced, n_init 10
(no p.55 reduction); 2: Step 01 ONC n_init 10 -> 50 (STEP01_ONC_N_INIT, shared/features/clustered_feature_importance.py).

### 4Z.3 T8 -- Step 01 rule-B selection across ONC seeds (n_init 50; trees/MDA seed 0; 30/30 jobs rc 0, ~9 min each)

Every partition equals the T5 n_init-50 partition at that seed; OOS AUC is identical across seeds (the model uses all
candidates; the partition only changes which clusters rule B keeps). Kept features by ONC seed 0-4:

| Edge | seed 0 | 1 | 2 | 3 | 4 | stable |
|---|---|---|---|---|---|---|
| GBPJPY dailyrangecompletionfade | 122 | 121 | 121 | 121 | 155 | no (s1-3 drop RSI_50; s4 keeps both clusters) |
| HK drawdownhazardcontinuation | 155 | 155 | 155 | 155 | 155 | yes (both clusters kept every seed) |
| JPN rangestructure | 155 | 155 | 155 | 155 | 155 | yes (both kept) |
| JPN volatilitycarry | 126 | 30 | 30 | 126 | 28 | no (Jaccard vs s0 down to 0.08) |
| XAUUSD rangestructure | 155 | 155 | 155 | 155 | 155 | yes (both kept) |
| XAUUSD volatilitycarry | 111 | 28 | 155 | 155 | 28 | no (Jaccard vs s0 down to 0.25) |

Reading rule NOT met: 3/6 edges change their kept set with the ONC seed, two of them drastically. The three "stable"
edges are stable only because rule B keeps both clusters whatever the split. Diagnosis (T5 q per seed): the silhouette
quality q of the returned 2-cluster partition differs across seeds at n_init 50 AND 100 (e.g. JPN volatilitycarry 2.76-2.97
at 50, 2.76-4.86 at 100; XAUUSD volatilitycarry 2.57-3.00 at 50, 2.68-4.86 at 100), so ONC's own objective (max q,
Snippet 4.1) is not reached even at 100 initialisations: a search-convergence problem, not a book-method ambiguity.
Reported to the owner with alternatives; none chosen in advance.

Owner 2026-11-02: "Approved A, run T9 in parallel" (A = run ONC to its own optimum: best-q partition over S restarts).

### 4Z.4 T9 -- best-of-S restarts (n_init 50 per restart; logs/mlam_ch4_tests_20261102/t9, t9_stage1.json, t8/)

Stage 1 (ONC seeds 0-59, 330 new runs rc 0). Share of disjoint seed blocks whose best-q partition is the 60-seed
best-q partition:

| Edge | max q (60 seeds) | seeds reaching it | S=5 | S=10 | S=20 |
|---|---|---|---|---|---|
| GBPJPY dailyrangecompletionfade | 3.513 | 3/60 | 0.25 | 0.50 | 1.00 |
| HK drawdownhazardcontinuation | 3.283 | 2/60 | 0.17 | 0.33 | 0.33 |
| JPN rangestructure | 3.184 | 1/60 | 0.08 | 0.17 | 0.33 |
| XAUUSD rangestructure | 3.457 | 13/60 | 0.67 | 1.00 | 1.00 |
| JPN volatilitycarry | 4.857 | 18/60 | 0.83 | 0.83 | 1.00 |
| XAUUSD volatilitycarry | 4.857 | 15/60 | 0.67 | 1.00 | 1.00 |

HK and JPN rs have near-tied optima (JPN rs 3.184 / 3.146 / 3.081), so the partition does not converge by S=20.

Stage 2 (rule-B selection on every distinct block-winning partition at S=10 and S=20; 8 new runs rc 0 + T8 runs):
GBPJPY 155 on both winners; HK 155 on both; JPN rs 155 on all three; XAU rs 155 (one winner); XAU vc 32 (one winner);
JPN vc 123 on the S=20 winner, 28 on the S=10 non-converged winner (seed 4, q 2.97). At S=20 every block on every edge
gives the same kept feature set; at S=10 JPN vc does not.

Reading rule: the kept-set half is met at S=20 on all 6 edges; the partition half is met at S=20 on 4/6 (HK and JPN rs
keep all 155 features on every near-tied partition, so their non-convergence does not change selection). Reported to the
owner with S=20 as the proposal. Note: at the max-q partition the volatilitycarry edges keep 123 (JPN) and 32 (XAU)
features vs 126 / 111 under the current n_init 10 seed-0 run; stored Step 01 outputs change at the RECERT.

### 4Z.5 Decision -- Ch.4 CLOSED (owner 2026-11-03: "Approved, close Ch.4, commit and push, then MLAM Ch.5")

D077: Step 01 feature ONC = onc_best_of_restarts (20 restarts x n_init 50, max q, ties to the lowest seed; CFI v2;
unit test OncBestOfRestartsTest; wiring check logs/mlam_ch4_tests_20261102/d077_verify.json reproduces the T9 seeds
0-19 winner on XAUUSD volatilitycarry). Trial ONC stays D076 (unreduced, n_init 10). Register / backlog D077, standing
rule ONC_SEARCH_CONVERGENCE_D077, runbook Rev 36 (Section 6A Step 01, Appendix 53). Stored Step 01 outputs re-run at
the RECERT.

## 4ZA. MLAM Ch.5 -- Financial labels audit (2026-11-03, CLOSED 2026-11-03)

Book: MLAM pdf 71-79 = printed pp.65-73. Tests pre-registered in logs/mlam_ch5_tests_20261103/PREREGISTRATION.md;
driver shared/research/mlam_ch5_tests.py.

| § / snippet (page) | Book | Quantara | Status |
|---|---|---|---|
| 5.1 (p.65) | labels define the task; try alternative label definitions | one label family: triple-barrier meta-label per thesis | matches (alternatives belong to Gate 0 thesis design) |
| 5.2 fixed horizon (pp.65-66) | avoid; time-bar returns are heteroscedastic, a constant threshold transfers intraday seasonality to the labels; fixes: tick/volume bars or volatility-standardised returns | not used; barriers scaled by Event_Vol (EWM of H-hour returns, span 100-200), which does not follow the intraday cycle | **T2 finding** below |
| 5.3 triple barrier (pp.66-67) | PT / SL / vertical; vertical label 0 or sgn(r); h "defined naturally" | vertical = sign (AFML Ch.3 decision 1); h searched over the 405-cell grid and paid for (D050-A) | matches (h searched, not natural: covered by D050-A) |
| 5.4 trend scanning, Snippets 5.1-5.2 (pp.67-69) | label = sign of the max-abs-t linear trend over look-forward L; t1 = farthest observation used | not used | **T1** transcription replicated; available as a label for future theses (Gate 0); not a meta-label replacement |
| 5.5 meta-labeling (pp.69-70) | secondary model on primary outcomes, loss 0 / gain 1; trades recall for precision | int(label_r > 0) on side-specialised rule primaries | matches; F1 finding already D028 |
| 5.5.1 bet size by expected SR (p.70) | symmetric payoff, z = (p - 1/2)/sqrt(p(1-p)), m = 2Z(z) - 1 | same formula on the payoff-aware p_eff (D007, asymmetric PT2/SL1) | = AFML 10.3; D007 kept (MLAM states the symmetric-payoff assumption explicitly) |
| 5.5.2 ensemble bet sizing (pp.70-71) | n classifiers' 0/1 votes, t = (p_hat - 1/2)/sqrt(p_hat(1 - p_hat)) sqrt(n), m = 2 t_{n-1}(t) - 1 | not used; Step 02 is a 1,000-tree bag whose averaged probability is calibrated and sized | new in MLAM; same p = 1/2 null as 10.3 (5/6 edges would never bet, s.4J); candidate for the deferred Ch.10 OOS sizing benchmark |
| 5.6 Snippet 5.3 (pp.71-72) | trend-scanning example | T1 | replicated |
| Exercises 5.1-5.5 (p.73) | label distribution by hour; standardised returns; TBM; trend scanning; meta-labeling pipeline | Ex.1-2 = T2; Ex.5 = our pipeline | -- |

**T1 (trend scanning, seeds 0-99):** median agreement of the label with the true deterministic trend over the chosen
window 0.89 (10th pct 0.78), median 8.5 label runs per 100-point series, t1 after t0 always: reading rule met; the
transcription behaves as Figure 5.1 describes. No pipeline change.

**T2 (label seasonality, eligible rows, 6 four-hour UTC blocks):**

| Edge | target vs block p / V | barrier vs block p / V | hour-dependent (p < .01 and V >= .10) |
|---|---|---|---|
| GBPJPY dailyrangecompletionfade | .004 / .065 | .026 / .050 | no |
| HK drawdownhazardcontinuation | .78 / .020 | .52 / .029 | no |
| JPN rangestructure | .26 / .038 | .004 / .054 | no |
| JPN volatilitycarry | .51 / .030 | .58 / .030 | no |
| XAUUSD rangestructure | .003 / .065 | 3e-28 / .133 | **yes** |
| XAUUSD volatilitycarry | 7e-05 / .076 | 1e-34 / .142 | **yes** |

XAUUSD: vertical exits 28-32% of entries in 00-12 UTC but 53-56% in 16-20 UTC; PT share 27% -> 17%; median holding
5-9 h (04-12 UTC) vs 16 h (16-24 UTC). The same Event_Vol barrier means a different event depending on the hour --
exactly the p.66 mechanism. The other four edges' vertical shares are 1-8% (barriers rarely outlast the horizon), so the
seasonality barely shows. Reported to the owner with options.

**T3 (owner option A, 2026-11-03): intraday-seasonal barriers on both XAUUSD edges.** Event_Vol x f_D(h0), the
predicted-variance ratio of the H clock hours from the entry hour (hourly squared-return profile over the prior 60
trading days, causal; `shared/research/mlam_ch5_seasonal_lab.py`); schema pinned at 63f4a570 as the 2026-10-31 baseline;
full 405-cell Gate A + Gate C (broker mode, 5700 ms), book-N PBO; control parity PASS (unadjusted). Run 1 died at Gate C
on the unpinned schema (records in logs/mlam_ch5_tests_20261103/run1_failed_gatec/; its Gate A stamps 20261103T102115Z /
102146Z are extra, unused files in the edge artifacts). Results `t3_results.json`:

| Edge | winner base -> seasonal | compound velocity | exec mean R | PBO / Prob[OOS<=0] | exit-reason vs entry block V |
|---|---|---|---|---|---|
| XAUUSD rs | H8_E200_V16_PT2_SL1 -> same | .00623 -> .00544 | .146 -> .132 | .073/.043 -> .164/.046 | .140 -> .146 |
| XAUUSD vc | H8_E200_V16_PT2_SL1 -> H8_E150_V24_PT1.5_SL1 | .00264 -> .00413 | .096 -> .100 | .422/.155 -> .304/.091 | .143 -> .107 |

Same cell (baseline winner under seasonal barriers): vc exec mean R .096 -> .089, V .143 unchanged; rs as above.
Exit shares by entry block (seasonal winners): rs verticals 27 % (00 UTC) .. 53 % (12 UTC) -- the hour pattern moved
(baseline peak was 16 UTC, 64 %) but did not shrink; vc verticals 15-36 %, still p 4e-18.
Reading rule: seasonal adopted only if V < .10 AND velocity >= baseline AND PBO/Prob < .5. **rs fails V and velocity;
vc fails V** (velocity rises only through a different cell found by 405 more trials; on the same cell it falls).
**T3 verdict: no change** -- the hour-of-day factor shifts the hour dependence instead of removing it and does not
improve the economics. The 810 T3 cells are trials under D050-A (logged; they enter an edge's DSR only if adopted).

**Ch.5 CLOSED (owner 2026-11-03: "Approved, close Ch.5, commit and push, then MLAM Ch.6"):** no pipeline change.
(1) fixed-horizon labels not used; triple-barrier labels unchanged, XAUUSD hour dependence recorded as a known label
property; (2) trend scanning available as a primary label for new theses at Gate 0; (3) meta-labels and the
expected-SR bet size unchanged (D007 payoff-aware p kept); (4) ensemble bet sizing deferred to the Ch.10 OOS sizing
benchmark. Runbook Revision 37 (Appendix 54, editor docs/tools/edit_runbook_rev37.py).


## 4ZB. MLAM Ch.6 -- Feature importance analysis audit (2026-11-03, CLOSED 2026-11-03)

Book: MLAM pdf 80-97 = printed pp.74-91. Most of the chapter was audited and implemented with AFML Ch.8 (s.4H, D038-D045)
and Ch.4 (D077); this audit checks the rest. Tests pre-registered in logs/mlam_ch6_tests_20261103/PREREGISTRATION.md;
driver shared/research/mlam_ch6_tests.py.

| § / snippet (page) | Book | Quantara | Status |
|---|---|---|---|
| 6.1 (p.74) | isolate the important variables first (any functional form), then fit a specification | Step 01 feature importance before any backtest | matches |
| 6.2 p-values (pp.75-77) | four caveats; do not rely on in-sample p-values | no regression p-values used for features; rule B is a CLT test on OOS clustered MDA (p.79) | matches |
| 6.3.1 / Snippet 6.2 MDI (pp.77-80) | per-tree MDI, max_features=1, zeros -> NaN, CLT std, normalised | cfi.mdi / Step 01 clustered MDI identical (reported, not used for selection: ~= size share, s.4H.2) | matches |
| 6.3.2 / Snippet 6.3 MDA (pp.81-82) | k-fold, log loss, shuffle one column, imp = (scr0 - scr1)/(-scr1), CLT | same formula; purged + embargoed CV (D039), sequential bootstrap, 5 shuffles averaged (D045), sample-weighted loss | matches (s.4H row 8.3.2 'raw difference' was the pre-D044 Step 01; current code divides by -scr1) |
| 6.4 PWA / NegAL (pp.82-83) | log loss preferred over accuracy; PWA an easier-to-read alternative | weighted log loss | **T2** (Ex.4) |
| 6.5.1 orthogonalisation (p.84) | PCA then MDI/MDA; three caveats | PCA weighted-Kendall diagnostic tool kept (AFML 8.4.2) | matches |
| 6.5.2 Step 1 (p.85) | ONC on a metric projection (correlation or VI); residual features when silhouettes are low | VI + ONC best-of-20 (D077); no residuals (D042, pre-D077 partitions) | **T3** (D042 re-check) |
| 6.5.2 Step 2 / Snippets 6.4-6.5 (pp.86-87) | clustered MDI = sum of members; clustered MDA = shuffle the whole cluster | identical | matches |
| 6.6 / Snippets 6.6-6.8 (pp.87-90) | ONC on the (ideally denoised + detoned) correlation; noise cluster ~0 MDA | book-exact replication passed 2026-10-24 (s.4H.1); Step 01 uses VI (Ex.5) | **T1** (Ex.3 + Ex.5 through the production path) |
| 6.7 (pp.90-91) | apply feature importance to every labelling method; the strongest feature/label combination points to the theory | importance only on the edge's own meta-label | Gate 0 thesis design note (no pipeline change proposed) |
| Exercises 1-2 (p.91) | Bayes: P(H1 given positive) is precision, not 1 - alpha or power | Ex.2: alpha .01, power 1, prior .001 -> precision .001/(.001 + .00999) = .091; two independent positives -> .909 | answered; = MLAM Ch.8 / AFML Ch.14 FWER logic already in use |

Results (logs/mlam_ch6_tests_20261103/; D077 partition held fixed per edge; rule B; cells = clusters as n_features (t)):

**T1 (Ex.3 + Ex.5, production Step 01 on Snippet 6.1 data):** VI + best-of-20 ONC recovers the 6-group truth exactly at
sigmaStd 0.1 and 0 (partition VI 0). Rule B drops noise (t 1.35) but also 3 of the 5 signal clusters (t 1.08-1.15; kept
t 1.91, 2.39): clustered MDA 0.007-0.039 vs the book-exact 0.14-0.23, OOS AUC 0.886. **FAIL** by the pre-registered
rule: Step 01's importance stage has little power even when the answer is known. Ex.3 literally (book-exact, sigmaStd 0):
unclustered MDI and MDA still rank the 5 noise features last; MDI concentrates on redundant copies (~.045 each), MDA
signal features 0.027-0.051 (substitution dilutes, does not reorder).
T1m (post hoc: sigmaStd 0.1, importance trees min_weight_fraction_leaf 0 = Snippet 6.8, nothing else changed): clustered
MDA of the signal clusters 0.15-0.23 (= book-exact), all 5 kept (t 3.1-8.6), OOS AUC 0.942 (was 0.886); the noise
cluster also scrapes through rule B (MDA 0.0021, t 1.74 vs line 1.645). D041 (mwfl 0.05, AFML p.98) is what costs Step 01
its power on this data; with mwfl 0 the one-sided 1.645 line admits a near-zero noise cluster at 10 folds.

**T4 (post hoc, owner-approved 2026-11-03, reading rule pre-registered before any result): Step 01 on the 6 edges, D077
partition fixed, D041 (mwfl 0.05) vs Snippet 6.8 (mwfl 0).** The D041 arm reproduces the T2 log-loss kept set on all 6.
| edge | kept D041 / mwfl 0 | cluster t D041 | cluster t mwfl 0 | OOS AUC D041 / 0 | OOS neg log loss D041 / 0 |
|---|---|---|---|---|---|
| GBPJPY | 155 / 37 | 12.7, 9.4 | 5.8, 0.4 | .521 / .483 | -.677 / -.704 |
| HK | 155 / 0 | 3.3, 3.0 | 0.5, 1.6 | .545 / .547 | -.694 / -.768 |
| JPN rs | 155 / 38 | 5.0, 2.4 | 3.9, 1.2 | .518 / .515 | -.684 / -.739 |
| JPN vc | 123 / 0 | 2.6, 0.7 | -2.8, -0.6 | .539 / .521 | -.708 / -.754 |
| XAUUSD rs | 155 / 38 | 8.2, 6.7 | 5.1, 1.0 | .519 / .547 | -.668 / -.705 |
| XAUUSD vc | 32 / 0 | 4.3, 1.5 | 0.0, 1.1 | .507 / .519 | -.686 / -.705 |
mwfl 0 keeps fewer clusters on 5 of 6 (none on 3) and has worse OOS log loss on all 6 (AUC higher on 3, lower on 3).
Pre-registered rule fails on both legs -> **D041 stands.** On real, overlapping, noisy labels the 5% leaf floor (AFML
p.98) regularises the importance trees and sharpens MDA; unregularised trees overfit and their MDA sinks into noise. The
synthetic T1 loss of power is recorded as a known limitation: Step 01 under D041 is conservative on low-noise,
non-overlapping data (it drops weak true signal), not on our data.

**T2 (Ex.4, PWA vs log loss):**
| edge | log loss kept (t) | PWA kept (t) |
|---|---|---|
| GBPJPY | 37 (12.7) + 118 (9.4) = 155 | same, 155 (13.9 / 12.1) |
| HK | 120 (3.3) + 35 (3.0) = 155 | same, 155 (5.9 / 5.7) |
| JPN rs | 38 (5.0) + 117 (2.4) = 155 | same, 155 (7.7 / 4.2) |
| JPN vc | 123 (2.6); 32 (0.7) out = 123 | **none** (0.5 / 0.9) = 0 |
| XAUUSD rs | 38 (8.2) + 117 (6.7) = 155 | same, 155 (9.7 / 9.1) |
| XAUUSD vc | 32 (4.3); 123 (1.5) out = 32 | **both** (5.7 / 2.6) = 155 |
Same kept set on 4 of 6; the two flips are near-line clusters on the two weakest edges (JPN vc removed Ch.12, XAUUSD vc
failed Gate E), in opposite directions. PWA t-stats are higher everywhere. Book p.82 prefers log loss -> no change.

**T3 + T3r (s.6.5.2 residual features; T3r post hoc, cluster order reversed):** every D077 cluster has mean silhouette
0.18-0.27 (all two-cluster partitions).
| edge | raw | residual, ONC order | residual, reversed |
|---|---|---|---|
| GBPJPY | 155 | 37 (118 at t 0.9 out) | 155 (37 t 12.7 residualised) |
| HK | 155 | 120 (35 at t -0.5 out) | 155 (120 t 1.73 residualised) |
| JPN rs | 155 | 38 (117 at t -0.9 out) | 117 (38 at t 0.5 out) |
| JPN vc | 123 | 123 | 0 (123 at t 1.1, 32 at t 1.3) |
| XAUUSD rs | 155 | 155 (117 t 2.9) | 117 (38 at t 1.3 out) |
| XAUUSD vc | 32 | 155 (123 rises 1.5 -> 2.6) | 0 (32 at t -1.8) |
The pre-registered D042 flag is raised (silhouettes < .25 and kept sets differ), but the post hoc order check shows why:
with two clusters the p.85 residualisation keeps whichever cluster comes first and judges the second only on what it
does not share; the two orderings give different kept sets on all 6 edges. Most of the information is shared
between the two clusters, not exclusive to either.

**Ch.6 CLOSED (owner 2026-11-03: "I approve"): no change.** D041 stands (T4); D042 stands (T3/T3r: residualised
selection is an artefact of cluster order); log loss stays the MDA score (T2, p.82); VI + ONC recovers the synthetic truth
exactly (T1). s.6.7 p.91 (importance can change with the labelling method) -> Gate 0 thesis-design note only.


## 4ZC. MLAM Ch.7 -- Portfolio construction, line-by-line pass (2026-11-03, CLOSED 2026-11-03)

Book pages: MLAM Ch.7 = pdf 98-110 = printed pp.92-104 (s.7.1-7.9, Snippets 7.1-7.9, Figures 7.1-7.3, footnotes 22-24,
Exercises 1-5), read line by line, with the Ch.2 functions it calls (Snippets 2.3, 2.7-2.10, pdf 31-50). The chapter was
first reviewed with AFML Ch.16 (s.4P: HRP kept, D010 / D057-D060; NCO tested on our inputs, s.4P.2 T3). This pass checks
every line again and closes what s.4P did not cover.

| s. (page) | Book | Quantara | Status |
|---|---|---|---|
| 7.1 (p.92) | mean-variance solutions concentrated and unstable; three remedies: priors (Black-Litterman), constraints, a better-conditioned inverse (Ledoit-Wolf); signal, not only noise, causes instability | allocator 07 = HRP (no inverse), denoise + conditional detone (D010), long-only by construction | context |
| 7.2 (pp.92-93) | min 1/2 w'Vw s.t. w'a = 1 -> w = V^-1 a / a'V^-1 a; a = 1, V = sigma I -> 1/N; diagonal V -> inverse variance; a = 1 -> min variance; a = mu -> max Sharpe, market portfolio V^-1 mu / 1'V^-1 mu | ch16_allocation_tests opt_port = Snippet 2.10 optPort (same normalisation 1'w); EW, IVP, min-var, max-Sharpe all in the D060 diagnostic | done |
| 7.3 (pp.93-95) | 2x2 eigen-system, det C = 1 - rho^2, C^-1 explodes as abs(rho) -> 1; condition number = abs(lambda max / lambda min) | condition number reported in the D060 diagnostic (4 edges: 1.24-1.60 across alignments, s.4P.2-4P.3) | done |
| 7.4 (p.95) | Markowitz's curse; HRP beats Markowitz out of sample although sub-optimal in sample; HRP "merely" a demonstration | D060: Monte Carlo on our inputs at every universe change; no method beat HRP-current (s.4P.3) | done |
| 7.5 (pp.96-98), Snippets 7.1-7.2, Fig. 7.1, fn 22 | signal-induced instability is structural, independent of N/T, not reduced by more data; clusters raise the condition number (3; 4 for blocks 3+1; 7 at rho .75; 251 at 500 x 500); a single dominant block sets it (Snippet 7.2) | not replicated; our 4-edge matrix is near the identity (no block signal now). A future book with 1 long + 1 short per asset could hold strongly correlated pairs, i.e. this structure | **T1a** |
| 7.6 (p.98) | NCO is a wrapper: agnostic to the frontier member and the constraints | -- | context |
| 7.6.1 (pp.98-99), Snippet 7.3, fn 23 | cluster the correlation matrix (ONC or other); denoise first for large matrices with low T/N; maxNumClusters = N/2 (singletons do not raise the condition number); corr vs abs(corr): same when all correlations >= 0; otherwise try both by Monte Carlo (abs tends to suit long-short) | ch16 tool NCO = Snippet 7.6 (no denoise, maxNumClusters N // 2, n_init 10, corr not abs) | abs(corr) not tried -> **T3** |
| 7.6.2 (p.99), Snippet 7.4 | intracluster min-variance on the denoised cov1; "nothing ... prevents the use of alternative allocation methods"; cov2 = reduced covariance | min-variance only | NCO around other allocators -> **T3**, Ex.3 |
| 7.6.3 (pp.99-100), Snippet 7.5 | intercluster allocation on cov2 (close to diagonal); final = intra x inter | same | done |
| 7.7 (pp.100-102), Snippets 7.6-7.9, Figs 7.2-7.3, fn 24 | Monte Carlo: true (mu0, V0), 1,000 empirical (mu, V) from 1,000 observations, raw and Ledoit-Wolf, Markowitz vs NCO, RMSE vs the true weights; NCO 52.98 % (min-var) and 45.17 % (max-Sharpe) of Markowitz's RMSE; fn 24: repeat on your own (mu, V) | the same design on our inputs is s.4P.1 M2; the book's own figures were never reproduced | **T1b** |
| 7.8 (pp.103-104) | high condition numbers from noise and signal; unstable weights -> transaction costs; NCO combines with BL, shrinkage, constraints; recursive NCO for clusters within clusters; Monte Carlo on your inputs and "apply opportunistically whatever optimization method is best suited" | D060 = this rule (re-run at every universe change) | done |
| 7.9 (p.104) | Exercises 1-5 | none | **T2** |

**Book errata found on this pass (recorded, no effect on Quantara):**
1. p.101 says the DGP is "a stylized version of a fifty securities portfolio, grouped in ten blocks". Snippet 7.7
   sets `nBlocks, bSize = 10, 50`, which is 500 securities (as Ch.2 p.33 says: "ten blocks of size fifty each").
2. p.102 says Ledoit-Wolf shrinkage "helps reduce the RMSE ... around 11.81%". But Figure 7.2 shows shrunk
   Markowitz 8.89E-03 against raw 7.95E-03, an 11.8 % increase. The same paragraph's conclusion ("shrinkage adds no
   value") agrees with the figure, not with that sentence.
3. Fig. 7.2 raw Markowitz min-variance RMSE (7.95E-03) differs from Ch.2 Fig. 2.5 (4.95E-03) for what is printed as
   the same DGP, seed, nObs and optPort. T1b reports which one our replication matches.
4. Snippet 7.3 passes `maxNumClusters=corr0.shape[0]/2` (corr0 is not defined there; a float in Python 3); Snippets
   7.1 / 7.2 use Python 2 print. Ch.2 p.36 says "set minVarPortf=True" for the max-Sharpe run; False is meant.

**Also confirmed in the code:** shared/models/denoise_onc.cluster_kmeans_base caps maxNumClusters at N - 1, which has
no effect at N/2. Its n_facts count (`eigenvalues > e_max`) equals Snippet 2.9's searchsorted formula.

**Tests (logs/mlam_ch7_tests_20261103/PREREGISTRATION.md).**
- **T1a PASS:** condition numbers exact: 3, 4, 7, 251, and 3 for the dominant block.
- **Scope cut:** T1b and T2 (synthetic, ~4 h) were stopped on the owner's instruction ("run only T3") before any result.
  They test the book, not Quantara; s.4P had already run the NCO / Markowitz / HRP comparison on our inputs.

**T3: our 4 edges, daily mark-to-market, T = 1,512.**
- Inputs: condition number 1.28; correlations -0.08 to 0.09.
- NCO clusters on corr: {GBPJPY, JPN}, {HK, XAU}.
- NCO clusters on abs(corr): {GBPJPY, JPN, HK}, {XAU}. HK, the only short, has small negative correlations with the
  two longs, so footnote 23 regroups it.

M2, 1,000 simulations. RMSE vs the method's own weights on the true V (vs the true min-variance weights in brackets):

| method | nObs 260 | nObs T |
|---|---|---|
| HRP-current | 0.0202 (0.0229) | 0.0082 (0.0167) |
| IVP | 0.0185 (0.0220) | 0.0076 (0.0145) |
| Markowitz = CLA long-only | 0.0254 (0.0254) | 0.0100 (0.0100) |
| NCO min-var (= NCO with CLA) | 0.0240 (0.0258) | 0.0106 (0.0155) |
| NCO min-var on abs(corr) | 0.0250 (0.0249) | 0.0097 (0.0098) |
| NCO with IVP | 0.0214 (0.0230) | 0.0096 (0.0166) |

- **No NCO variant has own-RMSE below HRP-current at either nObs.** By the pre-registered rule there is no candidate:
  D060 (HRP) stands. True-weight differences between all methods are at most 2.5 pp.
- abs(corr) brings NCO closest to Markowitz's own target (0.0098 at T). That is the expected direction with a short
  edge in the book, but the correlations are too small for it to matter now.

**Exercises, answered from the text and the results above (not re-simulated):**
1. **Ex.1, detoning in NCO:** detoning removes a market component (Ch.2 s.2.6). The book DGP has independent blocks and
   no market factor, so there is nothing to remove. 07 already detones only when a market component is confirmed
   (D010 (b')).
2. **Ex.2, no cluster structure:** NCO's gain comes from block structure (s.7.5). Our inputs are this case (condition
   number 1.28), and NCO does not beat Markowitz or HRP (T3, s.4P).
3. **Ex.3, CLA in place of minVarPort:** on our inputs every min-variance weight is positive, so CLA long-only equals
   Markowitz and NCO-CLA equals NCO (T3).
4. **Ex.4, size:** p.102 says NCO's advantage widens with N. At N = 4 it has none; D060 re-runs the comparison at every
   universe change, including the planned >= 20-edge book.
5. **Ex.5, maxNumClusters 2:** with N = 4, N/2 = 2, so T3 is this case.

**Ch.7 CLOSED (owner 2026-11-03: "Approved, close Ch.7 without the extra"): no change.** HRP-current stays (D060). The optional extra was declined:
add NCO on abs(corr) and NCO with IVP to the methods the D060 diagnostic compares at each universe change (tool change
only, no allocator change), which would matter once the book holds long and short edges with strong negative
correlations. Book errata 1-4 recorded.

## 4ZD. MLAM Ch.8 -- Testing set overfitting, line-by-line pass (2026-11-03, CLOSED 2026-11-03)

Book pages: MLAM Ch.8 = pdf 111-130 = printed pp.105-124 (s.8.1-8.10, Snippets 8.1-8.4, Figures 8.1-8.6, footnotes
25-26, Exercises 1-5), read line by line again. The chapter was first audited with AFML Ch.14 (s.4N: D054 all-trials DSR
+ FWER / Type II; 07b rewritten). Owner rule (2026-11-03): tests already run are not repeated. This pass ran only
arithmetic checks (seconds) and the existing unit tests (tests/test_afml_ch14_statistics.py, 14 pass).

| s. (page) | Book | Quantara | Status |
|---|---|---|---|
| 8.1 (p.105) | backtests are not controlled experiments; selection bias under multiple testing (SBuMT); hyperfitting = a second selection over researchers' already-selected backtests | trials accumulate (D050-A); 07b's portfolio-level DSR counts only the portfolio choice and is not evidence on its own (s.4N.3) | done |
| 8.2 (pp.105-106), fn 25 | precision = (1 - beta) theta / ((1 - beta) theta + alpha); recall = 1 - beta; gather evidence a strategy exists before backtesting (low theta -> low precision); example theta = 1/99: ~58 positives, ~50 false, FDR 86.09 % | the "evidence first" rule is D075 (Gate 0 theory check before Gate A); arithmetic re-checked: 57.5 positives, FDR 0.8609 | done |
| 8.3 (p.107) | FWER alpha_K = 1 - (1 - alpha)^K; familywise miss beta_K = beta^K; precision / recall under K | FWER and beta_K in 07b / afml_ch14_dsr (D054) | done |
| 8.4 (pp.107-108), fn 26 | SR estimate; Lo (2002) IID-Normal variance; Mertens (2002) (1 - g3 SR + (g4 - 1)/4 SR^2)/T, valid for stationary ergodic returns | z_stat / PSR use Mertens with g4 = 3 for Gaussian (07b kurtosis comment fixed, s.4N) | done |
| 8.5 (pp.108-109) | False Strategy theorem: E[max SR] = sqrt(V)((1 - gamma) Z^-1[1 - 1/K] + gamma Z^-1[1 - 1/(K e)]) | expected_max_sr; test: 3.26 at K = 1,000 (p.111) | done |
| 8.6 (pp.109-113), Snippets 8.1-8.2, Figs 8.1-8.2 | Monte Carlo check of the theorem: unbiased asymptotically, error sd < 0.5 % | not re-simulated: checks the book's theorem, cannot change Quantara (owner rule) | n/a |
| 8.7 (pp.113-114) | DSR = Z[(SR - E max SR) sqrt(T - 1) / sqrt(1 - g3 SR + (g4 - 1)/4 SR^2)] | dsr_report; test reproduces the formula | done |
| 8.7.1 (p.114) | effective trials E[K] = ONC clusters of the trials' correlation (a conservative estimate) | ONC on each edge's 406 Gate C trials (E[K] = 2) (s.4N.1-4N.2) | done |
| 8.7.2 (pp.114-116) | cluster series by min-variance weights (robust: Lopez de Prado & Lewis 2018); aSR_k = SR_k sqrt(Frequency_k); V = V[aSR_k] / Frequency_selected | get_ivp (LdP-Lewis Sherman-Morrison, the default) + the book's annualisation (s.4N.3) | done |
| 8.8-8.8.2 (pp.116-118), Snippet 8.3 | Sidak alpha = 1 - (1 - alpha_K)^(1/K) (Bonferroni approx.); alpha_K = 1 - Z[z]^E[K]; example z 2.4978, alpha 0.0062, alpha_K 0.0608, Normal-returns alpha_K 0.0261 | tests reproduce z and alpha_K; re-checked here: alpha 0.00625, Normal alpha_K 0.02607 | done |
| 8.8.3 (pp.118-120), Snippet 8.4 | power: beta = Z[z_alpha - theta], beta_K = beta^K; example theta 1.9982, beta 0.6913, beta_K 0.0249 | tests reproduce all three | done |
| 8.8.4 (pp.120-122), Figs 8.4-8.6 | alpha / beta trade-off; beta_K = Z[Z^-1[(1 - alpha_K)^(1/K)] - theta]^K falls as K rises | same function | done |
| 8.9 (pp.122-123) | two procedures: DSR (False Strategy theorem) and FWER (Sidak) | both reported; D054 gate at RECERT = DSR > 0.95 AND alpha_K < 0.05 (Level 1) | done |
| 8.10 (p.123) | Exercises 1-5 | see below | answered |

**Exercises (answered, nothing new simulated):**
1. **Ex.1-2, precision and recall vs theta and K:** precision rises with theta, and multiple testing lowers it
   (alpha_K grows with K). The reason is the s.8.2 formula; Ch.6 Ex.1-2 (s.4ZB) computed the same precision logic.
   Recall 1 - beta^K rises with K.
2. **Ex.3, 5 years daily (T = 1,250, 250 per year), best of 10, annualised SR 2, V[aSR] = 1, IID Normal:**
   (a) E[max aSR] = 1.575; (b) PSR[0] = 1.0000; (c) DSR = PSR[1.575] = 0.828. Significant after one trial, not after ten.
3. **Ex.4-5, 1,000 configurations of one strategy -> max SR, E[max SR], DSR, FWER and Type II at the median trial SR:**
   this is s.4N's Level 1 test on each edge's own 406 Gate C configurations (barrier, horizon and EWM grid), with
   beta_K at SR = 1.0 and at the median trial SR (s.4N.1).

**Book errata found on this pass (recorded, no effect):**
1. Snippet 8.1 continues a line with `+ /` (should be `\`).
2. Snippet 8.2 calls an undefined `getDistDSR` (getDistMaxSR is meant) and never uses its own `nSims0`.
3. p.111 says each K is simulated 10,000 times, but Snippet 8.1 passes nSims = 1E3.
4. Snippets 8.3 / 8.4 compute `sr_` in main0 of 8.3 without using it.

**Ch.8 CLOSED (owner 2026-11-03: "Approved, close Ch.8"): no change.** Every method in the chapter is implemented and was tested
on our data in s.4N; the D054 gate (RECERT) stands.

## 4ZE. MLAM Appendix A -- Testing on synthetic data, and Appendix B (2026-11-03, CLOSED 2026-11-03)

Book pages: App. A = pdf 131-133 = printed pp.125-127 (Figure A.1); App. B "Proof of the False Strategy theorem" =
pp.128-129. Both read line by line. App. A is a survey with no method of its own; each branch of Figure A.1 is mapped to
where Quantara uses it.

| Figure A.1 branch (pp.125-127) | Book | Quantara | Status |
|---|---|---|---|
| deterministic: combinatorial CV (leave k of N folds out -> (k/N) C(N, N-k) paths) | harder to overfit than one path (AFML Ch.12) | CPCV paths, D052 (code done, runs at the retrain) | scheduled |
| deterministic: cross-validation (one fold out) | -- | purged, embargoed k-fold in Steps 01-03 (AFML Ch.7, s.4G) | done |
| deterministic: jackknife; permutation test (all combinations) | -- | no jackknife; permutation-type tests: alpha-independence lab (circular-shift permutation, diagnostic) and the Gate 0 same-month random-entry baseline (D075) | covered where needed |
| random: subsample (without replacement) | weaker assumptions, impractical on small samples | not used (samples are small) | n/a |
| random: bootstrap (rows or blocks) | blocks keep serial dependence | circular block bootstrap in the D060 allocation Monte Carlo (M1 G1); row bootstrap in 07's market-component test | done |
| random: sequential bootstrap | adjusts draw probabilities online for overlap (AFML Ch.4) | Step 01 / model training (s.4D) | done |
| parametric Monte Carlo (regime-switching, EM transition matrix) | matches the observed statistics | Gaussian Monte Carlo in D060 (M1 G2, M2); AFML Ch.13 synthetic O-U trading-rule test (D053, s.4M); allocator Step 11 Monte Carlo stress gate; HMM regime states exist as features only | done (no regime-switching generator) |
| nonparametric Monte Carlo (VAE, SOM, GAN) | only if the sample is representative enough to learn the data-generating process, "otherwise a parametric Monte Carlo approach should be preferred" | not used: ~4 years of price-only data per asset; the book's own condition points to parametric | n/a (book's caveat applies) |

**Appendix B (pp.128-129).** The theorem is the Gumbel limit of the maximum of K iid Gaussians (Fisher-Tippett-Gnedenko;
normalising constants alpha = Z^-1[1 - 1/K], beta = Z^-1[1 - e^-1/K] - alpha). Its expectation gives eq. (3)-(4),
stated "where K >> 1". Our afml_ch14_dsr.expected_max_sr is eq. (4) exactly. **Finding (checked here, arithmetic only):**
our Level 1 DSR uses E[K] = 2 on every edge, which is far from K >> 1. Against the exact expectation of the maximum of K
standard normals (numerical integral):

| K | 2 | 3 | 4 | 5 | 10 | 50 | 1000 |
|---|---|---|---|---|---|---|---|
| eq. (3) / exact - 1 | **-7.9 %** | +0.8 % | +2.2 % | +2.5 % | +2.3 % | +1.2 % | +0.4 % |

At K = 2, eq. (3) gives 0.520 against the exact 1/sqrt(pi) = 0.564. SR* is therefore 7.9 % too low, which makes the DSR
slightly optimistic. From K = 3 on, eq. (3) is slightly conservative. Recomputed from the stored records (same V,
selected series, T, skew, kurtosis):

| edge | DSR stored (eq. 3) | DSR with exact E[max] | D054 verdict |
|---|---|---|---|
| XAUUSD rangestructure_long | 0.9983 | 0.9979 | pass, unchanged |
| JPN rangestructure_long | 0.9379 | 0.9321 | fail, unchanged |
| GBPJPY dailyrangecompletionfade_long | 0.6851 | 0.6473 | fail, unchanged |
| HK drawdownhazardcontinuation_short | 0.6498 | 0.6108 | fail, unchanged |

No verdict changes. FWER and Type II do not use eq. (3).

**App. A CLOSED (owner 2026-11-03: "Approved, close Appendix A"): no change.** The book formula eq. (3)-(4) stays; the
K = 2 caveat is recorded. With App. A, the AFML (Ch.1-22) and MLAM (Ch.1-8, App. A-B) walkthrough is complete. Next:
RECERT and the retrain.

**Runbook Revision 42 (2026-11-03, owner request "go through the Quantara_Runbook.docx and make sure it is up to date").**
Every section was checked against the register (D001-D077), the backlog (37 items) and this file. The body is now
current for the MLAM-era decisions; changes are listed in runbook Appendix 59:
- Section 6A STATE, the status line and Step 10B wording.
- Section 2: two owner rules.
- Steps 4, 10D, 10J and 10L: the MLAM Ch.5-7 and App. B findings.
- Step 13 and KEY RULES: the D075 checks.
- The Section 16C RECERT sequence: D074, D075, D076 and D077 added (13 steps).
- Appendix C: the RECERT go-live item.

The ledger gained RUNBOOK_v5.3_ADDENDUM_2026-11-03_MLAM_CH2_APPA_REV42.

## 4ZF. RECERT (2026-11-03/04) and the edge-level gate architecture (runbook Revision 43)

Full record: logs/recert_20261103/RECERT_LOG.md.

**RECERT steps 1-5.**
- Step 1: re-selection on the D074 grid, reusing the 2026-10-31 full replays (the GBPJPY feature-window correction was
  found at the export).
- Steps 2-3: re-export, Task 2A exact, Steps 01-09 complete. Gate D v3: GBPJPY, HK and XAUUSD pass; JPN fails.
- Step 5: D075 fails on all 4 at the locked geometry.
- Owner 2026-11-04: "remove all four"; config/edge_universe.json has 0 edges.

**Owner decisions (2026-11-04).**

Principle: edge-level gates decide entry to edge_universe and never depend on the allocator or on the Step 04
threshold, because the Step 04 threshold "is never the threshold that goes live". The owner's argument: "for an edge to
go live ... it has passed all the necessary checks or gates in its edge level phase not going to the future
(allocator) and then coming back".

- D075 amended:
  - the model-filtered drift test is deleted;
  - Gate 0 and the post-Gate C re-check both use one-sided 1.645;
  - RECERT edges re-run both checks before any gate that removes edges.
- Strategy risk (D055, AFML p.216) and Gate E (D023, AFML p.8) stay after the allocator as deployment gates.
- D079: Gate C stress is a KILL gate.
- D080: CV leakage LEAK_SUSPECT is a stop.
- D081: the label audit is threshold-free at the edge level.
- D078: Gate D v4 = D1 (F1 act/pass, ISO-week block bootstrap, 5th percentile > 0) AND D2 (cross-fitted calibrated log
  loss vs base rate, one-sided 1.645). AFML s.3.7 pp.52-53, s.14.8 pp.206-207, s.9.4 p.134. AUC becomes a diagnostic.
  The cross-fitted calibration is for D2 only; the live calibrator is unchanged.

**Found at the first Gate D v4 run (owner decision pending, backlog D078).**
- (1) D2 weighting. As first coded, D2 scored an unweighted calibrator with weighted loss and base rate, which is
  inconsistent. The two consistent versions give opposite verdicts on the 4 removed edges:
  - all unweighted: all fail (gain -0.0003..+0.0013);
  - all AFML-weighted: all pass (gain +0.007..+0.037).
- (2) D1 decision point. p > 0.5 acts on 74-100 % of events. On 2:1 geometry the return-attribution weights correlate
  with the label (winners carry 1.5-1.9x the weight), so the balanced classifier's neutral point is about 0.60-0.65.
  D1 failed on all 4 edges.

Runbook Revision 43 (Appendix 60). Canonical rules: docs/mldp/rules/THEORY_CHECK_RULE_D075.md,
docs/mldp/rules/GATE_D_V4_D078.md.

**Later on 2026-11-04 (runbook Revision 44).**
- The 4 edge folders were archived to Desktop\Quantara_edge_archive_2026-11-04 (owner: "Now").
- The Phase D provider tests are left failing (owner: the edges are killed).
- D1 decision point approved: act when the cross-fitted calibrated P(win) > b.
- D2 weighting: the owner asked what the books say. AFML is return-attribution weighted throughout -- s.4.6 pp.68-69,
  Snippet 7.4 cvScore (weighted log loss), s.9.4 pp.134-135 ("the right ML performance metric ... for financial
  applications"), s.10.3 sizes from the classifier's own probability. That reverses my earlier "unweighted"
  recommendation. Pending: weighted D2, and whether the live calibrator is fitted weighted too.

**Settled 2026-11-04 (runbook Revision 45).** D2 weighted throughout: "approved". Live calibrator: "fit it weighted
too".
- Gate D v4.1 is in force: a weighted cross-fitted calibrator; D1 acts when the calibrated P(win) > the weighted base
  rate; D2 is the weighted log loss.
- D082: Step 04's live sigmoid is fitted with the AFML return-attribution weights.
- The backlog item D078 is closed.

**Edge search, 2026-11-04 (runbook Revision 46).** NASDAQ campaign (docs/mldp/campaign/NASDAQ_2026-11/).
- Gate 0 records: D075 rule C amended (the card pre-registers its asset universe), the tiered drift bar (one-sided 90 %
  when every counterfactual passes at 95 %), D083 (H1_V1 cell), D084 (mechanism features, code pending), the tool
  shared/research/gate0/gate0_tool.py. T1 and T2 failed; T1b SHORT passed (post-hoc universe and bar, owner-ordered).
- D085: the >= 2000 model-row floor is removed (owner: "we have gates that will kill it regardless ... any edge with its
  own thesis floor gets to go through the pipeline"). Trigger: closehedgingflow_short's ceiling is 451 events. No code
  enforced the floor. The books set no minimum; the statistical gates judge a thin sample.

## 5. Open questions for the owner

None at the moment for Ch.1 beyond reviewing the thesis cards.

## 6. How to resume on a new machine

1. Read this file, then `docs/mldp/QUANTARA_MLDP_DOCTRINE.md`, `config/mldp_deviation_register.json`,
   and `config/us500_edge_search_ledger.json` key `RUNBOOK_v5.3_2026-10-22` (= runbook docx 6A).
2. Raw ticks (`assets/*/raw/ticks/`) and holdout ticks are not in git; copy them from the old
   machine or re-download (`auto_downloader.py` / `shared/data/download_holdout_ticks.py`).
3. Continue at the first item in §4 not marked DONE, then the next chapter in §3.

## Backlog: information sources beyond price (owner question 2026-10-26, NOT started)

Owner asked whether real drivers (macro etc.) could give the meta-model real information, with research = live.
Sources read: AFML s.2.2 pp.23-25 (four data types; macro variables are "fundamental data": point-in-time release dates
and revisions; "extremely regularized and low frequency ... unlikely ... much value left", "may be useful in combination
with other data types"; market data (order flow, FIX) and alternative (primary) data called most promising); MLAM s.1.2
pp.3-4 (theory first; ML finds the variables, a theory binds them); Lipton & Lopez de Prado 2020 "Three Quant Lessons
from COVID-19" (SSRN 3580185: more nowcasting with real-time data, theories not trading rules, regime-specific strategies
weighted by nowcast regime probabilities; calls macroeconomic theories "useless (if not downright dangerous)" citing
Lipton 2016 -- about macro theory, not macro data); Causal Factor Investing (2023, open access: phenomenological ->
theoretical -> falsification steps; ML for discovering variables; specification from a causal graph, never condition on
colliders). Diagnosis: almost all Quantara features are transforms of the same asset's own price (descendants of price),
consistent with Ch.8 MDA (mostly noise) and Ch.10 (only price location / path informative).
Candidate sources ranked by research = live parity and cost (feasibility unverified):
1. Driver prices from the same broker (USDJPY, EURUSD, US500, NASDAQ, USOIL, XAUUSD already; Dukascopy also has a US
   dollar index CFD, a VIX CFD, US T-bond / Bund / Gilt CFDs) -- history from Dukascopy, live from the same MT5 terminal if
   Moneta quotes the symbol (EURUSD, USDJPY appear in mt5_broker_profile.json; others unchecked).
2. Economic-calendar timing (scheduled release times, importance) -- known in advance, parity high; surprise values need
   point-in-time first-release + consensus history (AFML p.24 revisions trap).
3. Daily rates / yields, CFTC positioning -- free, low frequency, usable only lagged to their release time (regime features).
4. Real exchange volume / order flow (CME futures) -- AFML's preferred market data; paid history + live feed.
5. News / unstructured -- paid, hardest parity.
Process if started: causal hypothesis per edge first, point-in-time features, MLAM clustered MDA before any backtest,
trials counted. Owner decides when (chapters stay in order).

## Cross-asset feature experiment (owner go-ahead 2026-10-26; pre-registered before any result)

Purpose (owner): decide whether we continue with the current feature schema or add cross-asset driver features.
Restore point: commit 98a7824a on v3 (pushed) = everything before this experiment. JPN research datasets re-exported
on the current bars first (logs/jpn_reexport_20261026/: export -> Task 2A -> Steps 02-04 with selected features kept
locked; Step 01 re-selection stays in the retrain batch).
- Features (`shared/research/cross_asset_features.py`): for each of the 9 other assets, as-of strictly before the
  decision time on each driver's own research bars: z-returns over 1 / 4 / 24 h, hours since its last bar, vol ratio
  120 h / 480 h, 480 h hourly-return correlation with the target, target-minus-co-move residual over 4 h; plus a mean
  24 h z of the equity indices (64 features). Research bars only (holdout never read). Common window: rows <= the
  earliest research end (XAUUSD 2026-04-07 22:54 UTC).
- Test (`shared/research/cross_asset_feature_test.py`): AFML s.6.4 bagged trees (1000, avgU, min weight fraction 5 %),
  uniqueness weights, purged + embargoed 10-fold; baseline vs baseline + cross-asset (and cross-asset alone, diagnostic).
  Entry mode: all resolved events, label int(label_r > 0), baseline = the edge's selected features, loss = weighted log
  loss, bootstrap over calendar months. In-trade mode: live-approved trades only, target = remaining mid R, baseline =
  Ch.10 trade state, loss = weighted MSE, bootstrap over trades.
- GO rule: candidate beats baseline with the 95 % CI of the pooled OOS loss gain above zero. Decision rule (owner's
  question): cross-asset features go into the schema (one uniform change, owner approval) only if entry GO on a majority
  of the 6 edges, or in-trade GO followed by a s.10.6 step-3 gain; otherwise the current schema stays and the code stays
  as research tools. One trial per edge per mode.
**Results (2026-10-26).** Reports in each edge's `artifacts/feature_importance_audit/cross_asset_feature_test_*.json`;
12 trials logged. JPN re-export: both JPN edges re-exported on the cleaned bars, Task 2A re-labelled, Steps 01-04
re-run (Step 02's provenance guard required Step 01 first; JPN rs keeps 157 features, JPN vc 38); new OOF AUC JPN rs
0.510 (was 0.525), JPN vc 0.553 (was 0.563).
| edge | entry AUC base / +cross / cross only | entry log-loss gain (95 % CI) | in-trade R2 base / +cross | in-trade MSE gain (95 % CI) |
|---|---|---|---|---|
| GBPJPY | 0.556 / 0.557 / 0.517 | +0.0021 (-0.0019..+0.0056) | 2.0 % / 1.2 % | -0.013 (-0.023..-0.003) |
| HK | 0.555 / 0.561 / 0.521 | +0.0017 (-0.0011..+0.0045) | 1.0 % / 0.6 % | -0.008 (-0.021..+0.005) |
| JPN rs | 0.555 / 0.557 / 0.538 | +0.0012 (-0.0013..+0.0037) | 1.1 % / 0.7 % | -0.006 (-0.019..+0.006) |
| JPN vc | 0.558 / 0.560 / 0.532 | +0.0034 (-0.0002..+0.0067) | -6.5 % / 1.2 % | +0.143 (+0.021..+0.269) GO* |
| XAUUSD rs | 0.557 / 0.545 / 0.496 | -0.0016 (-0.0047..+0.0014) | 0.7 % / -0.6 % | -0.015 (-0.025..-0.004) |
| XAUUSD vc | 0.502 / 0.499 / 0.506 | -0.0001 (-0.0035..+0.0032) | -0.3 % / -0.2 % | +0.001 (-0.009..+0.010) |
Entry: GO on 0 of 6 (small positive gains on the 4 non-XAUUSD edges, none significant; AUC changes -0.012..+0.006).
In-trade: worse on 4, GO only on JPN vc*, which is not reliable: after the JPN re-calibration the old live threshold
(0.42) approves only 173 trades (calibrated p max 0.425), 5,958 rows instead of 40,675, and the "gain" comes from the
state-only baseline over-fitting that tiny sample (R2 -6.5 %), not from strong cross-asset information (+1.2 %).
Verdict under the pre-registered decision rule: the current feature schema stays; cross-asset code stays as research
tools. JPN vc in-trade can be re-tested once the allocator re-picks its threshold.

### 4K.1 Owner decisions (2026-10-26) and the all-trials PBO recompute (pre-registered)

| # | Item | Decision |
|---|---|---|
| 1 | PBO with every configuration tried (s.11.6) | APPROVED: first all replayed cells of the 2026-10-17 v5.1 Gate C runs (cheap), then all 405 cells (needs Gate C replays of the rest) |
| 2 | Backtest-first Gates A-C (s.11.4, 11.5 #3) | record as deviation D050 now; decide a theory-first redesign after Ch.12-14 |
| 3 | Single-asset edges (s.11.5 #1) | add a cross-asset consistency flag ("worked on k of n assets tried") to each edge record; judge with the Ch.14 DSR |
| 4 | Storytelling (sin 3) | new edges: thesis card written and pre-registered BEFORE the first backtest; existing cards marked post hoc |
Recompute (`shared/research/pbo_all_trials.py`, same Gate C machinery, stamps = each edge's 2026-10-17 run): candidate sets
recorded (must reproduce the recorded PBO), coverage_only (performance filter removed), all_replayed (every replayed cell,
controls included = the book's N, primary). Pass rule unchanged. One trial per edge. Logs `logs/ch11_pbo_all_trials_20261026/`.
Results (reports `artifacts/feature_importance_audit/ch11_pbo_all_trials.json`; trials logged). The recorded Gate C PBO
was reproduced exactly on all 6 edges.
| edge | replayed cells / 405 | recorded (N, PBO, Prob) | all replayed (N, PBO, Prob) | slope |
|---|---|---|---|---|
| GBPJPY | 278 | 143, 0.454, 0.432 | 278, 0.273, 0.430 | -0.49 |
| HK | 129 | 33, 0.254, 0.282 | 129, 0.093, 0.295 | -0.58 |
| JPN rs | 317 | 272, 0.401, 0.367 | 317, 0.366, 0.367 | -0.41 |
| JPN vc | 212 | 51, 0.507 (FAIL), 0.433 | 212, 0.245, 0.450 | -0.49 |
| XAUUSD rs | 406 | 405, 0.085, 0.058 | 406, 0.085, 0.058 | -0.56 |
| XAUUSD vc | 406 | 393, 0.305, 0.171 | 406, 0.288, 0.171 | -0.38 |
Finding: with the book's N the PBO goes DOWN (all 6 pass; JPN vc 0.507 -> 0.245). Reason: PBO is a rank statistic; adding
the poor configurations fills the bottom of the OOS ranking, so the in-sample winner ranks above the median more often.
Prob[OOS <= 0] (does the chosen configuration lose money out of sample) barely moves (0.06-0.45), so it is the more
robust of the two numbers here. Our positive-only filter was the STRICTER version. The 405-cell replay (Gate-B-negative
cells added) would push PBO further down by the same mechanism, so it adds little information.

**Ch.11 closed (owner 2026-10-26):** the book-N PBO is the gate (D051, gate_c_pbo.py; positive-only PBO reported as a
diagnostic; tests.test_gate_c_pbo_regression passes incl. TestGateCPboAllTrials); 405-cell replay skipped; D050 recorded;
runbook Revision 19 (Step 7B, Appendix 36, editor docs/tools/edit_runbook_rev19.py). Correction: the Gate C replay driver
is in the repo (shared/research/gate_c_runner.py), not only in the scratchpad as I said; the skip stands on the
information argument.

### 4L.1 Owner decisions (2026-10-26) and the CPCV path test (pre-registered)

| # | Item | Decision |
|---|---|---|
| 1 | 405-cell replay (Ch.11) | owner: skipping it deviates from the book's N -> DO IT. Parity check found a ~1e-5 R per trade cost-input drift since 2026-10-17 (HK winner 0.073549 vs 0.073539), so all 405 cells are replayed FRESH in one run per edge (GBPJPY, HK with their 2026-10-17 cell list; JPN after a fresh Gate A on the re-exported data); XAUUSD's 2026-10-17 runs already hold all cells. `shared/research/gate_c_runner.py --all-cells` (new flag), logs `logs/ch11_full405_20261026/` |
| 2 | CPCV paths (s.12.4.2 step 5) | APPROVED: keep paths separate, N = 5 and N = 10, k = 2 |
| 3 | per-path economics | same sizing, the live threshold chosen by the portfolio allocator (live_manifest; not per edge), Moneta executed R, daily Sharpe and mean R |
| 4 | report | path Sharpe mean / spread / worst / share <= 0, against the current single averaged path |
| 5 | caveat | calibrator and thresholds unchanged for this test (fitted on the averaged path) |
| 6 | adoption | owner: switch selection and sizing decisions to the CPCV path distribution, the book's way, WHETHER OR NOT the paths agree (design follows the test; feeds Ch.14 PSR/DSR) |
Test tool: `shared/research/cpcv_path_backtest.py` (re-uses each edge's own Step 03 module for fitting). Logs
`logs/ch12_cpcv_paths_20261026/`. One trial per edge.

**Results (2026-10-26).**
Full 405-cell Gate C replay, book-N PBO (official gate_c_pbo.py; one run per edge; trials logged; reports
`artifacts/feature_importance_audit/ch11_pbo_full405.json`):
| edge | N | PBO | Prob[OOS<=0] | verdict | winner (exec mean R) | positive-only diagnostic PBO |
|---|---|---|---|---|---|---|
| HK | 406 | 0.013 | 0.338 | PASS | H8_E100_V168_PT2_SL1 (+0.074) unchanged | 0.254 (N 33) |
| GBPJPY | 406 | 0.153 | 0.430 | PASS | H6_E100_V168_PT2_SL1 (+0.087) unchanged | 0.456 (N 136) |
| JPN rs (re-exported data, fresh Gate A best gross +0.167) | 406 | 0.349 | 0.400 | PASS | H8_E100_V96_PT2_SL1 (+0.099) unchanged | 0.489 (N 275) |
| JPN vc (re-exported data, fresh Gate A best gross +0.141) | 406 | 0.119 | **0.527** | **KILLED_PBO** | H8_E100_V96_PT2_SL1 (+0.051), was V168 | 0.620 (N 54) |
| XAUUSD rs / vc | 406 | 0.085 / 0.288 | 0.058 / 0.171 | PASS | unchanged (2026-10-17 runs already held all cells) | 0.085 / 0.305 |
CPCV paths (daily Sharpe per path; live chain with the frozen calibrator and the allocator's live threshold):
| edge | N=5: 4 paths (mean / sd / min / share <= 0) | N=10: 9 paths (mean / sd / min / share <= 0) | this-run averaged path | stored averaged path (2026-10-17 OOF) |
|---|---|---|---|---|
| GBPJPY | 0.69 / 0.07 / 0.59 / 0 | 0.67 / 0.06 / 0.61 / 0 | 0.66 (1,031 approved) | 0.91 (2,738) |
| HK | 0.39 / 0.09 / 0.31 / 0 | 0.52 / 0.17 / 0.20 / 0 | 0.56 (131) | 0.90 (2,571) |
| JPN rs | 0.74 / 0.02 / 0.72 / 0 | 0.74 / 0.01 / 0.71 / 0 | 0.74 (4,461) | 0.74 (4,461) |
| JPN vc | 0.33 / 0.32 / -0.02 / 0.25 | 0.06 / 0.34 / -0.62 / 0.33 | -0.28 (83) | 0.06 (173) |
| XAUUSD rs | 1.51 / 0.03 / 1.48 / 0 | 1.58 / 0.04 / 1.52 / 0 | 1.57 (2,567) | 1.65 (2,251) |
| XAUUSD vc | 0.87 / 0.21 / 0.58 / 0 | 0.90 / 0.07 / 0.76 / 0 | 0.87 (1,158) | 1.33 (1,708) |
Findings: (1) paths agree closely on JPN rs, XAUUSD rs, GBPJPY, XAUUSD vc; moderately on HK; JPN vc is unstable (a third
of the 9 paths lose) -- the same edge the book-N PBO kills (Prob 0.527). (2) The stored-vs-this-run gap (e.g. HK 2,571 ->
131 approved) is NOT a path effect: this run refits with today's Step 03 code (Ch.4-9 changes), whose probabilities no
longer match the frozen calibrator / allocator threshold fitted on the 2026-10-17 OOF -- calibration and thresholds must be
refit in the retrain batch. Path dispersion within one run is the valid result. Owner decision already taken: adopt the
CPCV path distribution for selection and sizing (design pending approval).

**Ch.12 closed (owner 2026-10-26):** JPN/volatilitycarry_long removed from config/edge_universe.json (book-N PBO
Prob[OOS<=0] 0.527 KILLED + unstable CPCV paths; folder untouched; backup edge_universe.json.bak_20261026_pre_ch11_jpn_vc);
deployable edges: GBPJPY dailyrangecompletionfade_long, JPN rangestructure_long, HK drawdownhazardcontinuation_short, XAUUSD
rangestructure_long. D052 approved (CPCV path distribution; Step 03 N = 10 k = 2 saving every path, Step 04 calibrator on
all paths, allocator 04b/04c pick by the mean across paths). Live threshold location VERIFIED in code: 04b
build_live_threshold_sweep -> 04c select_representatives_for_edge (one threshold per edge) -> 05-11 ->
portfolio_live/00_build_live_deployment_manifest.py line 346 (final_live_threshold). Runbook Revision 20 (Step 10H,
Appendix 37). Owner instruction: read the portfolio_allocator scripts before implementing anything there.

### 4M.1 OTR test (owner-approved 2026-10-26; pre-registered before results)

Owner points: the D052 allocator change is NOT implemented yet (scheduled for the retrain batch; nothing in
portfolio_allocator changed); run BOTH populations -- approved trades (live exits) and all primary events (the same
barriers are the training labels); the book (p.170) speaks of strategy S's opportunities and of the exit corridor of an
existing position, and ties the thresholds to the meta-labelling barriers, without separating primary from approved.
Flagged assumption: the book's OLS needs each trade's forecast E0[P_i,T]; Quantara gives a probability, so ONE common E0
(in R) is estimated from eq. 13.2 with an intercept (phi_hat = the book's cov[Y,X]/cov[X,X]; E0 = intercept / (1 - phi)).
Tool `shared/research/ch13_optimal_trading_rule.py`: Snippet 13.2 mechanics exactly (P0 = 0; exit at the first p > PT,
p < -SL or hp > maxHP at the value reached; Sharpe = mean / std, eq. 13.1); 100,000 paths, seed 0; maxHP = median research
bars from entry to the vertical deadline; meshes: the book's 20 x 20 at 0.5-10 sigma and the R mesh PT 0.25-6 x SL
0.25-4; current rule (2 R, 1 R) exact; steps 5a / 5b / 5c; without costs (book) and with the mean Moneta cost per trade.
Report: OTR, current rule's Sharpe and percentile, flatness (best - median). One trial per edge. Logs
`logs/ch13_otr_20261026/`.
Correction before any result was reported (2026-10-26): v1 of the tool fitted the O-U process on bar closes only while
the trade was open (paths truncated at our own barriers). That biases the fit (the 1 R stop is closer than the 2 R target,
so truncation cuts more downside and fakes an upward pull: v1 implied E0 +0.55..+0.72 R and Sharpe ~6 for "PT 0.75 R / SL
4 R", while the realised mean is +0.11..+0.23 R). v2 fits the full price path from entry to the vertical deadline, ignoring
our barriers, and adds a random-walk null (same estimator on random walks with the same path lengths, drift and step
volatility, 200 reps) because short-path OLS biases phi below 1 even for a random walk. v1 outputs kept in
logs/ch13_otr_20261026/v1_truncated_paths_superseded/ (not used).
**Results v2 (2026-10-26; reports `artifacts/feature_importance_audit/ch13_optimal_trading_rule.json`; trials logged).**
| edge (population) | phi_hat | random-walk null 95 % | half-life (bars) vs maxHP | surface best - median Sharpe | R-mesh OTR (PT/SL) | current 2/1: simulated SR, percentile | realised mid SR per trade |
|---|---|---|---|---|---|---|---|
| GBPJPY (approved) | 0.9999 | 0.9998-1.0002 | 8,255 vs 312 | 0.062 | 6.0 / 4.0 (mesh corner) | 0.053, 21 % | 0.126 |
| HK (approved) | 0.9998 | 0.9997-1.0002 | 2,777 vs 248 | 0.018 | 0.25 / 3.5 | -0.012, 55 % | 0.128 |
| JPN rs (approved = all) | 1.0007 | 0.9998-1.0002 | none (phi > 1) vs 160 | 0.036 | 6.0 / 3.0 (corner) | 0.051, 26 % | 0.102 |
| XAUUSD rs (approved) | 0.9996 | 0.9984-1.0012 | 1,903 vs 41 | 0.022 | 4.25 / 4.0 | 0.159, 28 % | 0.190 |
All primary events: same picture (phi 0.9989-1.0007; GBPJPY, HK and XAUUSD fall just below the null's lower bound, but with
half-lives of 600-5,900 bars against holding periods of 40-314 bars, i.e. no mean reversion inside a trade). With costs:
same ranking, Sharpe about 0.01-0.03 lower.
Findings: (1) inside a trade our prices behave as a random walk with a small drift (+0.002..+0.009 R per bar; HK -0.0003)
-- the book's s.13.6 case where the Sharpe surface is flat and "there is no optimal trading rule"; the implied E0 values
(+32, -1, -3, +25 R) are meaningless divisions by 1 - phi ~ 0. (2) The "optimal" nodes sit on the mesh edges (wider
barriers just harvest more drift), so the OTR is not an interior optimum and would move with the drift estimate.
(3) The O-U / random-walk model does not reproduce our realised results (simulated Sharpe at 2/1 is 0.03-0.14 below the
realised 0.10-0.19; HK even negative): the edges' profit is not a constant in-trade drift, so this model's OTR should not
set our geometry. (4) Our 2/1 rule sits at the 21st-55th percentile of a nearly flat surface: the process neither
supports it nor any other rule -- consistent with the book's warning that optimising PT/SL on history here is
overfitting (D050).
Owner check of the forecast-price assumption (2026-10-26): with the book's literal eq. 13.5-13.7 and a PER-TRADE forecast
E0 = p PT + (1 - p) SL in R (the forecast price "consistent with the barriers' settings", AFML s.10.6 p.145, from our
meta-model's p), phi_hat stays ~1: GBPJPY 0.99964, HK 0.99992, JPN rs 1.00059, XAUUSD rs 0.99955 -- over a whole trade
the price closes only 2-11 % of the gap to the forecast (JPN drifts away). The no-OTR finding does not depend on the E0
assumption.

**Ch.13 closed (owner 2026-10-26):** the book's answer to "no optimal rule" (s.13.6 pp.178, 180, 187; footnote 2
p.172) is not a different rule: do not tune PT/SL on history; fix the rule by thesis / risk and judge the strategy by
PBO / DSR. D053 recorded (no OTR, geometry unchanged, not tuned on history; feeds D050 after Ch.14). No alternative-process
test (exercise 13.1(b)). D052 implementation choice (f* once on the main path, gates on the main path) confirmed by the
owner. Runbook Revision 21 (Step 10I, Appendix 38).

### 4L.2 D052 implemented (owner go-ahead 2026-10-26; takes effect at the retrain + allocator re-run)

- Step 03 (all 6 edges, inlined helper `QUANTARA_AFML_CH12_CPCV_PATHS_V1`): CombinatorialPurgedCV(N = 10, k = 2) -> 45
  splits, phi = 9 paths; each split's test groups are written to their path (skfolio `get_path_ids`); OOF gains
  `cpcv_path_00..08_raw_probability`; `raw_probability` stays as the averaged diagnostic.
- Step 04: the live sigmoid calibrator is fitted on all paths stacked (AFML s.12.4.2: every path is a full out-of-sample
  forecast); fails closed if Step 03 has no paths. The fold-averaged CPCV calibration diagnostic is unchanged.
- 04b: every grid threshold is also scored on every path (same live calibrator, frozen payoff profile, broker-risk
  columns) -> `outputs/v2_threshold_grid_cpcv_path_trade_ledger.csv`; the grid gates and the main ledger are unchanged.
- 04c: each (threshold, path) gets the main growth formula at the main path's optimized f* (signed, so a losing path
  pulls the mean down instead of -inf); selection score = mean across the 9 paths (AFML s.12.5); gates stay on the main
  path; the edge report adds worst / median / sd / losing-path share, the single-path score and the threshold a single
  path would have picked.
- Both 04b and 04c fail closed without paths unless `--allow-legacy-single-path` (pre-D052 artifacts only).
- Flagged implementation choice for the owner: f* is optimized once on the main path and held fixed across paths (the
  book scores a fixed strategy on each path); gates on the main path.
- Tests: `tests/test_afml_ch12_cpcv_paths.py` (10 pass; skfolio CPCV(10, 2) gives 9 complete paths with the right split
  per row). 04b smoke on GBPJPY real artifacts with synthetic paths: path 0 reproduces the main ledger exactly (3,419
  trades, same rows and AFML sizes). Full suite: 513 tests; the 5 venv312 failures are the known `ast.dump` artefact and
  pass on venv39.

## Implementation backlog (owner rule 2026-10-26)

Nothing approved may be forgotten: `config/mldp_implementation_backlog.json` lists every approved item that is not fully
in effect (state, what is left, where, when); `tests/test_mldp_backlog.py` fails if an open register item is missing from
it. Read it at the start of every chapter and before the retrain batch.
