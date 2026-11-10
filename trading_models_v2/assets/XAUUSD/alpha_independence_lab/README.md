# XAUUSD Alpha Independence Lab

This is the asset-local runner for the V2 shared alpha-independence engine.

## Selected-trade independence test

Uses deployability-PASS edge manifests, calibrated OOF predictions, and each edge's selected calibrated-probability threshold artifact.

```powershell
python .\assets\XAUUSD\alpha_independence_lab\run_asset_independence_lab.py --asset XAUUSD --mode selected_trades
```

Outputs are written to:

```text
assets/XAUUSD/alpha_independence_lab/outputs/selected_trades/
```

## Research/candidate event overlap test

Use this before an edge is deployability-PASS, only for duplicate/overlap diagnostics:

```powershell
python .\assets\XAUUSD\alpha_independence_lab\run_asset_independence_lab.py --asset XAUUSD --mode research_events --include-non-pass
```

Outputs are written to:

```text
assets/XAUUSD/alpha_independence_lab/outputs/research_events/
```

## Key outputs

- `alpha_summary.csv`
- `combined_alpha_returns.csv`
- `combined_alpha_daily_returns.csv`
- `alpha_correlation_matrix.csv`
- `alpha_daily_correlation_matrix.csv`
- `alpha_trade_overlap_matrix.csv`
- `alpha_common_trade_count_matrix.csv`
- `alpha_loss_day_overlap_matrix.csv`
- `alpha_independence_scorecard.csv`
- `alpha_marginal_contribution.csv`
- `alpha_independence_manifest.json`

The lab consumes artifacts only. It does not score models, train models, or engineer edge features.
