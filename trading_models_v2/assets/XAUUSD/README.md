# XAUUSD V2 Asset Folder

This folder follows the V2 Blueprint asset-level contract.

## Ownership boundaries

- `raw/ticks/` contains this asset's raw quote tick files or local tick-file links.
- `bars/standard_quote_tick/` contains this asset's shared V2 standard quote-tick bars.
- `edges/` contains side-specialized edge packages such as `trend_long` and `trend_short`.

## Non-negotiable rule

Bars belong to the asset. Edge-specific features, candidate events, model rows,
models, calibrators, thresholds, and deployability manifests belong inside each
edge package.

The dashboard must not manually engineer edge-specific features.
