# Quantara MLDP Transformation - Task 2A

## Pre-model executable-outcome engine

Task 2A installs one shared, asset-agnostic and edge-agnostic audit station.
It does **not** yet change Step 01-09 training semantics.

### Methodological basis

AFML Chapter 3 makes triple-barrier labels path-dependent and defines `t1` as
the first barrier touch. When a primary model/rule supplies side, meta-labeling
lets a secondary model decide act versus pass. AFML Chapter 4 then measures
overlap and uniqueness using each label's `[t0,t1]` lifespan.

MLAM Chapter 5 likewise argues that financial labels should reflect the
success/failure of a position, preserve the relevant path information, and use
meta-labeling when side is predetermined.

Quantara's exact historical Bid/Ask resolver is not a canonical AFML/MLAM
algorithm. It is a `QUANTARA_EXTENSION`. Task 2A reuses that resolver rather
than creating a second execution model.

### Why Task 2A is audit-only

AFML and MLAM do not specify what a Quantara target should be when a
historical event has no timely quote because of true market inactivity, missing
historical coverage, a feed defect, or right-censoring. Treating all such cases as
losses would invent semantics. Dropping them blindly could condition the sample
on post-event information.

Task 2A therefore:
- resolves every source event it can;
- assigns `quote_executable_r`, `quote_executable_meta_label`, and
  `quote_executable_t1` only to genuinely resolved rows;
- leaves unresolved target fields missing and records the reason;
- marks outputs `AUDIT_ONLY_TASK2A_NOT_YET_AUTHORIZED`;
- does not authorize Step 01-09 to consume the new target.

The pilot audit will enumerate unresolved reasons before Task 2B freezes one
universal population rule.

### Dynamic scope

The CLI discovers:
`assets/<ASSET>/edges/<family>_<side>/`

No asset, edge family, current edge count, or profitability assumption is
hard-coded. `BTCUSD/absorption_long` is only the first validation specimen.

### Commands

From `trading_models_v2`:

```powershell
python .\build_mldp_executable_targets.py --all --dry-run
python .\build_mldp_executable_targets.py --edge-root .\assets\BTCUSD\edges\absorption_long
```

Do not run Step 01-09 from Task-2A outputs.
