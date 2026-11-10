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

## Status 2026-11-10
- XAUUSD/roundlevelcascade_long: all whitelisted allocator inputs committed (d5cd748a).
- History-extended bars / FFD / regime files (~1.5 GB, all 10 assets): modified locally, NOT yet committed
  (GitHub LFS storage quota -- owner decision).
- Not yet proven by a fresh clone. Proof step: clone into an empty folder, `git lfs pull`, run the allocator and live
  preflight checks, list anything missing.
