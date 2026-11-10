# NASDAQ/downsidevolatilitycascade_short

This is a V2 side-specialized alpha edge package.

## Blueprint contract

- One asset: `NASDAQ`
- One family: `trend`
- One side: `short`
- One side value: `-1`
- One candidate event generator
- One meta-model
- One calibrator
- One threshold sweep
- One `runtime.py`
- One `edge_manifest.json`

## Ownership boundary

This edge may build edge-specific features/events/model rows only through its own
`runtime.py` functions. Research and live must call the same runtime functions.

The dashboard must not manually engineer this edge's selected model features.

## Deployability

This edge is not deployable until it has:

1. Complete feature schema with `live_available` and `allowed_for_model_input`.
2. Selected features that are reproducible live.
3. Model/calibrator/threshold artifacts.
4. Probability replay: raw_probability -> calibrated_probability.
5. AFML replay.
6. Deployability audit PASS.
7. `artifacts/edge_manifest.json` with deployment_status=PASS.
