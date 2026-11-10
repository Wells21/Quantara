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

## Complete `active_account_rules.json` parameter reference

### Percentage format

All percentages in the JSON use decimal form:

```text
0.10  = 10%
0.08  = 8%
0.04  = 4%
0.005 = 0.5%
0.002 = 0.2%
```

Do not enter `8` when the intended value is 8%. Enter `0.08`.

The authoritative file is:

```text
portfolio_allocator/config/active_account_rules.json
```

Run this after every edit:

```powershell
python .\shared\prop_account_rules.py
```

A validation failure means the allocator and live system should not be started.

---

### Top-level identification and account-state fields

#### `schema_version`

```json
"schema_version": "v1"
```

This identifies the structure expected by `shared/prop_account_rules.py`.

Current accepted value:

```text
v1
```

Do not change it unless the shared loader is upgraded to another schema.

#### `profile_label`

```json
"profile_label": "current_prop_account"
```

A human-readable label stored in manifests, simulation outputs and logs.

It does **not** select special Python logic and does not need to contain the prop-firm name. The scripts never branch on this field.

Suitable examples:

```text
current_prop_account
phase1_current_account
funded_current_account
```

Keep it stable during one complete 08-11 run so the outputs remain easy to identify.

#### `description`

```json
"description": "Current independent account simulation."
```

Free-text notes for the user. It does not change calculations.

Use it to record anything useful, such as:

```text
GOAT 5K evaluation rules verified from dashboard on 2026-07-20.
```

#### `account_stage`

```json
"account_stage": "phase1"
```

A descriptive label for the independent stage currently being tested or traded.

Examples:

```text
phase1
phase2
funded
challenge
evaluation
```

The loader accepts any non-empty string. It does not automatically load different rules. You must change the actual profit target, drawdown and optional rules yourself.

The stage is carried into the allocator outputs, live manifest, approved signals and executor state. Changing it requires rerunning Scripts 08-11 and Live Scripts 00-02.

#### `initial_balance`

```json
"initial_balance": 5000.0
```

The nominal starting capital assigned by the prop firm for the current independent stage.

It is used to calculate:

```text
profit-target balance
static maximum-loss amount
fixed trailing-loss distance
minimum qualifying-day profit amount
maximum-risk-per-trade amount
daily-symbol-risk amount
floating-loss-guard amount
```

For a fresh $5,000 phase:

```json
"initial_balance": 5000.0
```

Do not replace it with current equity after gaining or losing money. It remains the original account size for that stage.

#### `current_balance`

```json
"current_balance": 5000.0
```

The account's current **closed balance** at the point from which the simulation or live deployment begins.

For a new phase:

```text
current_balance = initial_balance
```

For an account already in progress, use the actual current MT5 balance after all closed P/L, commissions and swaps.

Example:

```json
"initial_balance": 5000.0,
"current_balance": 5175.40
```

Do not use floating equity here.

#### `current_high_watermark`

```json
"current_high_watermark": 5000.0
```

The highest historical value reached by the source selected in:

```text
overall_drawdown.high_watermark_basis
```

This matters primarily for trailing-drawdown accounts.

It must never be lower than:

```text
initial_balance
current_balance
the true previously observed high-water mark
```

Examples:

```text
Fresh static account:
initial_balance = 5000
current_balance = 5000
current_high_watermark = 5000

Trailing account that previously reached 5400 and later fell to 5200:
initial_balance = 5000
current_balance = 5200
current_high_watermark = 5400
```

Never lower this number to create artificial drawdown room. For a static account it does not move the static floor, but keeping the true value prevents unsafe state migration if the profile later changes.

#### `assume_fresh_daily_reset_at_simulation_start`

```json
"assume_fresh_daily_reset_at_simulation_start": true
```

Records the assumption that each rolling simulation starts at the beginning of a fresh prop trading day, with the daily reference initialized from the configured current account state.

Accepted values:

```text
true
false
```

For a new phase or a simulation started immediately after the prop daily reset, use:

```json
true
```

In the current allocator implementation, rolling starts already initialize a new daily bucket; this field is mainly preserved in the output contract as an explicit assumption. Leave it `true` unless the simulation code is later extended to import an intraday daily-loss state.

---

### `profit_target`

Example:

```json
"profit_target": {
  "enabled": true,
  "pct": 0.08,
  "stop_immediately_when_eligible": true
}
```

#### `profit_target.enabled`

Controls whether the run has a finite balance objective.

```text
true  -> target is active
false -> no pass/objective can be reached through the target gate
```

