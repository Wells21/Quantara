# Runbook Update Log

Running log of every discrepancy found between `Quantara_Runbook.docx` and actual
system behavior while executing the go-live sequence, plus every latent bug/gap
found along the way that the runbook should document. **Not yet applied to the
docx** — per standing instruction, all of this gets folded into one consolidated
runbook update once portfolio_live work is complete and the system is ready to
go live. Entries are dated and reference the exact files/commits involved.

---

## 2026-09-19/20 — Phase C.2 rebuild (Section 11B.2)

- **Finding**: `phase_c_02_certify_incremental_features.py` failed for all 7 live
  assets. Root cause: the live incremental ("one-row") feature kernel
  (`shared/edges/native_one_row_feature_engine.py`) never implemented 5 AFML
  Ch.17/18/19 features (`Becker_Parkinson_Vol_1`, `Corwin_Schultz_Spread_1`,
  `Entropy_Kontoyiannis_100`, `Parkinson_Vol_20`, `SADF_ct_50`) that the batch
  feature contract requires. Pre-existing gap, not caused by this session's
  changes; first surfaced because today's edge-universe change (103->21 edges)
  was the first time this exact rebuild path ran end-to-end.
- **Fix**: implemented all 5 as bounded-window/stateless functions in
  `native_one_row_feature_engine.py` (`ENGINE_VERSION` bumped to
  `v1_4_afml_ch17_18_19_microstructure`). SADF_ct_50 required discovering that
  production's fixed 6,000-bar rolling window collapses the batch's
  stride/forward-fill mechanism into a constant `SADF_LAG` (=9) bars behind
  the current bar — see that module's own docstring for the derivation.
- **Runbook impact**: Section 7/11B should note the incremental kernel now
  supports the full AFML Ch.17-19 feature set; no operator-facing command
  changes needed (same certification commands as before).

## 2026-09-20 — Section 12 pre-C5 gate

- **Finding**: `validate_production_hardening_patch.py` failed on
  `PORTFOLIO_DASHBOARD_MAX_SIGNAL_AGE_SECONDS`/`PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS`
  — validator expected `1000`, live code has `120` (changed in commit
  `a65327bb` "Task 1 patcher done", 2026-09-10, tagged
  `QUANTARA_MLDP_TASK1_FOUNDATION_V1`). **User decision: 120s is the current,
  correct contract** — updated the validator and all fallback-default call
  sites (`production_snapshot_pipeline.py`, `portfolio_execution_bot.py`,
  `production_readiness_p0.py`) to `120` instead of reverting live code.
- **Runbook impact**: Section 12.1's text ("must resolve to 1000 seconds")
  is now wrong and needs updating to say 120 seconds, with a note that this
  was a deliberate 2026-09-10 change, not regression.
- **Finding**: `validate_production_hardening_patch.py`'s isolated test
  harness (`_install_execution_import_stubs`) stubs `shared.prop_account_rules`
  but was never updated after a 2026-09-08 commit (`9b4e5ceb`) added a new
  `shared.live_payoff_contract` import to `portfolio_execution_bot.py`. Added
  a matching stub. Pre-existing gap (~3 weeks stale), unrelated to this
  session's other changes.

## 2026-09-20 — Full `unittest discover` gate: provider-signature drift (major)

