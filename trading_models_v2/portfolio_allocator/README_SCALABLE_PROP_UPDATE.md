# Scalable Prop-Account Update

## Purpose

This replacement package keeps the existing `portfolio_allocator -> portfolio_live` workflow and makes the account/broker selection reusable without creating prop-firm-specific Python scripts.

The design remains deliberately small:

1. `portfolio_allocator/broker_contract_specs.json` is the editable asset-to-symbol/alias seed.
2. Script `00A` overwrites the authoritative MT5 broker snapshot with actual symbol availability and actual contract values.
3. Script `00B` builds the historical currency-conversion inputs required by that broker snapshot.
4. Script `01` excludes every PASS edge whose asset is unavailable on the connected broker.
5. `portfolio_allocator/config/active_account_rules.json` is the one editable account-rule file.
6. Scripts `08`-`11` and the live system consume the same normalized account rules.

There are no filenames such as `goat_*.json`, `ftmo_*.json`, or `atmos_*.json`. The Python scripts do not branch on a prop-firm name.

## Included replacement files

```text
shared/prop_account_rules.py
portfolio_allocator/config/active_account_rules.json
portfolio_allocator/broker_contract_specs.json
portfolio_allocator/00a_capture_mt5_broker_profile.py
portfolio_allocator/01_ingest_pass_edge_manifests.py
portfolio_allocator/08_rolling_start_prop_challenge_optimizer.py
portfolio_allocator/09_fast_manual_variant_lab.py
portfolio_allocator/10_final_candidate_walkthrough.py
portfolio_allocator/11_monte_carlo_stress_validation.py
portfolio_live/live_settings.py
portfolio_live/00_build_live_deployment_manifest.py
portfolio_live/01_validate_live_manifest.py
portfolio_live/02_validate_live_environment.py
portfolio_live/portfolio_live_dashboard_orchestrator.py
portfolio_live/portfolio_execution_bot.py
```

## Before replacing files

Back up the current repository and stop the dashboard, executor, supervisor and MT5-linked Python processes.

