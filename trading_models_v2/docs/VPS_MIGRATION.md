# VPS migration: git is the backup (owner, 2026-11-10, STANDING)

The research VPS is temporary. A new (smaller) VPS must be able to: clone this repo -> run `portfolio_allocator/`
scripts -> run `portfolio_live/` scripts -> trade, **without redoing any edge research**.

## Must be in git (push after every promotion / data rebuild)
| What | Where | How |
|---|---|---|
| Deployable edge list | `config/edge_universe.json` | plain |
| Each promoted edge: code, contracts, reports, manifests, model + calibrator `.pkl`, selected threshold | `assets/<A>/edges/<edge>/` | plain |
| Each promoted edge: model_rows, mldp_execution_resolution, oof_predictions, oof_exact_execution, calibrated_oof_predictions, calibrated_threshold_sweep CSVs | `assets/<A>/edges/<edge>/artifacts/...` | Git LFS (`.gitattributes`), whitelisted in root `.gitignore` |
| Asset bars, FFD + HMM features and live bundles, curated manifests | `assets/<A>/bars/`, `assets/<A>/curated/` | Git LFS for the big CSVs |
| Broker cost models, era-scale factors, conversions | `portfolio_allocator/config/` | plain |
| Rules, runbook, ledgers | `config/*.json`, `Quantara_Runbook.docx`, `docs/` | plain |

## Deliberately NOT in git
- `*_feature_matrix.csv`, `*_candidate_events.csv` (research intermediates; the allocator does not read the feature matrix).
- Raw ticks `assets/<A>/raw/` (~125 GB). Not read by the allocator; needed only to re-run research. Re-download with
  `shared/data/download_history_ticks.py` or keep an off-site copy.
- `portfolio_live/local_live_settings.json` (credentials -- re-enter on the new VPS), `live_state/`, `live_bars/`,
  `live_logs/`, `outputs/` (rebuilt by the scripts).

## Repo and keys (2026-11-10)
- Repo: `Wells21/Quantara` (private), branch `main`. Fresh start commit fcc501a (no old history; the old repo
  `trading_models` hit the 10 GB LFS limit with ~13.7 GB of old LFS versions). Full old history: git bundle
  `trading_models_full_history.bundle` (owner keeps a copy).
- Push/pull key: a deploy key with write access on Quantara only. This VPS: `~/.ssh/quantara_deploy_ed25519`,
  used through `git config core.sshCommand` (repo setting). A new VPS needs its own deploy key added in
  Quantara -> Settings -> Deploy keys (read-only is enough if it only pulls).
- Clone: `git clone git@github.com:Wells21/Quantara.git` (with that key), then `git lfs pull`.

## New-VPS run order (verified on a fresh clone 2026-11-10)
1. Python venvs (venv39 for portfolio_allocator / Phase C-D; venv312 for research) -- not in git.
2. MT5 + broker login: `portfolio_live/local_live_settings.json` (credentials, never in git) and step 00a.
3. Allocator: 01 -> 02 -> 03 -> 04b -> 04bb -> 04c -> 04d -> 05 -> 06 all PASS on a fresh clone with one edge.
   07 (HRP) and everything after it need >= 2 edges in config/edge_universe.json (MIN_COLUMNS_FOR_HRP = 2).
4. Step 00d (re-price every edge's trades at the current broker's costs) replays trades on CURATED TICKS
   (41 GB for XAUUSD alone), so it runs on the research VPS and its per-edge output
   `portfolio_allocator/outputs/broker_repricing/<firm>/<ASSET>__<edge>.csv` (~0.5-1 MB) is committed. A new VPS
   starts at 04b. If the broker or its costs change, 00d must be rerun on re-downloaded, curated ticks for the
   deployed assets.

## Status 2026-11-10
- Fresh-clone test: LFS 34/34 files downloaded, `git lfs fsck` OK; allocator 01-06 PASS (with the 00d file
  copied in); 07 stops by design with one edge.
- 00d CSV for XAUUSD/roundlevelcascade_long committed (ad4cae78). Git still treats the broker_repricing/<firm>/
  folders as ignored, so add each newly promoted edge's 00d CSV with `git add -f <file>` (once tracked, it stays
  tracked). Only edges in config/edge_universe.json; the old CSVs in that folder stay out.

## Status 2026-11-11 -- FULL ALLOCATOR VERIFIED ON A FRESH CLONE
- Fresh clone of Wells21/Quantara (3 edges: XAUUSD/roundlevelcascade_long, DAX/closehedgingflow_long,
  DAX/rangebreakout_long) ran 01 -> 02 -> 03 -> 04b -> 04bb -> 04c -> 04d -> 05 -> 06 -> 07 -> 07b -> 07b_geometry
  -> 07c -> 08 -> 08b -> 08c -> 09 -> 10 -> 11, every step rc 0 (venv39; 11 Monte Carlo ~18 min). Nothing copied in
  from this VPS. Step 09 is the owner's manual variant choice; the test ran it with its defaults (--full-single).
- Step 00b (MT5 conversion history EURUSD / USDHKD / USDJPY) is committed in
  portfolio_allocator/config/broker_conversions. Moneta's MT5 server holds FX history only from 2010, and USDHKD
  has 1-3.5 month holes in 2011, so the files start 2012-01-01 (`00b ... --start 2012-01-01`, manifest PASS).
  Enough for today's edges (gold is USD, DAX trades start 2014). A future HK / JPN / EUR-profit edge with
  trades before 2012 would fail 04b with missing_prior_conversion -> rerun 00b and decide then.
- Every promoted edge must carry its all-trials DSR record at
  `assets/<ASSET>/edges/<edge>/artifacts/feature_importance_audit/ch14_dsr_all_trials.json` (copy of the
  post-PBO `logs/<edge>_<ASSET>_dsr_full_era.json`; `git add -f`), or step 07b stops.
- Promotion checklist per edge: edge folder + edge_universe.json entry + 00d CSV (git add -f) + DSR record
  (git add -f), then commit and push.
