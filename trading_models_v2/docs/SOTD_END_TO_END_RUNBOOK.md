# SOTD End-to-End Research-to-Live Runbook

**Document status:** Single living operating guide  
**Applies to:** `trading_models_v2`  
**Source of truth:** SOTD — V2 Research-to-Live Trading System Architecture and Implementation Standard, including the scalability mandate  
**Audience:** Future developer, future Favour, or any engineer maintaining the V2 system without AI assistance

---

## 0. Purpose

This is the practical operating manual for the V2 research-to-live system. Do not create competing workflow docs. Update this file whenever implementation changes.

It explains how to add assets, add side-specialized edges, run the edge lifecycle, run early and deployable alpha-independence checks, promote lifecycle status after Step 09, run portfolio allocation, export live manifests, operate dashboard/live execution, preserve Colab compatibility, and keep downstream components aligned with SOTD.

The current XAUUSD V2 portfolio target is **10 useful deployability-PASS long edges**. As of this runbook refresh, the intended deployable XAUUSD long set is:

```text
XAUUSD/trend_long
XAUUSD/absorption_long
XAUUSD/reversal_long
XAUUSD/rangestructure_long
XAUUSD/timeregime_long
XAUUSD/volatilitycarry_long
XAUUSD/volatilityregime_long
XAUUSD/dailyrangecompletionfade_long
XAUUSD/trendpullbackcontinuation_long
XAUUSD/compressionbreakoutcontinuation_long
```

`deepreversal_long` was intentionally removed/retired as a duplicate-style edge of `reversal_long`. Research-only edges such as `trend_short` and `absorption_short` can remain in the registry as non-PASS, but allocator/live ingestion must consume only PASS manifests.

---

## 1. Core SOTD laws

### 1.1 Reproducibility law

An edge is not deployable unless it can reproduce its exact selected model-input row live using its own runtime package and the same code path used in research. Good metrics do not override this rule.

### 1.2 Scalability law

The folder layout is the identity system:

```text
assets/<ASSET>/edges/<family>_<side>/
```

Shared code derives:

```text
asset      = <ASSET>
family     = text before the final underscore
side_name  = text after the final underscore
side_value = +1 for long, -1 for short
edge_id    = <ASSET>/<family>_<side>
edge_stem  = <asset_lower>_<family>_<side>
```

Do not hardcode current edges in shared modules. Future edges must work through the same layout, contracts, manifests, and runtime API.

### 1.3 Side-specialization law

Every alpha edge is side-specialized. Do not create one runtime that decides both long and short live. Use separate edge packages:

```text
trend_long
trend_short
absorption_long
absorption_short
rangestructure_long
rangestructure_short
```

### 1.4 Shared-core law

Reusable lifecycle logic belongs in `shared/`. Edge-specific logic may live in:

```text
candidate_events.py
primary_side.py
model_rows.py
runtime.py edge-specific import/function references
contracts/feature_schema.json
configs/*.yaml
edge.yaml
```

Shared modules own layout resolution, artifact paths, contract validation, feature locking, CPCV utilities, calibration, AFML sizing, runtime scoring, registry verification, status promotion, edge manifest export, artifact hashing, alpha-independence diagnostics, and allocator/live manifest validation helpers.

### 1.5 Runtime law

Every runtime must expose:

```text
load_contract()
validate_environment()
build_features()
generate_candidate_events()
build_model_rows()
load_model()
load_calibrator()
score()
explain_missing_features()
```

Before model, calibrator, selected features, model-input schema, and threshold artifacts exist, scoring must fail closed.

### 1.6 Runtime-template law

For future edges, `runtime.py` should remain almost identical edge-to-edge. The normal changes should be:

```text
EDGE_ID / derived layout identity
candidate_events import/function reference
primary_side import/function reference
model_rows import/function reference
self-test print labels
edge-specific comments
```

Do not create one-off runtime scoring logic. Edge-specific alpha logic belongs in `candidate_events.py`, `primary_side.py`, `model_rows.py`, and optional edge-local helpers called by runtime.

### 1.7 Event-feature law

Every feature or event field used in research must be reproducible through `runtime.build_features()` and `runtime.generate_candidate_events()`.