```powershell
$root = "C:\Users\wells\Documents\WELLSMART Company\Trading Models\trading_models_v2"
$backup = "$root`_before_scalable_prop_update_$(Get-Date -Format yyyyMMdd_HHmmss)"
Copy-Item $root $backup -Recurse
Write-Host "Backup created: $backup"
```

Extract the replacement ZIP directly over `trading_models_v2` and allow Windows to replace the matching files. The ZIP does not contain credentials, live state, logs, live bars or large allocator outputs.

## The one account-rules file

Edit only:

```text
portfolio_allocator/config/active_account_rules.json
```

Validate it before running the allocator:

```powershell
python .\shared\prop_account_rules.py
```

The supplied file is a generic $5,000 Phase-1 starter profile:

- 8% profit target
- 10% static maximum drawdown
- 4% daily drawdown
- 3 qualifying trading days
- 0.5% minimum daily profit for a qualifying day
- no maximum-risk-per-trade rule
- no daily-symbol-risk rule
- no floating-loss guard during the evaluation

Compare these values with the rules shown on the exact purchased account. Older accounts may retain older terms.

### Phase 2

After Phase 1 passes, edit the same JSON. For example, change:

```json
"account_stage": "phase2"
```

and change only the rules that differ, such as the profit target.

### Funded stage

Edit the same JSON again. Funded-only rules can be enabled through:

```text
maximum_risk_per_trade
daily_symbol_risk
floating_loss_guard
minimum_trading_days
```

For a funded account with no formal target, use the intended payout/operating target for allocator simulation. A finite target is required by Scripts 08-11 so they can measure time-to-objective and pass/failure probabilities.

## Supported account-rule types

### Overall drawdown

```text
static
trailing_fixed_initial_amount
trailing_pct_high_watermark
```

Available breach bases:

```text
balance
equity
lower_of_balance_or_equity
```

Available trailing high-watermark bases:

```text
balance
equity
higher_of_balance_or_equity
```

`trailing_stops_at_initial_balance` controls whether a trailing floor locks at the initial balance.

### Daily drawdown

```text
fixed_initial_amount_from_day_start_balance
pct_of_day_start_balance
pct_of_higher_day_start_balance_or_equity
```

The current simulator uses a UTC reset boundary. Set `reset_hour_utc` to the correct current broker/prop reset hour. Recheck it when daylight-saving treatment may change.

### Minimum trading days

```text
minimum_profit
any_trade
```

For `minimum_profit`, specify `minimum_day_profit_pct` as a percentage of initial capital.

### Optional risk rules

The current generic engine supports:

```text
maximum risk per trade
daily risk per symbol
floating-loss guard
```

A floating-loss guard can either:

```text
block_new_trades
force_close_all
```

The executor only performs `force_close_all` when real-live trading is fully armed. Otherwise it reports that forced closing is required but does not submit orders.

### Operational permissions

The JSON records whether news trading, weekend holding, VPS use and automation are permitted. These are deployment/compliance declarations; the update does not add a news blackout or forced Friday-close gate.

## Broker workflow and missing assets

`broker_contract_specs.json` is only the symbol/alias seed. Its manually written lot and contract values are not authoritative.

Script `00A` connects to the selected MT5 terminal and writes the actual broker values to:

```text
portfolio_allocator/config/broker_profiles/mt5_broker_profile.json
```

The new snapshot separates:

```text
contracts                 captured and usable assets
unavailable_contracts     symbols that were not available
summary.available_assets
summary.missing_assets
```

A partial broker universe is valid. For example, when `HK50` is unavailable but the other assets are captured, Script `00A` reports `PASS` with `availability_status=PARTIAL`.

Script `01` then excludes every HK edge with:

```text
reason = broker_symbol_unavailable
```

The missing broker asset is not classified as a failed model. Because filtering happens before the candidate bank and HRP stages, the remaining portfolio is rebuilt and reweighted without that asset.

Do not run Script `00A` with `--strict` when intentionally accepting a partial broker universe. `--strict` requires all requested assets.

## Hashless behaviour

This update sets:

```python
LIVE_ARTIFACT_HASH_VALIDATION_ENABLED = False
```

Account rules, broker profiles, allocator handoffs and live contracts are validated structurally and by current paths rather than by SHA values.

Some legacy hash-named fields remain in schemas, logs or audit rows for backward compatibility, but they are blank, informational or ignored while hash validation is disabled. They are not account/broker/deployment authority.

## Full rerun for a new broker or changed available-asset universe

Connect the target MT5 terminal first, update the symbol seed file where necessary, then run from `trading_models_v2`:

```powershell
python .\portfolio_allocator\00a_capture_mt5_broker_profile.py
python .\portfolio_allocator\00b_build_historical_conversion_artifacts.py
python .\portfolio_allocator\01_ingest_pass_edge_manifests.py
python .\portfolio_allocator\02_build_edge_candidate_bank.py
python .\portfolio_allocator\03_classify_edge_candidate_bank.py
python .\portfolio_allocator\04b_build_threshold_grid_trade_ledger.py
python .\portfolio_allocator\04bb_validate_geometry_handoff.py
python .\portfolio_allocator\04c_select_representative_threshold_candidates.py
python .\portfolio_allocator\04ca_analyze_rejected_edge_gates.py
python .\portfolio_allocator\05_build_representative_trade_ledger.py
python .\portfolio_allocator\06_build_representative_return_streams.py
python .\portfolio_allocator\07_hrp_linkage_robustness.py
python .\portfolio_allocator\07b_validate_geometry_handoff.py
python .\portfolio_allocator\08_rolling_start_prop_challenge_optimizer.py
python .\portfolio_allocator\09_fast_manual_variant_lab.py --quick-probe
python .\portfolio_allocator\09_fast_manual_variant_lab.py --full-single
python .\portfolio_allocator\10_final_candidate_walkthrough.py
python .\portfolio_allocator\11_monte_carlo_stress_validation.py
```

Then rebuild the live handoff:

```powershell
python .\portfolio_live\00_build_live_deployment_manifest.py
python .\portfolio_live\01_validate_live_manifest.py
python .\portfolio_live\02_validate_live_environment.py --require-mt5
python .\portfolio_live\03_live_bar_threshold_diagnostic.py --fresh
python .\portfolio_live\04_freeze_live_bar_thresholds.py --require-pass
```

Because this is a different broker feed, rebuild and validate live continuation:

```powershell
python .\portfolio_live\repair_live_feature_continuation.py --no-cache
python .\portfolio_live\validate_live_feature_continuation_replay.py
```

Then smoke-test in paper mode:

```powershell
python .\portfolio_live\portfolio_live_dashboard_orchestrator.py --once
python .\portfolio_live\portfolio_execution_bot.py --once
```

## Rerun after changing only the phase/account rules

When the broker, symbols and contract specifications are unchanged:

```powershell
python .\shared\prop_account_rules.py
python .\portfolio_allocator\08_rolling_start_prop_challenge_optimizer.py
python .\portfolio_allocator\09_fast_manual_variant_lab.py --quick-probe
python .\portfolio_allocator\09_fast_manual_variant_lab.py --full-single
python .\portfolio_allocator\10_final_candidate_walkthrough.py
python .\portfolio_allocator\11_monte_carlo_stress_validation.py
python .\portfolio_live\00_build_live_deployment_manifest.py
python .\portfolio_live\01_validate_live_manifest.py
python .\portfolio_live\02_validate_live_environment.py --require-mt5
```

Scripts 03/04 and continuation reconstruction are not normally required when only the phase rules changed and the broker feed/bar policy did not change.

## Critical live-state rule

Before using a new prop account, new MT5 login, or independently reset phase, stop the executor and archive:

```text
portfolio_live/live_state/portfolio_execution_bot_state.json
```

Do not reuse another account's balance history, cursor, Goat Guard count, daily state or high-water mark.

The executor also stores the normalized account contract in state. A structural rules mismatch fails closed rather than silently migrating an old state into a new account profile.

Set these three values accurately in `active_account_rules.json` before rebuilding the live manifest:

```text
initial_balance
current_balance
current_high_watermark
```

The active JSON now overrides stale starting-capital, stage and high-watermark seeds that may still exist in `local_live_settings.json`.

## Validation completed on the replacement package

Completed without the target MT5 terminal or multi-million-row allocator ledgers:

```text
Python compilation for every modified source file
active-account JSON load and validation
static and trailing drawdown calculations
daily drawdown calculation
minimum qualifying-day target gate
invalid floating-guard action rejection
allocator 08-11 command/import smoke checks
synthetic Script 08 pass and daily-drawdown failure paths
synthetic partial broker-availability capture
synthetic generic live account-contract validation
synthetic executor static/daily account-rule derivation
```

A full Script 00A broker capture, complete Scripts 01-11 run, live threshold build and dashboard/executor shadow session must still be performed on the local machine/VPS with the real MT5 terminal and complete repository data.