Scripts 08-11 are designed to calculate pass probability and time to an objective. Therefore, normally keep this enabled.

For a funded account with no formal prop target, enter the payout or operating objective you want the simulation to test rather than disabling it.

#### `profit_target.pct`

The target profit as a percentage of `initial_balance`.

Formula:

```text
target_balance = initial_balance × (1 + pct)
```

Example:

```text
initial_balance = $5,000
pct             = 0.08
target_balance  = $5,400
```

#### `profit_target.stop_immediately_when_eligible`

Accepted values:

```text
true
false
```

`true` means the simulated account stops as soon as both conditions are satisfied:

```text
target balance reached
minimum trading-day requirement satisfied
```

`false` allows the simulation to continue after first becoming eligible. For ordinary challenge testing, use `true`.

---

### `overall_drawdown`

Example:

```json
"overall_drawdown": {
  "enabled": true,
  "type": "static",
  "pct": 0.10,
  "breach_basis": "lower_of_balance_or_equity",
  "high_watermark_basis": "balance",
  "trailing_stops_at_initial_balance": false
}
```

#### `overall_drawdown.enabled`

```text
true  -> enforce maximum overall loss
false -> disable the overall-loss floor
```

A prop evaluation should normally use `true`.

#### `overall_drawdown.pct`

The permitted maximum loss in decimal form.

For a 10% maximum loss:

```json
"pct": 0.10
```

For `static` and `trailing_fixed_initial_amount`, the fixed loss amount is:

```text
initial_balance × pct
```

Example:

```text
$5,000 × 10% = $500
```

#### `overall_drawdown.type`

Accepted values:

##### `static`

The floor never rises with profits.

Formula:

```text
floor = initial_balance - (initial_balance × pct)
```

Example:

```text
initial balance = $5,000
maximum loss    = 10%
floor           = $4,500
```

Even after the account reaches $5,500, the floor remains $4,500.

Choose this when the prop rule says:

```text
static drawdown
fixed maximum loss
absolute loss limit
```

##### `trailing_fixed_initial_amount`

The loss distance is fixed from the initial account size, but the floor follows the configured high-water mark.

Formula:

```text
loss distance = initial_balance × pct
floor         = high_watermark - loss distance
```

Example:

```text
initial balance = $5,000
pct             = 10%
loss distance   = $500
high-water mark = $5,400
floor           = $4,900
```

Choose this when the prop says the drawdown trails by a fixed dollar amount.

##### `trailing_pct_high_watermark`

The floor is recalculated as a percentage below the current high-water mark.

Formula:

```text
floor = high_watermark × (1 - pct)
```

Example:

```text
high-water mark = $5,400
pct             = 10%
floor           = $4,860
```

Choose this only when the prop rule explicitly calculates the allowed loss as a percentage of the latest high-water mark.

#### `overall_drawdown.breach_basis`

Determines which account value is compared with the overall floor.

Accepted values:

##### `balance`

Uses only closed balance.

Floating profit or loss does not create a breach until a position closes.

Choose this only when the prop explicitly says the overall limit is balance-based.

##### `equity`

Uses equity, including floating P/L.

In historical allocator simulations, exact intratrade equity is unavailable, so the conservative equity proxy is:

```text
closed balance - total open stop risk
```

In live execution, the MT5 account equity is used.

##### `lower_of_balance_or_equity`

Uses:

```text
min(balance, equity)
```

This fails if either balance or equity falls below the floor and is the most conservative option.

Choose it when the firm says both balance and equity are relevant, or when wording is ambiguous and you prefer to fail closed.

#### `overall_drawdown.high_watermark_basis`

Used only to update a trailing high-water mark.

Accepted values:

##### `balance`

Only realized closed profits can raise the high-water mark.

Floating profit does not tighten the trailing floor.

##### `equity`

Floating equity peaks can raise the high-water mark, even before profit is closed.

This is more restrictive and requires the true highest historical equity.

##### `higher_of_balance_or_equity`

Uses whichever is higher at each observation:

```text
max(balance, equity)
```

Choose this when the prop explicitly says the trailing reference is the higher of balance or equity.

For a static account, this setting is carried for consistency but does not change the static floor.

#### `overall_drawdown.trailing_stops_at_initial_balance`

Accepted values:

```text
true
false
```

`true` means the trailing floor rises only until it reaches the original account balance, then locks there and never rises above it.

Example:

```text
initial balance = $5,000
raw trailing floor reaches $5,050
effective floor remains $5,000
```

`false` means the floor continues trailing upward without that cap.

This field has no practical effect when `type` is `static`.

---

### `daily_drawdown`