For newer statistical edges, it is acceptable for event/control/context fields to be computed inside `candidate_events.py` rather than included in the generic model `feature_schema.json`, as long as:

```text
runtime.generate_candidate_events(features) computes them
all fields use only current/past completed bars
research export persists the same event fields
live scoring can rebuild the same candidate rows
no dashboard/manual/notebook-only engineering is required
```

### 1.8 MT5 live-data law

Any live edge must be reproducible from MT5/live standard_quote_tick bars. Do not make live event gates depend on unavailable data such as:

```text
real exchange volume
order book depth
bid/ask queue imbalance
trade aggressor imbalance
funding rate
sentiment feeds
news feeds
Ticks as alpha volume
Proxy_Value as alpha volume
Tick_Activity_Ratio
Proxy_Value_Ratio
Activity_Impulse
```

`Ticks` and `Proxy_Value` may exist for bar construction/audit compatibility, but price-only edges must not use them as alpha inputs unless a formal ADR approves a new live data contract.

### 1.9 Scoring law

Runtime scoring path:

```text
raw_probability -> sigmoid calibrated_probability -> AFML bet size -> calibrated-threshold decision
```

`runtime.score()` must call `shared.scoring.edge_scorer.EdgeScorer`. Runtime must not locally implement model loading, calibrator loading, threshold loading, sigmoid calibration, AFML sizing, signal decision logic, or artifact hashing.

### 1.10 Step 04 law

Step 04 implementation lives in:

```text
shared/calibration/fit_sigmoid_calibrator_and_thresholds.py
```

Edge-local `research/04_fit_sigmoid_calibrator_and_thresholds.py` files are thin wrappers only. They exist for command clarity and Colab/local workflow compatibility.

Step 04 must enforce calibrated-probability semantics and must select thresholds in `calibrated_probability` space, not `raw_probability` space.

### 1.11 Step 04B law

Never manually edit:

```text
artifacts/selected_features.json
contracts/model_input_schema.json
```

They must be generated by:

```text
shared/edges/lock_selected_features_and_model_input_schema.py
```

### 1.12 Step 06 law

Step 06 verifies runtime scoring readiness. It must not patch `runtime.py`. Do not use `06_wire_runtime_score.py` as a standard.

### 1.13 Step 09 exporter and status-promotion law

Step 09 implementation lives in:

```text
shared/edges/export_edge_manifest.py
```

Edge-local `research/09_export_edge_manifest.py` files are thin wrappers only.

Step 09 exports:

```text
artifacts/edge_manifest.json
```

Step 09 does not automatically update `edge.yaml` or `config/edge_universe.json`.

After Step 09 PASS, promote status with:

```powershell
python .\shared\edges\promote_edge_status.py --edge-root .\assets\<ASSET>\edges\<edge_name> --status deployability_pass --write
python .\shared\edges\verify_edge_registry.py
```

Invalid state:

```text
edge_manifest.json = PASS / deployable=true
edge.yaml = non-PASS status
edge_universe.json = non-PASS status
```

Registry verification must fail in that mismatch state.

### 1.14 Registry fail-closed law

`verify_edge_registry.py` must treat states differently:

```text
deployability_pass                      -> requires PASS manifest and replay PASS fields
research_in_progress/audit_pending/etc. -> may be incomplete but must fail closed
scaffold_not_deployable                 -> requires scaffold surface and no fake PASS manifest
```

A research edge should not need future lifecycle artifact folders before those lifecycle steps run. Do not create fake artifacts just to satisfy a verifier.

### 1.15 Colab research workflow law

Heavy research steps may be run in Colab when local hardware is too slow, especially:

```text
01_feature_surgery_clustered_mda.py
02_train_meta_model_optuna.py
```

That is allowed if output artifacts are copied back into the canonical edge folders and then verified locally through the SOTD gates. Colab is an execution environment, not a second architecture.

After Colab outputs are copied back, the local repo remains the source of truth and must pass the local SOTD gates.

### 1.16 Dashboard/live law

The dashboard does not engineer edge features manually. It orchestrates:

```text
MT5 ticks -> live standard_quote_tick bars -> runtime.build_features() -> runtime.generate_candidate_events() -> runtime.score() -> approved signal export
```

The execution bot consumes approved signal schema and live manifest only. It must not rebuild research labels, train models, select thresholds, or override artifact hashes.

---

## 2. Canonical layout

```text
trading_models_v2/
  config/edge_universe.json
  shared/
    assets/
    bars/
    edges/
    validation/
    models/
    calibration/
    scoring/
    io/
    ffd/
    regime/
    independence/
  assets/<ASSET>/
    raw/ticks/
    bars/standard_quote_tick/
    ffd/
    regime/
    alpha_independence_lab/
      run_asset_independence_lab.py
      outputs/
    edges/<family>_<side>/
      edge.yaml
      runtime.py
      candidate_events.py
      primary_side.py
      model_rows.py
      feature_expansion.py
      regime_features.py
      export_research_dataset.py
      contracts/
      configs/
      artifacts/
      reports/
      research/
  portfolio_allocator/
  portfolio_live/
  docs/SOTD_END_TO_END_RUNBOOK.md
```

---

## 3. Adding a new edge package

Run all commands from repo root:

```powershell
cd "C:\Users\wells\Documents\WELLSMART Company\Trading Models\trading_models_v2"
```

### 3.1 Create edge folder layout

```powershell
@'
from shared.edges.edge_layout import get_edge_layout

layout = get_edge_layout("XAUUSD", "<family>", "long")
layout.create_directories()

print("edge_root:", layout.edge_root)
print("edge_id:", layout.edge_id)
print("edge_stem:", layout.edge_stem)
'@ | python -
```

Example:

```text
family = trendpullbackcontinuation
side   = long
edge   = assets/XAUUSD/edges/trendpullbackcontinuation_long
```

### 3.2 Copy safe non-artifact files from a close source edge

Copy only source-code/contracts/config scaffolding. Do **not** copy lifecycle outputs.

Safe to copy:

```powershell
$src = ".\assets\XAUUSD\edges\<closest_existing_edge>"
$dst = ".\assets\XAUUSD\edges\<new_edge>"

Copy-Item "$src\README.md" "$dst\README.md" -Force
Copy-Item "$src\runtime.py" "$dst\runtime.py" -Force
Copy-Item "$src\candidate_events.py" "$dst\candidate_events.py" -Force
Copy-Item "$src\primary_side.py" "$dst\primary_side.py" -Force
Copy-Item "$src\model_rows.py" "$dst\model_rows.py" -Force
Copy-Item "$src\research_pipeline.py" "$dst\research_pipeline.py" -Force
Copy-Item "$src\export_research_dataset.py" "$dst\export_research_dataset.py" -Force
Copy-Item "$src\feature_expansion.py" "$dst\feature_expansion.py" -Force
Copy-Item "$src\regime_features.py" "$dst\regime_features.py" -Force
Copy-Item "$src\contracts\*" "$dst\contracts\" -Force
Copy-Item "$src\configs\*" "$dst\configs\" -Force
```

Never copy:

```text
artifacts/
reports/
artifacts/edge_manifest.json
trained model files
calibrator files
OOF files
threshold files
selected_features.json
training_summary.json
validation_report.json
model_input_schema.json generated for another edge
```

### 3.3 Replace identity safely with Python

Do not use `Set-Content -Encoding UTF8`; it may introduce BOM problems.

```powershell
@'
from pathlib import Path

edge = Path("assets/XAUUSD/edges/<new_edge>")
files = [
    edge / "README.md",
    edge / "runtime.py",
    edge / "candidate_events.py",
    edge / "primary_side.py",
    edge / "model_rows.py",
    edge / "research_pipeline.py",
    edge / "export_research_dataset.py",
    edge / "feature_expansion.py",
    edge / "regime_features.py",
]
files += list((edge / "contracts").glob("*"))
files += list((edge / "configs").glob("*"))

replacements = {
    "XAUUSD/<old_edge>": "XAUUSD/<new_edge>",
    "xauusd_<old_edge>": "xauusd_<new_edge>",
    "<old_edge>": "<new_edge>",
}

for path in files:
    if not path.is_file():
        continue
    text = path.read_text(encoding="utf-8-sig")
    for old, new in replacements.items():
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8", newline="\n")
    print("updated:", path)
'@ | python -
```

