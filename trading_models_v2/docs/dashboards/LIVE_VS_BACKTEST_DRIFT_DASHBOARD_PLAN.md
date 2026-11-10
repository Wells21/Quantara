# Quantara Live-vs-Backtest Drift Dashboard — Design Ledger

Status: **V1 BUILT AND RUNNING (2026-09-24).** Serving at
`http://localhost:8765` via `python -m portfolio_live.drift_dashboard.serve`.
Update this file
whenever the design changes; do not let it go stale. Treat it the same way
`RUNBOOK_UPDATE_LOG.md` and the per-asset `*_edge_search_ledger.json` files are
treated — the single source of truth for this effort across sessions.

## Why this exists (do not lose this framing)

2026-09-22: live floating equity moved from +$400 to negative. The user
(correctly) did not manually close positions, but found the swing
emotionally difficult and asked for a dashboard that answers one specific
question with real statistics, not vibes: **is this floating-equity swing
inside or outside what Gates A-D already told us to expect from this
system's own tested variance?**

This is not a "make the user feel better" tool. It must be willing to say
"insufficient live sample — cannot distinguish drift from normal variance
yet" rather than manufacture false confidence. That honesty requirement is
the most important design constraint in this document and must never be
compromised for the sake of a more satisfying-looking chart.

Two distinct failure modes must be told apart, because they call for
different responses:
- **Normal variance within the tested distribution** -> correct response is
  to do nothing, let the edge play out.
- **Real drift** (execution quality degraded, meta-model calibration bent
  away from OOF, realized distribution statistically inconsistent with the
  Gate C/D reference) -> correct response is to investigate the specific
  broken assumption, not to react to any single trade.

## CRITICAL DESIGN PRINCIPLE — dynamic scope, never hardcoded (added 2026-09-24)

Verified directly: of 674 closed trades in `portfolio_closed_trades.jsonl`,
**592 (87.8%) are from edges NOT in the current live-deployed book** (66
distinct `edge_id` values traded historically vs. 21 currently live) — a
mix of stale/prior-version edges, pre-V3 architecture, and edges since
killed/deregistered. Including these would silently corrupt every panel
(confirmed: the first hold-time pass before this filter was applied showed
a 112h max outlier that came entirely from a stale edge). **This is not an
edge case to handle later — it is the first filter applied, before any
other computation.**

**The rule**: the dashboard must never hardcode an edge list, an asset
list, or a "V3 cutoff date." At render/refresh time, it must:
1. Read `portfolio_live/live_manifest.json`'s `selected_edges` field fresh
   — this is the single authoritative source for "which edges currently
   count" (same rule already established elsewhere: "selected edges come
   only from portfolio_live/live_manifest.json"). Handle both plain-string
   and `{edge_id: ...}`-dict list entries (observed both shapes in testing).
2. Filter the closed-trade ledger, the live signal audit, and any other
   live data source to `edge_id IN current_selected_edges` before computing
   anything.
3. On the backtest-reference side, resolve each currently-selected edge's
   Gate C summary file dynamically via glob
   (`assets/<ASSET>/edges/<EDGE>/artifacts/v3_horizon_lab/v3_execution_velocity_summary_*.csv`,
   most recent by filename timestamp) rather than a hardcoded per-asset
   file list — this is what makes adding a new asset or a newly-promoted
   edge require zero dashboard code changes, only a live_manifest.json
   update (which already happens as part of normal promotion).
4. **REVISED 2026-09-24 (supersedes the original point 4 below):** the
   rendered dashboard itself must stay clean and show **only** the current
   21 live-deployed edges — no "excluded stale/unresolved trades" panel,
   table, or footnote cluttering the UI. The three-way split
   (in_scope / excluded_stale / excluded_unresolved) built into
   `drift_dashboard/data_sources.py`'s `ScopedTrades` stays in the data
   layer as a debugging/internal-sanity tool (it's how the 87.7%-stale
   finding was caught and verified) and may be logged to the console when
   the aggregation script runs, but it must **not** appear in the HTML
   report the user actually looks at. If a future session is tempted to add
   an "excluded trades" section back into the rendered dashboard to be
   "more transparent," don't — this was explicitly requested and then
   explicitly walked back by the user in the same conversation; the
   original instinct (below, struck through) was superseded on purpose.
   - ~~Any edge that appears in live trades but not in the current manifest
     should be visible in a small "excluded (stale/non-current) trades"
     footnote/count for transparency — not silently dropped without a
     trace, and not included in any statistic.~~ (superseded 2026-09-24)

