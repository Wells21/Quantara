# Phase D.4 certification and provider overlay

This directory contains **offline certification tooling**.  Nothing here is a
prop-firm account configuration and the dashboard/executor do not import the
certifier during normal live operation.

## V3 certifier

`phase_d4_consolidated_real_history_certifier_v3.py` is the exact audit script
that produced `portfolio_live/outputs/phase_d4_provider_real_history_certification_v2.json`.
Its historical SHA-256 is recorded for reproducibility only.  It is **not** a
runtime Git/SHA lock and it is **not** something to run every time the dashboard
starts.

Rerun the consolidated certifier when the selected edge universe, candidate or
primary mechanics/providers, selected model-input contract, or downstream model
scoring/decision chain materially changes.  A prop-account switch by itself is
an account/risk/deployment workflow and does not require this research
certification unless it also changes those strategy/model authorities.

## Provider overlay modes

The live provider overlay is controlled separately by:

`portfolio_live/config/phase_d/provider_overlay_activation.json`

- file absent: overlay is inert; current production path is unchanged
- `SHADOW`: provider state advances and is compared to source authority, but the
  existing D.3 authority remains the trading output
- `FAST_CONTINUATION`: provider candidate+primary events feed the existing D.3
  downstream scorer; D.3/dashboard/allocator/risk/executor remain authoritative

The example activation file is intentionally named `.example.json` so merely
committing it cannot activate production.