Example:

```json
"daily_drawdown": {
  "enabled": true,
  "pct": 0.04,
  "mode": "pct_of_higher_day_start_balance_or_equity",
  "breach_basis": "lower_of_balance_or_equity",
  "reset_timezone": "UTC boundary corresponding to the prop rule",
  "reset_hour_utc": 22
}
```

#### `daily_drawdown.enabled`

```text
true  -> enforce the daily loss rule
false -> do not simulate or enforce a daily floor
```

#### `daily_drawdown.pct`

Daily loss percentage in decimal form.

Example:

```json
"pct": 0.04
```

means 4%.

#### `daily_drawdown.mode`

Accepted values:

##### `fixed_initial_amount_from_day_start_balance`

The daily loss amount is always a fixed percentage of the original account size, subtracted from the day-start balance.

Formula:

```text
loss amount = initial_balance × pct
daily floor = day_start_balance - loss amount
```

Example:

```text
initial balance  = $5,000
day-start balance = $5,200
pct               = 4%
fixed loss amount = $200
daily floor        = $5,000
```

Choose this when the firm says the daily loss amount is always a fixed percentage of initial capital.

##### `pct_of_day_start_balance`

The allowed daily loss changes with the balance at the start of each prop day.

Formula:

```text
loss amount = day_start_balance × pct
daily floor = day_start_balance × (1 - pct)
```

Example:

```text
day-start balance = $5,200
pct               = 4%
daily floor       = $4,992
```

##### `pct_of_higher_day_start_balance_or_equity`

The daily reference is the higher of balance or equity at the daily reset.

Formula:

```text
reference   = max(day_start_balance, day_start_equity)
loss amount = reference × pct
daily floor = reference × (1 - pct)
```

Example:

```text
day-start balance = $5,200
day-start equity  = $5,300
reference         = $5,300
pct               = 4%
daily floor       = $5,088
```

Choose this when the prop explicitly says the daily calculation uses the higher of starting balance or starting equity.

#### `daily_drawdown.breach_basis`

Determines the live/current value compared with the daily floor.

Accepted values:

```text
balance
equity
lower_of_balance_or_equity
```

Their meanings are the same as the overall-drawdown breach bases:

```text
balance                     -> closed balance only
equity                      -> floating P/L included
lower_of_balance_or_equity  -> breach if either is below the floor
```

#### `daily_drawdown.reset_timezone`

A human-readable description carried into outputs and logs.

Example:

```json
"reset_timezone": "America/New_York 17:00 broker reset"
```

Important: the current engine does **not** convert this text into a timezone. It is documentation only.

#### `daily_drawdown.reset_hour_utc`

The actual hourly reset used by the allocator and live contract.

Accepted integer range:

```text
0 through 23
```

Examples:

```text
0  -> 00:00 UTC
21 -> 21:00 UTC
22 -> 22:00 UTC
```

If the prop uses a local reset affected by daylight saving, convert it to the correct current UTC hour and update this field when the UTC relationship changes.

The engine currently supports whole-hour boundaries only. A reset such as `21:30 UTC` is not represented by this field.

---

### `minimum_trading_days`

Example:

```json
"minimum_trading_days": {
  "enabled": true,
  "required_days": 3,
  "day_count_mode": "minimum_profit",
  "minimum_day_profit_pct": 0.005
}
```

#### `minimum_trading_days.enabled`

```text
true  -> target cannot pass until enough qualifying days exist
false -> trading-day count is ignored
```

#### `minimum_trading_days.required_days`

The number of separate prop trading days required.

Example:

```json
"required_days": 3
```

#### `minimum_trading_days.day_count_mode`

Accepted values:

##### `minimum_profit`

A day qualifies only when its **realized balance gain** from the day-start balance reaches the configured minimum.

Floating profit does not qualify the day.

##### `any_trade`

A day qualifies once at least one trade is admitted by the simulation on that trading day, regardless of the day's final profit.

Use this only when the firm defines a trading day simply by activity.

#### `minimum_trading_days.minimum_day_profit_pct`

Used by `minimum_profit`.

The qualifying-day amount is:

```text
initial_balance × minimum_day_profit_pct
```

Example:

```text
initial balance = $5,000
pct             = 0.005
required gain   = $25
```

When `day_count_mode` is `any_trade`, this field is retained but does not determine qualification.

---

### `maximum_risk_per_trade`

Example:

```json
"maximum_risk_per_trade": {
  "enabled": false,
  "pct": null
}
```

#### `maximum_risk_per_trade.enabled`