This single principle satisfies both of the user's requirements at once:
(a) the dashboard only ever reflects the current, Gate-A-D-passed, V3 book,
and (b) it requires no re-coding when edges or assets are added/removed —
it re-derives its own scope every time it runs.

## Data sources — CONFIRMED 2026-09-24 (research agent survey)

- **Live closed-trade ledger**: `portfolio_live/portfolio_closed_trade_ledger.py`
  writes `portfolio_live/outputs/portfolio_execution_bot/portfolio_closed_trades.jsonl`
  (674 rows as of 2026-09-24 — real usable sample already). Confirmed schema
  per row includes: `position_id, signal_id, edge_id, asset, symbol, side_name,
  entry_time, exit_time, planned_entry_price, actual_entry_price,
  entry_slippage_price, planned_stop_loss, planned_take_profit,
  actual_exit_price, exit_reason (stop_loss/take_profit/vertical_barrier/
  broker_or_manual_close), gross_profit, commission, swap, fee, net_profit,
  planned_risk_usd, realized_r, bars_held, mfe_price, mae_price, mfe_r,
  mae_r, execution_plan_hash, live_manifest_hash, edge_manifest_hash`.
  **MAE/MFE already exist live** (computed post-hoc by walking live bar CSVs
  between entry/exit) — no new instrumentation needed on the live side.
- **Live signal audit**: `live_signal_audit.csv` / `live_approved_signals.jsonl`
  — decision-time record only (signal_price, barrier_value, pt/sl_multiplier,
  planned distances, calibrated_probability, raw_probability, afml_bet_size,
  final_live_threshold). No fill price here by design (pre-trade).
- **Backtest/research reference per edge**:
  - Gate C summary (`artifacts/v3_horizon_lab/v3_execution_velocity_summary_*.csv`)
    is the canonical backtest reference table: `profit_factor_R, win_rate,
    pt_rate/sl_rate/vertical_rate, mean/median/p90/p95/p99 hold_bars & hold_hours,
    max_drawdown_R, max_loss_streak, weekly_cluster_bootstrap_mean_R_95_lo/hi,
    capital_time_velocity_R_per_occupied_day, entry_spread/drift/lag percentiles,
    coverage_rate`. The bootstrap CI here is the authoritative reference
    distribution for the drift-engine's KS-test — use it directly, do not
    re-derive.
  - Task2A resolution CSV (`*_mldp_execution_resolution.csv`) has per-trade
    `realized_r, executed_r, hold_seconds, exit_reason, entry_price, exit_price,
    entry_spread/lag/drift_bps, quote_executable_r` — **confirmed gap: no
    MAE/MFE columns in backtest**, unlike live. Real asymmetry (see Open
    Questions) — MAE/MFE panel will be live-only until/unless backtest MAE/MFE
    is backfilled from research bar data.
  - `artifacts/calibration/*_calibration_report.json` +
    `*_calibration_fold_report.csv` — calibration diagnostics for the
    model-quality panel's OOF reference curve.
- **Slippage — two of three legs exist, not pre-joined**:
  1. `signal_price` -> `plan.entry_price` (decision-tick) drift is computed
     live in `portfolio_execution_bot.py` (~line 4207) but only used for the
     `entry_drift_too_large` guard and **discarded, not persisted**.
  2. `plan.entry_price` -> actual broker fill is captured as
     `entry_slippage_price` in the closed-trade ledger.
  Full signal->fill slippage is reconstructable today by joining
  `live_signal_audit.csv` (by `signal_id`) with the closed-trade ledger — no
  new instrumentation required for v1, just a join. Persisting the discarded
  leg 1 value directly into `make_position_metadata()` would be a clean
  follow-up but is not a blocker.
- **No existing dashboard/serving infrastructure** — no Flask/Streamlit/Dash
  anywhere in the repo; `outputs/live_dashboard/` is a data-drop directory
  only. Decision (revised 2026-09-24): serve over **localhost**, not a bare
  static file the user double-clicks — user explicitly wants to open it at
  `http://localhost:<port>` in a browser. Not internet-facing, no need to
  keep it always running. Use Python's stdlib `http.server` (or an
  equivalent minimal wrapper) serving a directory containing the
  Plotly-rendered HTML report, regenerated by the aggregation script on
  demand/refresh — avoids adding Flask/Streamlit as a new dependency while
  still giving a real localhost URL.

