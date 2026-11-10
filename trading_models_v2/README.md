# Trading Models V2

> **START HERE (current state, 2026-10-23):** `docs/mldp/AFML_MLAM_WALKTHROUGH.md` — where Quantara
> stands, what has been implemented, and where to continue. The "Current state" / "Start here"
> sections below describe the early 2026 pilot and are historical.

V2 is a production-grade MLDP/AFML research-to-live rebuild governed by the saved **Blueprint**: the V2.1 Multi-Asset Multi-Alpha Trading System Blueprint.

Core rule:

> No edge is deployable unless the same edge runtime can reproduce its exact selected model input rows live and pass the deployability audit.

V2 is not a patch of V1. Useful V1 logic is preserved only when it is moved behind V2 contracts: side-specialized edge packages, runtime parity, schemas, manifests, calibrated probability, AFML sizing, allocator gates, and live/execution boundaries.

---

## Current state

Current completed pilot edge:

```text
assets/XAUUSD/edges/trend_long/
```

Current status:

```text
edge_manifest.json: deployment_status=PASS
runtime scoring: implemented
training artifact verifier: PASS
runtime score smoke test: PASS
selected-row/probability/calibration/AFML replay audit: PASS
```

Important boundary:

```text
The edge is deployability-PASS for allocator ingestion.
It is not the final live system by itself.
```

The edge manifest exposes `0.52` as the **edge default calibrated candidate threshold** from Step 04. That value is not the final live portfolio threshold. The final live threshold must be selected or confirmed by the V2 allocator through representative threshold/plateau compression, HRP, overlap-aware simulation, and live manifest export.

---

## Start here

Read these in order:

```text
docs/00_MASTER_WORKFLOW.md
docs/XAUUSD_TREND_LONG_STATUS.md
docs/V2_TRAINING_PHASE_RUNBOOK.md
docs/ARCHITECTURE_CONTRACTS.md
```

---

## Non-negotiable architecture rules

1. **Asset-level bars**

   Raw quote ticks belong to the asset:

   ```text
   assets/{ASSET}/raw/ticks/
   ```

   Shared bars also belong to the asset:

   ```text
   assets/{ASSET}/bars/standard_quote_tick/
   ```

   Edges under the asset consume those shared bars.

2. **Side-specialized edges**

   Each alpha edge is one asset, one family, and one side:

   ```text
   assets/{ASSET}/edges/{family}_{side}/
   ```

   Examples:

   ```text
   assets/XAUUSD/edges/trend_long/
   assets/XAUUSD/edges/trend_short/
   assets/BTCUSD/edges/absorption_long/
   assets/USTECH/edges/time_regime_short/
   ```

3. **Runtime parity**

   Research and live must call the same edge `runtime.py` functions:

   ```text
   load_contract
   validate_environment
   build_features
   generate_candidate_events
   build_model_rows
   load_model
   load_calibrator
   score
   explain_missing_features
   ```

4. **No dashboard-side edge feature engineering**

   The dashboard may orchestrate edge runtimes, but it must not manually create edge-specific selected features. If a feature is missing live, fix the edge runtime/schema. Do not patch the dashboard.

5. **Feature schema contract**

   A selected model feature must be declared, live-safe, non-future, and allowed for model input.

6. **Bar contract**

   Production bars use:

   ```text
   Research_Bar_Mode = standard_quote_tick
   Threshold_Unit = ticks
   Proxy_Value = Ticks
   ```

   `Proxy_Value` is only backward-compatible tick count. It is not volume, dollar value, or fake liquidity.

7. **Probability flow**

   V2 probability flow is:

   ```text
   raw_probability
   -> sigmoid calibrated_probability
   -> AFML bet size
   -> threshold decision using calibrated_probability
   ```

8. **Allocator gate**

   The allocator may consume only deployability-PASS edge manifests. It selects/validates final representative thresholds and risk settings; the edge manifest does not finalize portfolio live deployment.

9. **Execution bot boundary**

   The execution bot consumes approved signals from the live manifest/dashboard path and applies direct-weight calibrated-AFML risk sizing. It must not research, score, engineer features, select thresholds, or select alpha legs.

---

## Current XAUUSD/trend_long command path

From inside `trading_models_v2`:

```powershell
python .\assets\XAUUSD\edges\trend_long\research\05_training_artifact_verifier.py
python .\assets\XAUUSD\edges\trend_long\runtime.py
python .\assets\XAUUSD\edges\trend_long\research\07_runtime_score_smoke_test.py
python .\assets\XAUUSD\edges\trend_long\research\08_probability_calibration_afml_replay_audit.py
python .\assets\XAUUSD\edges\trend_long\research\09_export_edge_manifest.py
```

Expected final Step 09 state:

```text
Deployment status: PASS
Deployable: True
Edge default calibrated threshold: 0.52
Final live threshold source: allocator/live manifest
```

---

## Next aligned Blueprint step

The next architecture step is to expand from one PASS edge to multiple side-specialized edges, starting with:

```text
assets/XAUUSD/edges/trend_short/
```

But before allocator work can be meaningful, each edge must independently pass its own runtime, feature schema, model, calibration, score, replay, and manifest gates.

---

## Documentation rule

Every future code/config change must update documentation in the same step.

Document:

```text
what changed
why it exists
how it aligns with Blueprint
how to run/test it
whether it is scaffold-only, edge-PASS, allocator-ready, or live-manifest-ready
what future component consumes it
```