### 3.4 Write edge.yaml for the actual edge identity

`edge.yaml` must match folder identity exactly:

```text
edge_id: XAUUSD/<family>_<side>
asset: XAUUSD
family: <family>
side_name: long
side_value: 1
edge_stem: xauusd_<family>_long
status: research_in_progress
```

Update `alpha_contract`, `primary_side_rule`, `labeling.row_builder`, and notes to the real logic. Do not leave stale copied references such as `reversal_long`, `deepreversal_long`, or `Deviation_Side` unless that is truly the edge logic.

### 3.5 Implement the five edge-specific files

The professional V2 edge-edit surface is:

```text
candidate_events.py
primary_side.py
model_rows.py
runtime.py import/function references
export_research_dataset.py
```

Rules:

```text
candidate_events.py     -> event/control/context fields and event gate
primary_side.py         -> side-specialized mapping and fail-closed validation
model_rows.py           -> 1R triple-barrier labels for research only
runtime.py              -> keep template stable; update import/function references and self-test labels only
export_research_dataset.py -> persist runtime-built feature matrix, candidate events, model rows, manifests
```

Do not make dashboard, notebooks, or one-off scripts compute edge-specific features that runtime cannot reproduce.

---

## 4. Edge design policy after the XAUUSD 10-edge buildout

The edge-building evidence from XAUUSD is now clear:

```text
Good useful edges are broad event/state engines with enough model rows.
Over-specific textbook patterns are usually too sparse.
```

### 4.1 Minimum event-count rule

Before spending time on training, the runtime self-test should normally produce:

```text
minimum model rows: 2,500+
preferred model rows: 3,500–8,000+
```

Reject, park, or redesign edges that produce only a few hundred rows unless there is a formal reason to keep them.

Edges that were too sparse or not worth forcing:

```text
openingauctionbias_long
dailyrangeunderfillbreakout_long
volatilityshockreversion_long
```

### 4.2 Raw event quality rule

Raw meta-label distribution does not need to be perfect. A broad event set can be useful even around 49–51% raw win rate if the PT-SL structure is healthy and the meta-model has enough rows.

Preferred raw checks before export:

```text
model rows >= 2,500, preferably 3,500+
PT rate - SL rate positive
vertical rate not excessive
no huge ambiguous/fallback label count
labeling stats sane
```

### 4.3 Do not overfit the event gate

For new statistical edges, tune in this order:

```text
1. loosen non-core quality/position filters
2. adjust spacing/min_gap_bars only after core logic is stable
3. avoid weakening the core alpha definition too early
4. stop once event count and raw edge are healthy
```

Example from `compressionbreakoutcontinuation_long`:

```text
core alpha = compression + upside expansion + positive return + bullish close
safe first loosening = close/body/channel-position quality
safe second loosening = MIN_GAP_BARS 3 -> 2
risky loosening = removing compression or range expansion requirement
```

### 4.4 Independence does not mean zero correlation

Same-asset long edges may correlate. That is not automatic deletion evidence. The early deletion decision should focus on:

```text
trade overlap
high correlation plus crowded overlap
marginal contribution
whether the trigger/timing is actually different
```

High correlation alone means allocator/HRP should cluster-weight or risk-cap it. It does not automatically mean the edge is redundant.

---

## 5. Syntax, contract, runtime, and export checks

### 5.1 Syntax check

```powershell
python -m py_compile `
 .\assets\<ASSET>\edges\<edge_name>\candidate_events.py `
 .\assets\<ASSET>\edges\<edge_name>\primary_side.py `
 .\assets\<ASSET>\edges\<edge_name>\model_rows.py `
 .\assets\<ASSET>\edges\<edge_name>\runtime.py `
 .\assets\<ASSET>\edges\<edge_name>\export_research_dataset.py `
 .\assets\<ASSET>\edges\<edge_name>\feature_expansion.py `
 .\assets\<ASSET>\edges\<edge_name>\regime_features.py