## Section design

1. **Executive overview** — live equity curve rebased against the backtest
   equity path; current drawdown plotted as a percentile of the backtest's
   own bootstrap drawdown distribution (not a raw dollar number); a
   prominent statistical-power/sample-size indicator (this must not be
   buried — it's the honesty check).
2. **Per-edge distributional panel** — realized-R histogram (live vs. Gate C
   bootstrap distribution), hold-time distribution, PT/SL/vertical hit-rate
   breakdown (live vs. backtest), a drift-delta column, and a KS-test
   (or Mann-Whitney, same family as Gate D) p-value per metric.
3. **Execution quality panel** — planned entry vs. actual fill, slippage in
   bps vs. the asset's `RESEARCH_COST_BPS_BY_ASSET` research assumption
   (ties into the existing `spread_parity_guard`), entry lag.
4. **Model-quality panel** — live calibration curve (realized outcome by
   `live_calibrated_probability` bucket — **never** the plain
   `calibrated_probability` column, which is the CPCV-fold-*averaged*
   research diagnostic and does not track live scoring faithfully; see
   `config/mldp_deviation_register.json` entry **D015** and
   `reference_calibrated_probability_columns.md` memory) vs. the Step 04 OOF
   calibration curve; live raw-probability AUC vs. the Gate D reference AUC.
   This is checking whether the *meta-model* still discriminates, which is a
   different question from whether a given trade won.
5. **Capital-velocity panel** — realized R/occupied-day over time vs. each
   edge's Gate C reference R/occupied_day, so a turnover slowdown is visible
   independent of a losing streak.
5b. **Vertical-horizon / hold-time-distribution panel** (added 2026-09-24,
   corrected same day) — VERIFIED across all 13 currently-deployed edges
   with a persisted Gate A grid: the top-3 cells by
   `net_R_per_mean_hold_bar` are unanimously `vertical_horizon=1000` — this
   was genuinely tested against 5/10/15/20 in the same grid and decisively
   won, not skipped. Mechanism: since essentially no trade ever reaches the
   vertical barrier in practice (zero vertical exits across both the full
   674-trade and the current-book 83-trade live samples), extending the cap
   to 1000 costs nothing, while cutting it to 5-20 bars forces real,
   frequent premature exits on trades that were genuinely resolving toward
   TP. **The real gap is the untested region between 20 and 1000** (e.g.
   50/100/200/300/500) — not re-testing 5-20, which lost fairly. This
   panel's job: show each currently-deployed edge's full realized hold-time
   distribution (not just mean/median) so it's visible where an
   intermediate vertical horizon would actually bite the tail vs. needlessly
   cut off already-fine median trades. Known gap: `US500/absorption_long`,
   `US500/candleengulfing_long`, `US500/volatilitycarry_long` have no
   persisted Gate A grid file — cannot confirm the same pattern for these 3
   yet, flag visibly rather than assume.
6. **Drift engine** — the actual statistical tests feeding one verdict per
   edge per metric: `on_distribution` / `insufficient_sample` /
   `drift_flagged`. This is the number the user's eyes should go to first,
   not the raw charts.

## Statistical methodology notes

- Drift test: two-sample KS test (or Mann-Whitney U) comparing live realized
  R sample against the Gate C bootstrap reference distribution — reuses a
  test family already trusted in this codebase (Gate D), not a new
  unfamiliar method.
- Minimum sample size before a drift verdict is issued (not "insufficient
  sample" by default): to be set once real Gate C bootstrap rep counts and
  live trade cadence are known — do not hardcode arbitrarily, derive from
  the actual power needed to detect a meaningful effect size at this
  sample's likely arrival rate.
- Never render a comparison chart as if it were meaningful when the
  underlying sample size does not support a real statistical claim.

## Build/serving approach

Local hosting is fine (per user + ChatGPT discussion) — no need for a
hosted service at this stage. Reuse whatever lightweight serving mechanism
is idiomatic for this repo rather than introducing a new stack; confirm via
the data-source investigation whether anything dashboard-like already
exists to build on top of.

## Open questions (update as resolved)

- [x] Exact closed-trade ledger schema — confirmed, see Data sources above.
- [x] Does MAE/MFE exist anywhere already? — live yes, backtest no (real gap).
- [x] Does live execution already record actual fill price / slippage? —
      yes (`entry_slippage_price`), but full signal->fill needs a join.
- [x] Any existing dashboard-serving code to reuse? — none; building static
      Plotly HTML, regenerated on demand.
- [ ] What minimum live-trade count justifies a real drift verdict per edge,
      given each edge's own Gate C bootstrap rep count? — to be set once the
      drift engine is implemented and can be validated against a null case
      (e.g. resampled backtest-vs-backtest should show "on distribution").

## Verified numbers (current-book-only, 83 trades, for calibration against later dashboard output)

- exit_reason: stop_loss 58, take_profit 23, broker_or_manual_close 2, **zero vertical_barrier** (consistent with the all-674 sample — vertical horizon has never once bound in live trading, current book or not).
- hold_hours: mean 17.2h, median 9.9h, p75 24.6h, max 72.0h.
- `DAX/trend_long` (n=23, the edge active throughout this session): mean 21.5h, median 13.5h hold, mean realized_r -0.269.
- This is the reference snapshot to check the built dashboard against once it's running — if the dashboard's own numbers don't match these, the aggregation logic has a bug.

## Portfolio-level backtest reference (added 2026-09-24, per user request)

User asked explicitly to incorporate `portfolio_allocator/` simulation-level
backtest metrics (max_drawdown etc.), since current live drawdown is causing
real emotional distress and a portfolio-level (not just per-edge) reference
is needed. Found:

- `portfolio_allocator/outputs/backtest_statistics_outputs/backtest_statistics_report.csv`
  — has a row named `PORTFOLIO(hrp_average_core_plus_watchlist_raw_r)` with
  `max_drawdown_r`, `p95_drawdown_r`, `n_drawdown_cycles`,
  `max_time_under_water_years`, `p95_time_under_water_years`. This is the
  historical-return-stream-based portfolio drawdown reference (R units).
- `portfolio_allocator/outputs/monte_carlo_stress_outputs/`:
  - `exact_monte_carlo_summary.csv` — exact-path stress simulation, one row
    per cost-stress scenario, with `worst_buffer_above_floor`,
    `p01_worst_buffer_above_floor`, `p05_worst_buffer_above_floor`,
    `max_daily_drawdown_pct`, `pass_rate`, `hard_fail_rate`,
    `median_trading_days_to_target` — buffer-above-floor is in the same
    dollar/pct terms as the real account's drawdown floor, directly
    comparable to a live computation.
  - `historical_bootstrap_summary.csv` — same metrics but from
    start-month cluster-bootstrap resampling of real historical return
    streams, WITH proper uncertainty bounds (`_median`, `_p025`, `_p975`
    suffixes per metric) — this is the best available reference
    distribution for "is the current drawdown/buffer normal" since it's
    grounded in real history with honest confidence intervals, not a single
    point estimate.
- **Current live buffer-above-floor**, to place on the same scale as the
  above: `portfolio_live/common.py::init_mt5()` gives a live `(mt5, account)`
  connection (reuses the exact same MT5 login/path/server settings the
  execution bot itself uses; multiple simultaneous read-only connections to
  the same MT5 terminal are safe) for real-time `account.balance` /
  `account.equity`. Combine with `shared/prop_account_rules.py`'s
  `overall_drawdown_floor()` / `drawdown_stats()` and
  `portfolio_allocator/config/active_account_rules.json` (the current rules)
  to compute the account's actual current buffer-above-floor on the exact
  same definition the Monte Carlo/bootstrap outputs use.
- **Executive panel design, finalized**: plot current buffer-above-floor as
  a point/marker directly on the historical_bootstrap distribution
  (median/p025/p975 band) for the matching cost-stress scenario. This
  directly answers the emotional question with real numbers: is today's
  drawdown comfortably inside the historically-simulated range, near the
  tail, or genuinely outside it.
- MT5 connection is best-effort with graceful degradation: if
  `init_mt5()` fails (e.g. terminal busy), the executive panel must say so
  explicitly rather than silently show stale/fabricated numbers, and fall
  back to the last known `prop_risk_state.overall_high_watermark` from
  `live_state/portfolio_execution_bot_state.json` with a clear "as of last
  known state, not live" label.

## Vertical-horizon verification (2026-09-24)

User asked me to confirm, before assuming a gap existed, whether V=1000
winning was actually already tested against shorter verticals across the
21 deployed edges. Checked directly against persisted Gate A grids
(`v3_hybrid_fast_grid_*.csv`) for all 21 — 13 had a file, 3 (US500
absorption_long/candleengulfing_long/volatilitycarry_long) did not (gap
noted, not yet explained). Result for all 13 with data: top-3 cells by
`net_R_per_mean_hold_bar` = `[1000, 1000, 1000]`, no exceptions. Conclusion
corrected from "under-tested" to "correctly tested and decisively won, but
the 20-1000 gap itself was never swept" — see section 5b above.

## Real per-trade backtest samples exist — upgrades the drift test (2026-09-24)

Found `assets/<ASSET>/edges/<EDGE>/artifacts/v3_horizon_lab/v3_execution_trades_<CONFIG_LABEL>_<timestamp>.csv`
next to each Gate C summary — genuine per-trade backtest records (e.g.
10,313 rows for DAX/trend_long alone), columns include `entry_ts, exit_ts,
exec_r, exit_reason, hold_bars, entry_spread, sl_dist, tp_dist`. This means
the drift engine can run a REAL two-sample KS test (live realized_r sample
vs. this actual backtest sample) instead of just checking whether the live
mean falls inside a bootstrapped CI band — a materially stronger test.
`config_label` in the trades CSV matches the summary CSV's `label` column
(e.g. `H20_E100_V1000_PT2_SL1`) — resolve which one is the actually-locked
winner by reading the edge's OWN current geometry constants dynamically
(`candidate_events.py`'s VOL_LOOKBACK/VOL_EWM_SPAN, `model_rows.py`'s
VERTICAL_HORIZON/PT_MULTIPLIER/SL_MULTIPLIER) and constructing the matching
label string — never hardcode which row/file is "the winner," since that
would break the moment any edge's geometry is re-locked.

## Implementation progress (2026-09-24)

- `portfolio_live/drift_dashboard/data_sources.py` built and verified against
  real data. Provides: `load_current_live_edge_ids()` (reads
  `live_manifest.json.selected_edges` fresh, handles both plain-string and
  `{edge_id:...}`-dict list entries), `load_closed_trades()`,
  `scope_closed_trades()` (the three-way split — kept as an internal/console
  sanity tool per the REVISED point 4 above, not rendered in the dashboard),
  `resolve_gate_c_summary_path()` / `resolve_gate_a_grid_path()` (dynamic
  glob resolution per edge_id, no hardcoded asset list),
  `load_gate_c_reference()`, `load_live_signal_audit()`.
- **Real data-quality finding surfaced while building this** (not
  hypothetical): of 675 total closed trades, only 83 belong to the current
  21-edge live book. Of the other 592: 219 are attributed to real but
  no-longer-deployed edges ("stale"), and **373 (55% of the whole ledger)
  have `edge_id=None` and `metadata_source="unresolved"`** — the
  closed-trade ledger's own admission it couldn't match these positions to
  any signal/edge at all. Confirmed these are pre-V3 legacy positions on
  symbols (BTCUSD, USDMXN, USOUSD) not even in the current 7-asset universe.
  This distinction (stale-but-known vs. fully-unresolved) matters for
  understanding the data even though neither is shown in the rendered
  dashboard.
- **Real gap surfaced**: 3 of the 21 live edges
  (`US500/absorption_long`, `US500/candleengulfing_long`,
  `US500/volatilitycarry_long`) have no persisted Gate A grid or Gate C
  summary artifact at all. The dashboard must show "no backtest reference
  available" for these edges' backtest-comparison panels rather than
  crashing or silently omitting the edge entirely from the live-side
  panels it can still populate (execution quality, raw hold-time, etc. can
  still show for these 3 from live data alone).
- Next: statistical drift-engine layer (KS-test vs. Gate C bootstrap
  distribution, insufficient-sample guard), then the HTML rendering layer,
  then the localhost `http.server` wrapper.

## V1 build complete (2026-09-24) — file map and how to run

- `portfolio_live/drift_dashboard/data_sources.py` — dynamic scope
  (`live_manifest.json.selected_edges`), closed-trade loading, three-way
  scoping (internal/console only, not rendered).
- `portfolio_live/drift_dashboard/edge_reference.py` — dynamically reads
  each edge's CURRENT geometry constants from its own `candidate_events.py`
  / `model_rows.py` (never a hardcoded/cited label), constructs the Gate C
  `config_label`, resolves the matching `v3_execution_trades_*.csv` per-trade
  backtest sample by glob. Verified against all 21 live edges: 18 resolve
  cleanly (thousands of backtest trades each), 3 US500 edges correctly
  report a gap (no persisted Gate A/C artifacts at all for those 3 — a real,
  pre-existing gap, not a bug in this module).
- `portfolio_live/drift_dashboard/drift_stats.py` — two-sample KS test
  (`scipy.stats.ks_2samp`) comparing live realized_r / hold_hours against
  the real per-trade backtest sample. Verdicts: `insufficient_sample`
  (n<10), `low_power_*` (10<=n<30, informative but caveated),
  `on_distribution` / `drift_flagged` (n>=30). **Real finding on first run**:
  `DAX/trend_long` (n=23 live trades) already shows `low_power_drift_flagged`
  on realized_r (p=0.0028, live mean -0.269 vs backtest +0.082) while
  hold_hours shows `low_power_on_distribution` (p=0.397, in line with
  backtest) — a genuinely differentiated, real signal worth investigating,
  not noise. This is exactly the kind of result the whole dashboard exists
  to surface.
- `portfolio_live/drift_dashboard/portfolio_reference.py` — live account
  snapshot via a fresh, read-only `portfolio_live/common.py::init_mt5()`
  connection (degrades to last-known state-file watermark on failure, never
  fabricates); computes current buffer-above-floor via
  `shared/prop_account_rules.py`; loads the portfolio-level backtest
  reference (Step 07c `backtest_statistics_report.csv` PORTFOLIO row, Step
  11 `historical_bootstrap_summary.csv`/`exact_monte_carlo_summary.csv`).
  Verified units match (`worst_buffer_above_floor` is computed via the same
  `drawdown_stats()` function, confirmed in `11_monte_carlo_stress_validation.py`)
  before treating them as directly comparable — this mattered, since mixing
  R-units and dollar-units here would have produced a misleading chart.
  **Real result on first run**: current live buffer ($1,314.72 as of
  2026-09-24) sits comfortably above the historical-bootstrap reference's
  worst-ever-reached buffer distribution (median -$8.50, p97.5 +$27.40,
  no-cost-stress scenario) — genuinely reassuring, evidence-based, not
  reassurance-for-its-own-sake.
- `portfolio_live/drift_dashboard/build_report.py` — aggregates everything
  into one self-contained Plotly HTML report
  (`portfolio_live/drift_dashboard/report/index.html`). Sections built:
  Executive Overview (buffer-vs-backtest chart + account stats), Per-Edge
  Drift Engine (full 21-row table, color-coded verdict badges, drift-delta),
  Hold-Time Distributions (live vs. backtest histogram overlay per edge with
  sufficient data — currently populates for edges with both a resolvable
  backtest sample and live trades; most edges don't have enough live trades
  yet for a meaningful histogram, which is honest and expected this early).
- `portfolio_live/drift_dashboard/serve.py` — rebuilds the report fresh then
  serves it via stdlib `http.server.ThreadingHTTPServer` on
  `http://localhost:8765` (localhost only, not internet-facing). Run:
  `python -m portfolio_live.drift_dashboard.serve` from the `trading_models_v2`
  directory (or pass `--port` to change it). Ctrl+C to stop. Currently
  running in the background as of this session.

## Known v1 limitations / v2 follow-ups (be honest about these, don't claim they're done)

- Model-quality panel (live_calibrated_probability calibration curve vs.
  Step04 OOF reference) — NOT built yet.
- Capital-velocity time-series panel (R/occupied-day over time vs. Gate C
  reference) — NOT built yet.
- Execution-quality/slippage panel (signal_price -> fill price vs.
  RESEARCH_COST_BPS_BY_ASSET) — NOT built yet; the join logic (via
  signal_id between live_signal_audit.csv and the closed-trade ledger) was
  scoped but not implemented.
- Report file size: embeds full per-trade backtest samples in histogram
  data (10,000+ points for some edges) — fine for the 2 edges that
  currently qualify for a hold-time chart, but will need binning/downsampling
  before embedding once more edges accumulate enough live trades to also
  qualify, to keep the HTML file size reasonable.
- Most edges still show `insufficient_sample` (only 2-7 live closed trades
  each as of 2026-09-24) — this is correct, honest behavior, not a bug to
  fix. The dashboard will get more informative as more trades close
  naturally; don't be tempted to lower `MIN_SAMPLE_FOR_VERDICT` to make it
  "say something" sooner.

## Change log

- 2026-09-24: initial plan drafted, per user request following a live
  floating-equity swing (+$400 -> negative) that was emotionally difficult
  but correctly not acted on.
- 2026-09-24: data sources confirmed via research agent survey. Real finding:
  MAE/MFE exists live but not in backtest resolution CSVs — asymmetry noted,
  MAE/MFE panel will be live-only for v1.
- 2026-09-24: user corrected serving approach — needs a real localhost
  server (open in browser), not a bare static file. Switched to stdlib
  `http.server` serving a directory of Plotly-rendered HTML, regenerated on
  demand — still no new framework dependency.
- 2026-09-24: user flagged (correctly, verified with real data) that most
  closed-trade history is from stale/non-current edges — 592/674 (87.8%)
  are not in the current 21-edge live book. Added the CRITICAL DESIGN
  PRINCIPLE section above: dynamic scope derived from
  `live_manifest.json.selected_edges` at run time, never hardcoded — this
  also satisfies the user's separate requirement that adding new
  edges/assets must never require dashboard code changes. Recomputed the
  reference hold-time numbers on the correctly-filtered 83-trade sample
  (see Verified Numbers section) — conclusion from the unfiltered pass held
  (median ~10h, zero vertical exits) but the 112h max outlier was fully
  explained by contamination from a stale edge, confirming the filter is
  not optional.
  Moving to implementation: build the drift-engine aggregation script first
  (the statistical core, with dynamic scope resolution baked in from the
  start), then the HTML rendering layer, then wire up the localhost server.
- 2026-09-24: user revised the exclusion-visibility decision — the rendered
  dashboard must show ONLY the current 21 live-deployed edges, full stop,
  no "excluded trades" panel/footnote in the UI (walks back the earlier
  "visibly counted, not silently dropped" instruction for the *rendered
  output* specifically). See REVISED point 4 in the CRITICAL DESIGN
  PRINCIPLE section above. The underlying three-way split stays as an
  internal/console data-quality tool since it's how the 87.7%-stale and
  55%-unresolved findings were caught, but nothing about it appears in the
  HTML the user looks at.

## V2 build complete (2026-09-24)

User feedback on v1: UI looked unpolished, and refreshing meant manually
re-running `serve.py` — confusing given the tool's own stated purpose.
Also: explicit new requirement — an execution-quality / cost-drag
drill-down, because edges were gated as net-positive after modeled
cost_bps, yet live results still diverge, and the user wants to know why
before assuming the edge itself decayed.

**New module: `execution_quality.py`.** Gate A/B/C's `cost_bps`
(`RESEARCH_COST_BPS_BY_ASSET`, `live_settings.py`) is a single per-entry
spread assumption. It has no model for (a) real fill slippage vs. the
planned entry price, (b) overnight swap on trades held longer than
typical, (c) commission — all three are real, already-recorded per-trade
dollar fields on the closed-trade ledger (`entry_slippage_price`, `swap`,
`commission`, `fee`, `planned_risk_usd`). `build_edge_execution_quality()`
converts each into R units (÷ `planned_risk_usd`, same denominator as
`realized_r`) so they're directly comparable to drift deltas.
`explain_drift_vs_cost()` then estimates, for any edge flagged as drifting
on `realized_r`, what fraction of that drift measured cost drag alone can
account for — the rest is labeled explicitly as unexplained/real drift, not
execution friction. Verified real finding: `DAX/trend_long`'s -0.351R
drift is only ~11% explained by measured cost drag (-0.0396R/trade, mostly
swap) — the remaining -0.312R is real, not a costing-methodology gap. Also
found real non-zero fill slippage on 26% of that edge's live trades and on
several other edges — a genuine gap versus what Gate A/B/C modeled, worth
tracking as sample size grows, but not (yet) the dominant driver of any
flagged drift.

**UI redesign** (`build_report.py`): dark glass-card layout, Inter/JetBrains
Mono fonts, sticky top nav with section anchors, `plotly_dark` themed
charts, redesigned verdict badges. Still a single self-contained
`report/index.html`, still fully dynamic (zero hardcoded edge/asset lists).

**Auto-refresh on reload** (`serve.py`): the server now calls
`build_report()` on every `GET /` / `GET /index.html`, not just once at
startup. A build failure serves an inline error page (with traceback)
instead of crashing the server or silently serving stale data — reload
again once the underlying issue is fixed. `--no-live-refresh` restores the
old static-serve behavior if ever needed (e.g. to keep the page frozen
while screen-sharing a specific moment). This removes the earlier
"re-run build_report.py to refresh" instruction entirely — it was
confusing self-inflicted friction and is no longer accurate.

## V3 build complete (2026-09-24)

User pushed back on "not yet built" for model-quality/capital-velocity and
asked for a deeper root-cause drill-down specifically addressing: "could
this drift flag just be a losing streak, given we still need more trades
for confidence?"

**New module: `deep_dive.py`.** Two independent checks per edge, both using
ONLY that edge's own real backtest sample (never a generic assumption):
1. **Streak check** — the edge's own Gate C execution-velocity summary
   already records `max_loss_streak` (worst-ever consecutive-loss run in
   full backtest history for that exact geometry). Compared directly
   against the live book's current consecutive-loss streak. Real result:
   `DAX/trend_long`'s backtest `max_loss_streak=75`; its live streak is
   currently 4 — nowhere close.
2. **Small-sample-noise bootstrap** — resample 3,000 windows of size
   n_live WITH replacement from the backtest's own per-trade sample,
   computing mean R each time; report the percentile the live sample's
   actual mean R falls at. This is a different question than the KS test
   ("is a sample this extreme common purely from having so few trades so
   far," vs. KS's "do the two full distributions differ"). Real result:
   `DAX/trend_long` (the one KS-flagged edge) sits at the 10th percentile —
   inside the normal 5–95% band, i.e. NOT extreme by this measure even
   though KS flagged it; the dashboard states this nuance explicitly rather
   than picking one test as "the answer." Also estimates (rough power
   simulation) how many more live trades would be needed to resolve the
   question either way — 23 more for `DAX/trend_long` at the currently
   observed effect size, i.e. it's already near that resolving sample size.
   Rendered as a card grid for every edge with 2+ live trades (not only
   flagged ones), and its verdict note is also injected as a second
   root-cause line under any KS-flagged row in the Drift Engine table
   (alongside the existing cost-drag line).

**New module: `model_quality.py`.** Reference AUC recomputed (Mann-Whitney,
same family as Gate D) from each edge's own Step04
`calibrated_oof_predictions.csv` (`raw_probability` vs.
`quote_executable_meta_label`). Live side joins closed trades to
`live_signal_audit.csv` by `signal_id` to recover the probability actually
used on that trade — that CSV is a rotating log, so match rate is reported
honestly (e.g. 8/23 for `DAX/trend_long` — older signals have rotated out)
rather than assumed complete. Live AUC only computed when both wins and
losses exist in the matched sample.

**New section in `build_report.py`: Capital Velocity Over Time.** Live
cumulative realized-R-per-occupied-day, recomputed trade-by-trade, plotted
against a dashed reference line at
`capital_time_velocity_R_per_occupied_day` from that edge's own Gate C
summary at its current live geometry — the actual V3 objective (turnover,
not just positive expectancy).

Nothing left unbuilt from the original v1/v2 scope list. Any further panels
are new asks, not backlog.

## 2026-10-23 update (AFML Ch.1 walkthrough)

The dashboard had stopped resolving every live edge: it read bar-count constants
(`EVENT_VOL_LOOKBACK`, `VERTICAL_HORIZON`) that the calendar-time rollout replaced with
`EVENT_VOL_LOOKBACK_HOURS` / `VERTICAL_HORIZON_HOURS`. Changes:

- `edge_reference.read_current_edge_geometry` reads hours constants first, bars as fallback.
- Backtest comparison sample = meta-approved bets at the live threshold with broker costs
  (allocator representative trade ledger, same benchmark as `portfolio_live/decommission_rule.py`),
  not every Gate C candidate event — live trades are approved bets (AFML p.211).
- `data_sources.load_gate_c_reference` finds the edge's current label in any Gate C batch summary.
- `data_sources.scope_closed_trades` keeps only trades of each edge's current certified version
  (`portfolio_live/edge_versions.py` registry); older versions go to excluded_stale.
- New Decommission / Shrink panel (verdicts of `portfolio_live/decommission_rule.py`).
- Known artefact: "worst-ever backtest losing streak" counts overlapping same-side bets separately
  (56-74 for current edges); bet merging per AFML p.196 is scheduled for the Ch.14 walkthrough.