```text
true  -> apply an additional prop-imposed cap to every trade
false -> use only the portfolio's selected risk caps
```

#### `maximum_risk_per_trade.pct`

The legal fixed maximum risk amount as a percentage of initial balance.

Formula:

```text
maximum trade risk amount = initial_balance × pct
```

Example:

```text
initial balance = $5,000
pct             = 0.01
maximum risk    = $50
```

The cap is applied after broker-aware lot sizing. A trade is rejected when even the minimum broker lot cannot fit safely beneath the applicable cap.

Use `null` when the rule is disabled:

```json
"enabled": false,
"pct": null
```

---

### `daily_symbol_risk`

Example:

```json
"daily_symbol_risk": {
  "enabled": false,
  "max_pct": null,
  "internal_operating_pct": null
}
```

This rule limits daily risk attributed to one symbol/asset.

The current usage calculation is conservative:

```text
same-day closed losses on the symbol
+
floating stop risk currently open on the symbol
```

#### `daily_symbol_risk.enabled`

```text
true  -> enforce the rule
false -> do not apply it
```

#### `daily_symbol_risk.max_pct`

The firm's legal hard cap, calculated from initial balance.

Example:

```text
initial balance = $5,000
max_pct         = 0.01
legal cap       = $50 per symbol/day
```

#### `daily_symbol_risk.internal_operating_pct`

A stricter safety limit used for new-trade admission before reaching the legal cap.

It must be less than or equal to `max_pct`.

Example:

```json
"max_pct": 0.01,
"internal_operating_pct": 0.009
```

means:

```text
legal cap      = 1.0%
operating cap  = 0.9%
safety reserve = 0.1%
```

When no safety reserve is needed, set both values equal.

When the rule is disabled, set both to `null`.

---

### `floating_loss_guard`

Example:

```json
"floating_loss_guard": {
  "enabled": false,
  "pct": null,
  "internal_operating_pct": null,
  "action": "block_new_trades"
}
```

This is a total-account floating/open-risk guard, separate from overall and daily drawdown.

#### `floating_loss_guard.enabled`

```text
true  -> enforce the guard
false -> do not apply it
```

#### `floating_loss_guard.pct`

The legal guard amount as a percentage of initial balance.

Formula:

```text
guard amount = initial_balance × pct
```

Example:

```text
initial balance = $5,000
pct             = 0.02
guard amount    = $100
```

#### `floating_loss_guard.internal_operating_pct`

A stricter proactive limit for admitting new trades.

It must be less than or equal to `pct`.

Example:

```json
"pct": 0.02,
"internal_operating_pct": 0.016
```

means the executor should stop adding risk around 1.6%, leaving room before the legal 2% guard.

#### `floating_loss_guard.action`

Accepted values:

##### `block_new_trades`

No additional trades are admitted when the guard has no remaining capacity. Existing positions remain open under their broker SL/TP and ordinary management.

##### `force_close_all`

When the legal guard is reached, the fully armed live executor attempts to close all open positions.

In paper or dry-run mode, the system reports that forced closure is required but does not send live close orders.

Use this only when the prop rule itself forces liquidation, such as a funded-stage floating-loss guard.

---

### `operational_permissions`

Example:

```json
"operational_permissions": {
  "news_trading_allowed": true,
  "weekend_holding_allowed": true,
  "vps_allowed": true,
  "automation_allowed": true
}
```

All four fields accept:

```text
true
false
```

They document whether the selected account permits:

```text
news trading
weekend holding
VPS use
automated trading
```

These are currently compliance declarations. They are written into account contracts and validation outputs, but this update does not automatically:

```text
block high-impact news
force Friday closing
detect VPS location
detect third-party automation
```

Therefore:

- Set each field truthfully.
- Do not assume `false` creates an automatic operational gate.
- Do not deploy the model on an account whose permissions conflict with the intended architecture.

---

### Common profile examples

#### Static evaluation account

```json
{
  "initial_balance": 5000.0,
  "current_balance": 5000.0,
  "current_high_watermark": 5000.0,
  "profit_target": {
    "enabled": true,
    "pct": 0.08,
    "stop_immediately_when_eligible": true
  },
  "overall_drawdown": {
    "enabled": true,
    "type": "static",
    "pct": 0.10,
    "breach_basis": "lower_of_balance_or_equity",
    "high_watermark_basis": "balance",
    "trailing_stops_at_initial_balance": false
  }
}
```

#### Fixed-distance trailing account that locks at starting balance

```json
"overall_drawdown": {
  "enabled": true,
  "type": "trailing_fixed_initial_amount",
  "pct": 0.08,
  "breach_basis": "equity",
  "high_watermark_basis": "balance",
  "trailing_stops_at_initial_balance": true
}
```

