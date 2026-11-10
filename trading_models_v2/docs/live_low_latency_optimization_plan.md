# V2 Live Low-Latency Optimization Plan

Prepared for the V2 portfolio live dashboard after confirmed stale-signal latency in production logs.

## Current confirmed problem

The dashboard is functionally correct but still behaves like a batch recompute orchestrator:

1. Poll ticks and form live quote-tick bars.
2. When one or more executable bars close, score selected assets.
3. For each asset, score each selected edge sequentially.
4. For each edge, rebuild full runtime features over the combined memory window.
5. Generate all candidate events.
6. Filter recent events and score only the recent rows.
7. Apply timestamp/stale/probability gates.

The latest event-score cap fixed repeated scoring of hundreds of old event rows, but stale signals still occur because the expensive work happens before the cap: full feature construction, full event generation, and sequential edge processing.

## Non-negotiables

- Do not loosen timestamp gates.
- Do not increase stale age as the primary fix.
- Do not let the dashboard manually engineer edge-specific alpha features.
- Do not change selected model features, thresholds, AFML sizing, or execution bot risk gates.
- Do not let MT5 tick/bar ingestion wait for model scoring in the final architecture.
- Every speed optimization must preserve research/live parity and fail closed.

## Stage 1 — Observability and latency attribution

Status: implemented.

File:

```text
portfolio_live/01_analyze_live_dashboard_latency.py
```

Purpose:

- Parse `portfolio_live_dashboard_orchestrator.log`.
- Measure `RUNTIME_SCORE_START -> RUNTIME_EVENT_SCORE_FILTER`.
- Measure `RUNTIME_EVENT_SCORE_FILTER -> RUNTIME_SCORE_DONE`.
- Measure `RUNTIME_SCORE_DONE -> SIGNAL_CHECK`.
- Measure `event_time -> SIGNAL_CHECK log time`.
- Identify worst edges/assets/stages causing stale events.

Run:

```powershell
cd "C:\Users\wells\Documents\WELLSMART Company\Trading Models\trading_models_v2"
python .\portfolio_live\01_analyze_live_dashboard_latency.py
```

Outputs:

```text
portfolio_live/outputs/live_dashboard_latency/edge_signal_latency_report.csv
portfolio_live/outputs/live_dashboard_latency/edge_stage_latency_report.csv
portfolio_live/outputs/live_dashboard_latency/summary.json
```

## Stage 2 — Dashboard hot-path latency instrumentation

Add live `LIVE_LATENCY_STAGE` log lines inside the dashboard hot path without changing behavior.

Target stages:

- `combined_bars_for_asset`
- `latest_live_executable_timestamps`
- `get_runtime`
- `runtime_build_features`
- `runtime_generate_candidate_events`
- `filter_recent_events_for_runtime_score`
- `runtime_score`
- `validate_runtime_score_output`
- `timestamp_cursor_live_gate`
- `export_runtime_score_rows`
- `score_edge_total`

This gives exact stage timings for future live runs, not only approximate timings inferred from current log lines.

## Stage 3 — Build combined bars once per asset per scoring batch

Current issue:

- `score_edge()` calls `combined_bars_for_asset()` per edge.
- For 8 edges on one asset, the same seed/live bars can be loaded and standardized 8 times.

Fix:

- Build one bars snapshot per asset in `score_selected_assets()`.
- Pass the snapshot into `score_edge()`.
- Do not change feature/event/scoring logic.

Expected benefit:

- Lower file I/O and pandas standardization overhead.
- Lower latency before edge feature building starts.

## Stage 4 — Per-asset edge parallelism

Current issue:

- Edges are scored sequentially.

Fix:

- Dispatch edge scoring jobs for the same closed asset concurrently.
- Keep MT5 access, state mutation, and output writing in the main process.
- Workers only receive immutable bars snapshots and edge config, then return signal rows/diagnostics.

Expected benefit:

- Reduce total asset scoring latency from sum(edge times) to near max(edge times) plus overhead.

## Stage 5 — Live event generation API

Current issue:

- Each runtime generates all historical events, then the dashboard filters.

Fix:

Add optional runtime API:

```python
generate_candidate_events_live(features, live_event_times, last_processed)
```

Fallback:

- If an edge does not implement it, keep existing `generate_candidate_events(features)`.

Expected benefit:

- Avoid building hundreds of historical events when only the latest executable bar can trade.

## Stage 6 — Asset-level base feature cache

Current issue:

- Each edge recomputes many common OHLC/TA/FFD/HMM features.

Fix:

- Compute reusable asset-level base features once per asset/bar-close.
- Edge runtimes continue to own edge-specific alpha/event/model-row logic.
- Shared feature cache must be produced by shared runtime modules, not dashboard-side manual alpha engineering.

Expected benefit:

- Large reduction in duplicate feature work across edges.

## Stage 7 — Incremental live feature state

Endgame fix:

- Do not recompute 8,000 rows on every bar.
- Maintain rolling state buffers and update only the newest closed bar.

Required validation:

- Incremental row must replay-match full recompute row for selected features before any edge is deployable through this path.

## Stage 8 — Market data/scoring decoupling

Final architecture:

- Tick/bar engine must never wait for scoring.
- Bar-close events go into a queue.
- Scoring workers consume queue jobs.
- Signal writer applies final gates and writes approved signals immediately.

This is the professional low-latency architecture that removes stale events caused by scoring blocking tick ingestion.