```

### 5.2 Package/contract checks

Use these from `trading_models_v2`:

```powershell
python .\shared\edges\edge_yaml_contract.py --edge-root .\assets\<ASSET>\edges\<edge_name>
python .\shared\validation\edge_contracts.py --edge-root .\assets\<ASSET>\edges\<edge_name>
python .\shared\edges\validate_edge_feature_schema.py --asset <ASSET> --family <family> --side <side>
python .\shared\edges\verify_edge_registry.py
```

Do not use a scaffold verifier that requires fake future lifecycle artifacts for a normal `research_in_progress` edge. A research edge should not create fake `selected_features.json`, fake `edge_manifest.json`, or fake model artifacts.

### 5.3 Runtime self-test

```powershell
python .\assets\<ASSET>\edges\<edge_name>\runtime.py
```

Expected self-test shape:

```text
Environment: PASS
Bar contract: PASS
Feature implementation: ...
Candidate events after primary side mapping: > 0
Model rows built: > 0
Meta-label distribution: {...}
Barrier hit distribution: {...}
Model artifact/calibrator/score: IMPLEMENTED — REPLAY AUDIT PENDING
```

Do not train/export if runtime cannot build features, events, and model rows through the edge package.

### 5.4 Research dataset export

Dry run:

```powershell
python .\assets\<ASSET>\edges\<edge_name>\export_research_dataset.py --dry-run
```

Actual export:

```powershell
python .\assets\<ASSET>\edges\<edge_name>\export_research_dataset.py --overwrite
```

The exporter must call:

```text
runtime.build_features()
runtime.generate_candidate_events()
runtime.build_model_rows()
```

Expected outputs:

```text
assets/<ASSET>/edges/<edge_name>/artifacts/research_dataset/<edge_stem>_feature_matrix.csv
assets/<ASSET>/edges/<edge_name>/artifacts/research_dataset/<edge_stem>_candidate_events.csv
assets/<ASSET>/edges/<edge_name>/artifacts/research_dataset/<edge_stem>_model_rows.csv
assets/<ASSET>/edges/<edge_name>/artifacts/research_dataset/model_row_manifest.json
assets/<ASSET>/edges/<edge_name>/artifacts/research_dataset/research_dataset_manifest.json
```

---

## 6. Formal edge lifecycle: 00 -> 09

After model rows exist, run the lifecycle in order:

```powershell
python .\assets\<ASSET>\edges\<edge_name>\research\00_validate_edge_contract.py
python .\assets\<ASSET>\edges\<edge_name>\research\01_feature_surgery_clustered_mda.py
python .\assets\<ASSET>\edges\<edge_name>\research\02_train_meta_model_optuna.py
python .\assets\<ASSET>\edges\<edge_name>\research\03_generate_oof_predictions.py
python .\assets\<ASSET>\edges\<edge_name>\research\03b_oof_threshold_sweep.py
python .\assets\<ASSET>\edges\<edge_name>\research\04_fit_sigmoid_calibrator_and_thresholds.py
python .\assets\<ASSET>\edges\<edge_name>\research\04b_lock_selected_features_and_model_input_schema.py
python .\assets\<ASSET>\edges\<edge_name>\research\05_training_artifact_verifier.py
python .\assets\<ASSET>\edges\<edge_name>\research\06_verify_runtime_scoring_ready.py
python .\assets\<ASSET>\edges\<edge_name>\research\07_runtime_score_smoke_test.py
python .\assets\<ASSET>\edges\<edge_name>\research\08_probability_calibration_afml_replay_audit.py
python .\assets\<ASSET>\edges\<edge_name>\research\09_export_edge_manifest.py
```

Only after Step 09 produces a PASS `artifacts/edge_manifest.json`, promote:

```powershell
python .\shared\edges\promote_edge_status.py --edge-root .\assets\<ASSET>\edges\<edge_name> --status deployability_pass --write
python .\shared\edges\verify_edge_registry.py
```

---

## 7. Edge registry rules

`config/edge_universe.json` is the registry of available edges. It is not proof of deployability by itself.

For a new edge:

```json
{
  "asset": "XAUUSD",
  "family": "<family>",
  "side": "long",
  "enabled": true,
  "status": "research_in_progress",
  "bar_scope": "asset_level_shared_bars",
  "bar_mode": "standard_quote_tick",
  "threshold_unit": "ticks",
  "candidate_event_generator": "<edge_name>_candidate_events",
  "notes": "Research edge. Must pass full 00-09 lifecycle before allocator ingestion."
}
```

Do not set:

```text
"status": "deployability_pass"
```

until Step 09 PASS and status promotion are complete.

---

## 8. Alpha independence lab

The V2 alpha-independence lab lives at:

```text
assets/<ASSET>/alpha_independence_lab/run_asset_independence_lab.py
shared/independence/asset_independence_lab.py
```

It consumes artifacts only. It does **not** score models and does **not** engineer features.

### 8.1 Early candidate overlap check

Use after research dataset export, before full deployability, especially when deciding whether a new edge is a duplicate before training:

```powershell
python .\assets\XAUUSD\alpha_independence_lab\run_asset_independence_lab.py --asset XAUUSD --mode research_events --include-non-pass
```

`research_events` uses exported model rows and ignores probability thresholds. This is candidate-opportunity overlap only; it is not deployability evidence.

### 8.2 Deployable selected-trade check

Use after Step 09 PASS for deployable edges:

```powershell
python .\assets\XAUUSD\alpha_independence_lab\run_asset_independence_lab.py --asset XAUUSD --mode selected_trades
```

This uses calibrated OOF predictions and each edge's selected calibrated-probability threshold artifact.

### 8.3 Broad threshold/hidden-overlap check

Use this when selected thresholds hide overlap or when comparing broad post-model candidate banks:

```powershell
python .\assets\XAUUSD\alpha_independence_lab\run_asset_independence_lab.py --asset XAUUSD --mode selected_trades --threshold-policy override --probability-threshold 0.10
```

For scored candidate banks:

```powershell
python .\assets\XAUUSD\alpha_independence_lab\run_asset_independence_lab.py --asset XAUUSD --mode scored_candidates --threshold-policy override --probability-threshold 0.10
```

### 8.4 Outputs

The lab writes outputs under:

```text
assets/<ASSET>/alpha_independence_lab/outputs/<mode>/
```

Important files:

```text
alpha_summary.csv
combined_alpha_returns.csv
combined_alpha_daily_returns.csv
alpha_correlation_matrix.csv
alpha_daily_correlation_matrix.csv
alpha_trade_overlap_matrix.csv
alpha_common_trade_count_matrix.csv
alpha_loss_day_overlap_matrix.csv
alpha_independence_scorecard.csv
alpha_marginal_contribution.csv
alpha_independence_manifest.json
```

### 8.5 Interpretation policy

Deletion/merge decisions should mainly depend on:

```text
trade overlap
high correlation plus crowded overlap
marginal contribution
standalone expectancy
trigger/timing similarity
```

Interpretation:

```text
keep_candidate                  -> safe to keep
keep_with_stress_risk_warning   -> keep, but watch shared loss-day risk
keep_but_cluster_weight         -> keep, allocator/HRP should cluster-weight it
manual_review_before_accept     -> inspect manually before promotion
reject_or_merge_candidate       -> likely duplicate; delete, merge, or redesign
```

High daily correlation alone is cluster-weight evidence, not automatic rejection. Same-loss-day overlap is a stress/risk warning only, not alpha duplication by itself.

---

## 9. Portfolio allocator and live manifest gates

Allocator ingestion must consume only deployability-PASS `edge_manifest.json` files. It must never read arbitrary research folders or non-PASS edges as live candidates.

Allocator responsibilities:

```text
ingest PASS edge manifests
build calibrated candidate banks
analyze overlap/correlation/uniqueness
select representative thresholds/candidates
run HRP/portfolio weighting
run prop/stress simulation
export active_edge_universe.json and stress-validated config
```

Live manifest build consumes allocator outputs:

```text
portfolio_allocator/outputs/active_edge_universe.json
portfolio_allocator/outputs/monte_carlo_stress_outputs/selected_candidate_stress_validated_config.json
portfolio_allocator/outputs/monte_carlo_stress_outputs/monte_carlo_stress_validation_manifest.json
portfolio_allocator/outputs/monte_carlo_stress_outputs/stress_gate_report.csv
```

Build live deployment manifest:

```powershell
python .\portfolio_live\00_build_live_deployment_manifest.py
python .\portfolio_live\01_validate_live_environment.py
```

The live manifest must preserve:

```text
final_live_threshold in calibrated_probability space
threshold_source = allocator_hrp_selected_representative_threshold
portfolio_weight
risk caps
edge_manifest_hash
runtime_module
dashboard_may_engineer_edge_features = false
```

Real live trading should remain disabled unless `portfolio_live/live_settings.py` explicitly arms real live mode and stress validation has passed.

---

## 10. Dashboard/live execution contract

The dashboard/live orchestrator may:

```text
build/update live standard_quote_tick bars
load live_manifest.json
import each listed edge runtime module
call runtime.validate_environment()
call runtime.build_features()
call runtime.generate_candidate_events()
call runtime.score()
write run_id-aware audits
export approved signals
```

The dashboard/live orchestrator must not:

```text
engineer edge-specific features manually
choose thresholds
train models
fit calibrators
build labels
override artifact hashes
place trades directly
```

Execution bot may:

```text
consume approved signal schema
validate manifest hashes
deduplicate events/signals
size risk using direct-weight calibrated-AFML rules
apply broker/risk constraints
place broker orders if live manifest allows it
```

Execution bot must not:

```text
score models
build research features
select thresholds
read research folders directly
ignore live_trade_enabled / blockers
```

### 10.1 Research/live spread-parity guard

Every asset's Gate A/B/C/D verdict was earned against Dukascopy's historical
bid/ask spread, not the live prop broker's. `portfolio_live/live_settings.py`'s
`PORTFOLIO_MAX_SPREAD_POINTS_BY_ASSET` is a deliberately generous
catastrophic-blowout ceiling only -- it does not check that live spread
actually resembles what each edge was gated on.

`RESEARCH_COST_BPS_BY_ASSET` in `live_settings.py` holds each asset's exact
Gate A/B `cost_bps` (read verbatim from `config/<asset>_edge_search_ledger.json`,
not re-derived). `spread_parity_guard()` in `portfolio_execution_bot.py`
compares the live tick's own spread, expressed in bps against that tick's own
mid price (no static bps<->points conversion -- points would go stale as
price moves; recomputing from the live mid price every check is the
live-updating equivalent), against `research_cost_bps * RESEARCH_SPREAD_PARITY_MULTIPLIER_BY_ASSET`
(default 1.5x, since research cost_bps is already a p95-p99 band). It keeps a
small bounded rolling window per asset (`RESEARCH_SPREAD_PARITY_WINDOW`,
default 20 readings, never raw tick history, never unbounded) in live state
and blocks NEW entries only -- open positions are untouched -- while the
rolling median spread is running materially above the researched threshold.
It recovers automatically the next time it is called once the rolling median
normalises; there is no separate unblock step. Assets with no
`RESEARCH_COST_BPS_BY_ASSET` entry are never blocked by this guard (nothing
to compare against), only flagged in the diagnostic.

Override `research_cost_bps_by_asset` / `research_spread_parity_multiplier_by_asset`
/ `research_spread_parity_window` / `research_spread_parity_min_observations`
in `portfolio_live/local_live_settings.json` if these ever need broker-specific
tuning without a code change.

---

## 11. Current XAUUSD edge logic summary

These are one-line operating definitions for the current 10 useful XAUUSD long edges:

```text
trend_long
  Positive CUSUM impulse plus verified FFD trend-side confirmation.