- **Finding**: 100% of the 23 `shared/edges/candidate_continuations/providers/*.py`
  modules had stale `PROVIDER_SPEC["supported_signatures"]` pins relative to
  the current `portfolio_live/config/phase_d/candidate_contract.json` — every
  currently-live edge that should have a "fast continuation" provider was
  running `AUTHORITATIVE_BATCH` only (D.4.1 itself reported "Bound providers: 0"
  earlier in this session, correctly, but wasn't flagged as a problem until
  the full test run surfaced 56 failures/errors from it). Confirmed this
  predates this session (e.g. HK's `drawdownhazardcontinuation_short`
  candidate source was rewritten 2026-09-17 "Done with HK, only 3 edges
  survived"; its provider was last pinned 2026-08-19).
- **Fix**: resynced 12 provider modules' pinned signatures (`absorption`,
  `reversal`, `timeregime`, `cusum` positive side, `dailyrangefade_long`,
  `breakdownretest_short`, `compressionbreakout_short`, `drawdownhazard_short`,
  `rangestructure`, `trendpullback_long`, `volatilitycarry`, `volatilityregime`),
  verifying via git history for each whether the drift was a safe
  parameter-only change (read dynamically by the provider's `create_provider()`
  from the edge's own compiled `semantic_constants` at bind time) or needed
  real review. Multi-asset families now carry every asset's distinct current
  signature, not just one.
- **New tool**: `portfolio_live/phase_d_05_audit_provider_signature_drift.py`
  — run this any time `phase_d_01_compile_candidate_contract.py` (or the
  incremental D.4.1 contract) is recompiled, to catch this class of drift
  immediately instead of as a wall of unrelated-looking test failures weeks
  later. **This should become a standard step in the runbook's Section 7/11B
  sequence**, right after the D.1 candidate-contract compile step.
- **Real bug found via this process**: `trendpullback_long`'s provider
  (`shared/edges/candidate_continuations/providers/trendpullback_long.py`)
  emitted 12 extra events vs. the real batch source for NASDAQ's edge. Root
  cause: the batch source (`generate_v1_trendpullbackcontinuation_long_candidate_events`)
  selects a bar for gap-spacing purposes *before* its final
  `.dropna(subset=[...,"Pullback_Return_Z",...])` — so a bar can consume the
  `min_gap_bars` slot even though it's later dropped from the returned events
  (during the `PULLBACK_Z_LOOKBACK`=50-bar warmup). The provider's state
  machine replicated the gap-slot consumption but not the emission
  suppression. Fixed and verified via direct numerical diff (exact frame
  parity confirmed). **This exact pattern (batch does post-selection dropna
  after gap-filtering already ran) may exist in other candidate families not
  yet exercised by a test with a long-enough warmup column to expose it —
  worth a deliberate audit pass if time allows before go-live.**

## 2026-09-20 — Fourth staleness layer: D.4 real-history certification (UNRESOLVED)

- **Finding**: `portfolio_live/outputs/phase_d4_provider_real_history_certification_v2.json`
  is frozen from 2026-08-22, certified against the *old* 103-edge (BTCUSD-
  inclusive) universe. Even with provider signatures fully resynced, **zero
  edges are currently fast-eligible** — every live edge safely runs
  `AUTHORITATIVE_BATCH` (not a correctness bug) but gets none of the
  fast-continuation performance benefit. Confirmed via
  `phase_d_04_provider_overlay.static_overlay_prerequisites()`.
- **Not fixed**: re-certifying requires running
  `portfolio_live/certification/phase_d4_consolidated_real_history_certifier_v3.py`
  against real historical data for the current 21-edge universe — a
  substantial, separate operation not triggered in this session. **Needs an
  explicit decision on when/whether to run this before or shortly after
  go-live.**
- **Runbook impact**: Section 7/11B/13 should document that this
  certification must be re-run whenever the selected edge universe changes,
  same as the Phase C/D.1 contracts — it currently isn't listed as a
  recurring step anywhere.

## 2026-09-20 — Fifth staleness layer: threshold/payoff artifact hash precision (RESOLVED)

- **Finding**: `shared/validation/v3_semantic_contract.py`'s `payoff_context_id`
  hash changed precision from `.17g` to `.12g` in commit `afdf2fc1`
  (2026-09-18 14:22, "Fixed issues in the portfolio_allocator folder script
  until 08...") — a deliberate, well-documented fix for genuine 1-ULP
  floating-point non-determinism in the empirical payoff profile's
  arithmetic (see that commit's own comment and `config/mldp_deviation_register.json`).
  **Every one of the 20 currently-eligible edges'** saved threshold/payoff
  artifacts (`assets/<ASSET>/edges/<EDGE>/artifacts/thresholds/*_selected_threshold.json`,
  Step 04 output) predate that commit and now fail
  `validate_payoff_context_mapping`'s self-consistency check with
  "Threshold/payoff semantic identity mismatch" — confirmed the underlying
  data is NOT corrupted (every other field matches; only the hash itself
  differs, exactly as expected from a precision-scheme change).
- **Correction to initial assessment**: re-running Step 04 is NOT required.
  `payoff_context_id` is a hash of numbers the artifact already has stored
  (win rate, R-multiples, threshold, etc.) — Step 04 was already run
  correctly once; only the hash *formatting scheme* changed afterward.
  Confirmed for all 25 affected artifacts (spanning both live and
  currently-non-selected edges) that every field except the ID string
  matched exactly between the stored artifact and what current code would
  rebuild from that artifact's own data — i.e. purely a hash-format
  mismatch, not data drift, corruption, or anything edge-specific.
- **This was confirmed live-blocking before the fix**: `EdgeScorer.score()`
  calls `validate_artifacts()` on every scoring call, and
  `portfolio_live_dashboard_orchestrator.py:6368` calls
  `preload_edge_scorer_artifacts(..., validate=True)` at dashboard startup
  for every edge — all 20 live edges would have failed to preload/score.
- **Fix applied**: recomputed `payoff_context_id` from each artifact's own
  stored fields and re-stamped it (string replacement of the old ID with the
  new one) across every file under that edge's `artifacts/` tree that
  referenced it (6-7 files/edge: `*_selected_threshold.json`,
  `*_threshold_manifest.json`, `*_calibration_manifest.json`,
  `*_calibration_report.json`, `edge_manifest.json`, and one row in
  `*_calibrated_threshold_sweep.csv`) — 25 edges, 165 files total. Every
  touched file was backed up first (`<file>.bak` alongside it — used a short
  suffix since several of these paths are already close to Windows' 260-char
  MAX_PATH). Verified after: all 20 live edges now build a
  `ResidentEdgeScorer`/`create_composed_decision_continuation()`
  successfully; `tests/test_phase_d_resident_edge_scorer.py` passes for real
  (no longer needs its skip path, though the skip guard stays in as a
  safety net for any *future* recurrence of this same drift class).
- **New tool**: `shared/validation/audit_payoff_context_id_drift.py` — run
  this any time `v3_semantic_contract.py`'s identity-hash logic changes, or
  whenever this exact error resurfaces. Reports drift; `--fix` applies the
  same safe re-stamp automatically, refusing to touch any edge where a real
  data field (not just the ID) differs. **Should be added as a standard
  post-check whenever `shared/validation/v3_semantic_contract.py` is
  modified**, analogous to `phase_d_05_audit_provider_signature_drift.py`
  for candidate/primary signatures.
- **Why this happened, for the runbook's benefit**: this is NOT tied to
  creating a new edge, and does not require redoing an edge's lifecycle. It
  recurs only when someone edits the *shared* identity-hash formula in
  `v3_semantic_contract.py` (rare, and usually for a good reason — the
  2026-09-18 change fixed genuine float non-determinism). When that happens,
  every artifact generated before the change becomes stale relative to the
  new formula until re-stamped. The runbook should document
  `audit_payoff_context_id_drift.py --fix` as the standard, cheap remedy —
  not "re-run Step 04."

## 2026-09-20 — Sections 13-15 executed, no discrepancies

- C5 sealed cleanly on the first attempt post-11B (all 9 gates PASS,
  including full unittest discovery). C4 activated. D3 hybrid activation:
  PASS, 20/21 edges optimized, 1 (`US500/candleengulfing_long`, which has no
  provider at all) correctly routed to C4 authority. D4 overlay activation:
  PASS but structurally inert (0 fast, 21 authoritative,
  `unsupported_trading_edges=0`) — expected, given the D4 real-history
  certification staleness logged above; deferred to Section 15A per plan.
  Matched the runbook's documented commands/expectations exactly — nothing
  to change here.

## 2026-09-20 — Section 16 ordering gap for a continuing (not new) account

- **Context**: this deployment is NOT a new account/broker migration (Section
  16A does not apply — the account has real trading history and is
  currently in drawdown from prior live trading). But the dashboard has been
  stopped for some time during the upstream re-engineering work in this
  session, so `portfolio_live/live_bars/<ASSET>_live_standard_quote_tick_bars.csv`
  (the dashboard's own rolling tick-bar output, NOT account/position/trade
  state) is currently empty for all 7 assets.
- **Finding**: Section 16's `repair_live_feature_continuation.py --repair`
  correctly fails closed and rolls back for all 7 assets with
  `internal_ffd_coverage_error:required_file_missing`, because its coverage
  guard (`live_feature_continuation_guard.py::_ffd_internal_live_coverage`)
  hard-requires the live_bars CSV to already exist — it has no broker-backfill
  capability itself. The actual backfill mechanism is
  `portfolio_live_dashboard_orchestrator.py::run_startup_gap_healing()`,
  which only runs when the dashboard itself starts (Section 17) and bridges
  from seed/research bars to "now" via fresh MT5 tick pulls.
- **Runbook impact**: Section 16, as currently sequenced strictly before
  Section 17, implicitly assumes `live_bars` already has recent data (true
  for "restarting an already-running dashboard for infra changes", the
  presumed common case) but not for "continuing account whose dashboard sat
  idle for a while." For that case, the correct order is: attempt Section 16
  (expect a clean fail-closed rollback, not a bug), proceed to Section 17 to
  let startup gap-healing backfill `live_bars` from the broker, then
  RE-RUN Section 16's health check/repair afterward now that real bar data
  exists to validate continuation against. The runbook should document this
  as an explicit branch alongside the existing "new account" (16A) case.
- **Resolution**: the user located and restored the `live_bars/*.csv` files
  from elsewhere (confirmed genuine tick-bar content, timestamps through
  2026-08-25). Root cause of the empty directory was NOT determined with
  certainty — filesystem forensics (directory birth timestamp 2026-09-19,
  ~2 months after this environment's other `portfolio_live/` directories
  were provisioned on 2026-07-24; no Recycle Bin trace; no command in this
  session touched the path) point to the data never having existed on this
  specific environment/checkout rather than an in-session deletion, but this
  could not be fully confirmed without visibility into whatever happened
  before 2026-07-24. Re-ran the health check after restoration: all 7 assets
  PASS immediately with no repair needed (live/ffd/hmm all consistent at
  2026-08-25 timestamps), confirming the continuation state itself was
  never actually broken -- it only needed the bar data present to validate.

## 2026-09-20 — Section 17 first attempt: correctly blocked by weekend market closure

- Attempted dashboard startup on a Sunday. Startup sequence itself was fully
  clean: MT5 connected (balance/equity matched known account state exactly),
  all 21 edges preloaded with zero failures, C4/D3/D4 hybrid routing
  installed matching Section 15's validation (20 D3-optimized/C4-auth, 1
  C4-only, unsupported=0). This is a real end-to-end confirmation that
  today's provider-signature and payoff-context-id fixes hold up under an
  actual dashboard start, not just isolated checks.
- Startup then correctly fail-closed at `resolve_broker_clock_consensus`
  with `clock_contract_no_fresh_symbol_samples` — the broker clock lock
  requires fresh tick samples, and the latest available ticks were from
  Friday's close (markets closed for the weekend). Dashboard exited cleanly
  on its own; no persistent process, no state corruption, no order activity.
- **Runbook impact**: Section 17 should note that a first dashboard startup
  attempt outside trading hours (weekend, or before markets reopen) will
  legitimately fail at the broker-clock-consensus gate, and that this is
  expected fail-closed behavior, not a fault to debug — retry once the
  relevant market(s) have fresh tick flow.

## 2026-09-20 — Section 15A executed in full: 21/21 edges promoted to FAST

- 15A.1 inventory (using the real `_phase_d410_overlay_partition` logic, not
  a naive "is edge_id a cert key" check): 20/21 edges were provider-capable
  but certificate-only (code correct, cert stale); exactly 1
  (`US500/candleengulfing_long`) had zero provider coverage at all.
- 15A.2: built new candidate-side
  (`shared/edges/candidate_continuations/providers/candleengulfing_long.py`)
  and primary-side
  (`shared/edges/primary_side_continuations/providers/candleengulfing_long.py`)
  providers from scratch, reading the real source
  (`assets/US500/edges/candleengulfing_long/{candidate_events,primary_side}.py`)
  directly. Found and pre-emptively handled the same "batch selects a bar
  for gap-spacing before its final dropna suppresses the row" pattern
  discovered earlier in `trendpullback_long` (here: `Event_Vol`'s 20-bar
  warmup can genuinely outlast the engulfing pattern's own ~5-bar warmup).
  Verified via direct numerical diff before running anything expensive:
  candidate full-frame parity PASS, restart parity PASS, primary-side parity
  PASS -- all on the first attempt.
- Preflight for 15A.3 initially failed ("existing D.3 downstream
  certification chain is not current all-edge PASS") -- confirms Section
  14.1's own text that D2/D3 full certification, while optional for basic
  C4 tradability, is a genuine prerequisite for the D4 acceleration path.
  Ran `phase_d_02_certify_candidate_first.py` (PASS 21/21, including
  candleengulfing_long's real historical event count of 103) and
  `phase_d_03_certify_production_router.py --seal-production` (PASS,
  21/21 deferred-projection edges, D.2 authority PASS, C.5 model/decision
  parity PASS) first.
- 15A.3: staged `phase_d4_consolidated_real_history_certifier_v3.py` run --
  PASS, 21/21 edges checked and passed, 0 failed, FAST capability 21/21,
  unsupported=0. Exactly the runbook's required release evidence.
- 15A.4: promoted atomically (backup, SHA-verified copy, re-validated via
  `static_overlay_prerequisites()`/`validate_activation()`). First promotion
  attempt was incorrectly rolled back by a bug in my own verification
  script (`0 or -1` evaluates to `-1` in Python since `0` is falsy --
  silently corrupted an "unsupported_trading_edges == 0" check into a false
  failure). Fixed the script and re-ran; promotion succeeded and verified
  clean on the second attempt. No runbook/production-code issue -- purely a
  bug in the one-off promotion script I wrote for this step.
- 15A.5: all four focused suites (`test_phase_d_04_provider_overlay`,
  `test_phase_d_scalable_router`, `test_phase_d_candidate_provider_registry`,
  `test_phase_d_primary_side_provider_registry`) PASS -- notably the two
  D4-fallback tests that were skipping earlier (no fast-eligible edge to
  test against) now execute for real.
- 15A.6: `validate_production_hardening_patch.py` -- top-level PASS.
- **Final state**: C4 PASS / D3 PASS (21 optimized, 0 C4-authoritative) /
  D4 PASS (21 FAST, 0 authoritative, 0 unsupported, 100% coverage).
- **Runbook impact**: none identified -- Section 15A's documented sequence
  was followed exactly and worked as written, including its own
  anticipation of the D2/D3-prerequisite failure mode.

## Signal-age note carried over from earlier session (still open)

- `local_live_settings.json` was found with `enable_order_send=True` /
  `portfolio_execution_mode=live` pre-configured. Flagged, not yet resolved —
  confirm this is intentional before going live.