#### Funded account with a 2% floating-loss guard

```json
"floating_loss_guard": {
  "enabled": true,
  "pct": 0.02,
  "internal_operating_pct": 0.016,
  "action": "force_close_all"
}
```

The exact operating percentage should come from simulation and should remain below the firm's legal threshold.


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

### Is it safe to clear `portfolio_live/live_state` when moving to another prop firm?

**Yes. A new prop firm, MT5 login or independently reset account must not inherit the old account's live state.**

The current dashboard and executor recreate fresh state files automatically when their state JSON files do not exist.

However, do not clear the folder while:

```text
dashboard, executor or supervisor is running
the old account still has open positions
the old account still has pending orders managed by this system
```

The executor state contains more than balance history. It can contain:

```text
processed signal IDs
open-position metadata
position-to-edge attribution
vertical-exit tracking
daily prop-risk state
equity high-water mark
floating-guard state
preferred MT5 filling modes
transient order retry records
```

Deleting it while old positions remain can remove the metadata needed to manage those positions correctly.

### Correct migration sequence

1. Stop the supervisor, dashboard and executor.
2. Confirm the old account has no open positions or pending orders that the model still needs to manage.
3. Archive the old runtime files rather than permanently deleting them immediately.
4. Recreate empty `live_state` and `live_bars` directories.
5. Connect the new prop MT5 terminal.
6. Run the complete new-broker allocator and live sequence.
7. Start the new account in paper mode first.

### Recommended clean reset for a different broker/account

Run from PowerShell after all Python processes have stopped:

```powershell
$root = "C:\Users\wells\Documents\WELLSMART Company\Trading Models\trading_models_v2"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$archive = Join-Path $root "portfolio_live\account_migration_archive\$stamp"

$items = @(
    "portfolio_live\live_state",
    "portfolio_live\live_bars",
    "portfolio_live\outputs\live_dashboard",
    "portfolio_live\outputs\portfolio_execution_bot",
    "portfolio_live\outputs\live_feature_continuation",
    "portfolio_live\outputs\threshold_diagnostics",
    "portfolio_live\config\live_bar_policy.json",
    "portfolio_live\live_manifest.json",
    "portfolio_live\live_deployment_manifest.json",
    "portfolio_live\live_manifest_validation_report.json",
    "portfolio_live\live_environment_validation_report.json"
)

foreach ($relative in $items) {
    $source = Join-Path $root $relative

    if (Test-Path $source) {
        $destination = Join-Path $archive $relative
        New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
        Move-Item $source $destination -Force
        Write-Host "Archived: $relative"
    }
}

New-Item -ItemType Directory -Path "$root\portfolio_live\live_state" -Force | Out-Null
New-Item -ItemType Directory -Path "$root\portfolio_live\live_bars" -Force | Out-Null
New-Item -ItemType Directory -Path "$root\portfolio_live\outputs" -Force | Out-Null
New-Item -ItemType Directory -Path "$root\portfolio_live\config" -Force | Out-Null

Write-Host ""
Write-Host "Old runtime archived to:"
Write-Host $archive
```

This resets account-specific and broker-feed-specific live runtime data while preserving a recovery copy.

### Why more than `live_state` is archived

Moving to another broker can change:

```text
symbol availability
tick history
tick density
broker-time offset
bar thresholds
contract specifications
approved-signal history
position sizing
```

Therefore, keeping old live bars, pending bars, dashboard signals or frozen bar policy can mix two broker environments.

### Do not delete these research artifacts

Do **not** clear:

```text
assets/<ASSET>/edges/<EDGE>/artifacts
assets/<ASSET>/edges/<EDGE>/contracts
assets/<ASSET>/ffd research checkpoints
assets/<ASSET>/regime research checkpoints
trained models
calibrators
selected thresholds
edge manifests
portfolio allocator source scripts
```

Those are immutable research/deployability artifacts, not account runtime state.

The live continuation rebuild uses the research FFD/HMM checkpoints and the new broker's fresh live bars.

### Active account values before rebuilding live

Set these accurately in:

```text
portfolio_allocator/config/active_account_rules.json
```

```text
initial_balance
current_balance
current_high_watermark
account_stage
profit target
drawdown rules
daily reset
optional funded rules
```

For a fresh phase:

```text
current_balance        = initial_balance
current_high_watermark = initial_balance
```

After the reset, rebuild the live manifest and environment from the new Script 11 output. Do not copy an old executor state into the new account.


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