absorption_long
  Downside failed displacement / lower-wick absorption / bullish rejection style event.

reversal_long
  Oversold/local fair-value deviation style long reversion event.

rangestructure_long
  Statistical range-structure bias from price/range state rather than manual SMC.

timeregime_long
  Time/regime statistical bias using past-only event state tendencies.

volatilitycarry_long
  Volatility carry / volatility-state continuation bias.

volatilityregime_long
  Volatility-regime state bias using live-reproducible OHLC/regime features.

dailyrangecompletionfade_long
  Downside expected daily range completion/exhaustion fade into long.

trendpullbackcontinuation_long
  Positive medium-term drift plus short-term pullback/reclaim, deliberately different from trend_long impulse.

compressionbreakoutcontinuation_long
  Local compression followed by upside expansion and bullish close quality, deliberately different from daily underfill breakout.
```

Each edge still needs its own Step 00-09 PASS artifacts and independence review. A good story does not override SOTD gates.

---

## 12. What not to do again

Do not:

```text
manually patch runtime.py after training
copy edge artifacts from another edge
manually edit selected_features.json
manually edit model_input_schema.json
create fake edge_manifest.json
set deployability_pass before Step 09 PASS
use raw_probability for final deployment thresholds
let dashboard calculate edge-specific features
let old run outputs pollute current audits
create hidden CSV dependencies
refactor every edge at once
weaken replay/deployability gates because an edge looks profitable
use in-sample predictions for deployment thresholds
force rare-event edges with 200-1000 rows into training just because the idea sounds good
reject same-asset edges only because correlation is high without checking overlap/marginal contribution
use MT5-unavailable volume/order-book data in live event gates
```

If a future exception is genuinely needed, create an ADR under:

```text
docs/decisions/
```

---

## 13. Standard command checklist

From `trading_models_v2`:

```powershell
# Contract checks
python .\shared\edges\edge_yaml_contract.py --edge-root .\assets\<ASSET>\edges\<edge_name>
python .\shared\validation\edge_contracts.py --edge-root .\assets\<ASSET>\edges\<edge_name>
python .\shared\edges\validate_edge_feature_schema.py --asset <ASSET> --family <family> --side <side>
python .\shared\edges\verify_edge_registry.py

# Runtime self-test
python .\assets\<ASSET>\edges\<edge_name>\runtime.py

# Research dataset export
python .\assets\<ASSET>\edges\<edge_name>\export_research_dataset.py --dry-run
python .\assets\<ASSET>\edges\<edge_name>\export_research_dataset.py --overwrite

# Lifecycle
python .\assets\<ASSET>\edges\<edge_name>\research\00_validate_edge_contract.py
python .\assets\<ASSET>\edges\<edge_name>\research\01_feature_surgery_clustered_mda.py
python .\assets\<ASSET>\edges\<edge_name>\research\02_train_meta_model_optuna.py
python .\assets\<ASSET>\edges\<edge_name>\research\03_generate_oof_predictions.py
python .\assets\<ASSET>\edges\<edge_name>\research\03b_oof_threshold_sweep.py
python .\assets\<ASSET>\edges\<edge_name>\research\04_fit_sigmoid_calibrator_and_thresholds.py
python .\assets\<ASSET>\edges\<edge_name>\research\04b_lock_selected_features_and_model_input_schema.py
python .\assets\<ASSET>\edges\<edge_name>\research\05_training_artifact_verifier.py
python .\assets\<ASSET>\edges\<edge_name>\research\06_verify_runtime_scoring_ready.py
python .\assets\<ASSET>\edges\<edge_name>\research\07_runtime_score_smoke_test.py
python .\assets\<ASSET>\edges\<edge_name>\research\08_probability_calibration_afml_replay_audit.py
python .\assets\<ASSET>\edges\<edge_name>\research\09_export_edge_manifest.py

# Promote after Step 09 PASS
python .\shared\edges\promote_edge_status.py --edge-root .\assets\<ASSET>\edges\<edge_name> --status deployability_pass --write
python .\shared\edges\verify_edge_registry.py

# Independence checks
python .\assets\XAUUSD\alpha_independence_lab\run_asset_independence_lab.py --asset XAUUSD --mode research_events --include-non-pass
python .\assets\XAUUSD\alpha_independence_lab\run_asset_independence_lab.py --asset XAUUSD --mode selected_trades
python .\assets\XAUUSD\alpha_independence_lab\run_asset_independence_lab.py --asset XAUUSD --mode selected_trades --threshold-policy override --probability-threshold 0.10

# Live manifest gates after allocator outputs exist
python .\portfolio_live\00_build_live_deployment_manifest.py
python .\portfolio_live\01_validate_live_environment.py
```
