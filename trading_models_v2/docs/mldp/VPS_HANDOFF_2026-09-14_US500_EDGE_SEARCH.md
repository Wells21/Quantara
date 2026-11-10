# VPS handoff — US500 edge search, 2026-09-14

This document exists because the local session that did this work cannot switch
its working directory to the VPS checkout — a new session must be started
there, and this file is what to hand it. Read this file plus
`config/us500_edge_search_ledger.json` (the live state ledger) before doing
anything else.

## What to tell the new VPS-rooted session, verbatim

> Read `docs/mldp/VPS_HANDOFF_2026-09-14_US500_EDGE_SEARCH.md` and
> `config/us500_edge_search_ledger.json` in full before doing anything. We are
> mid-way through a US500 edge search using a three-gate geometry-selection
> law (Gate A gross screen, Gate B Stage-1 finalist selection, Gate C Stage-2
> exact-executable-r authoritative decision — full law is in the ledger file
> under `CANDIDATE_SELECTION_LAW_v2_2026-09-14_SUPERSEDES_v1`). Three edges
> (`absorption_long`, `trend_long`, `candleengulfing_long`) are geometry-locked
> and Colab-ready. `candlepiercing_long` is mid-build (trigger loosened,
> self-test needs re-running to confirm event count). `candlehammer_long` was
> killed under the old law but should be re-tested under Gate C now that the
> new law's Gate A bar (gross >= 0.05) is what it needs to clear, which it
> already does (gross=0.0712). Confirm the VPS's `venv39`/`venv312` package
> versions (pandas/numpy) match local before running anything, to avoid a
> repeat of a pickle-version-mismatch bug hit earlier this session. Then
> proceed per the "Next actions" list below.

## Current state (2026-09-14, as of this handoff)

### Colab-ready (geometry locked, Stage-2 executable_r confirmed positive)
- **`absorption_long`**: geometry `evlb=20, evewm=50, horizon=1000, PT=2:SL=1`.
  Stage-2 exec_mean_R=+0.0330, Task2A resolution mean=+0.0341 (cross-confirms).
  Fresh export + resolution CSV done.
- **`trend_long`**: geometry `evlb=20, evewm=100, horizon=1000, PT=1.5:SL=1`
  (note: this edge's constant is named `EVENT_VOL_LOOKBACK_BARS`, not the
  generic `EVENT_VOL_LOOKBACK` — grep before assuming). Stage-2
  exec_mean_R=+0.0333, Task2A resolution mean=+0.0321. Fresh export +
  resolution CSV done. Also had a genuine pre-existing bug fixed here (see
  "Cross-repo bug fix" below) before it would export at all.
- **`candleengulfing_long`**: brand-new edge this session (Nison bullish
  engulfing pattern, Ch.4 p.42-48). Geometry `evlb=20, evewm=50, horizon=1000,
  PT=2:SL=1`. Stage-2 exec_mean_R=+0.0371 (strongest R/occupied_day of any
  candidate this session, +0.049). Task2A resolution mean=+0.0379. Registered
  in `config/edge_universe.json`. Fresh export + resolution CSV done.

**Next action for all three**: run Step01/02 (currently written in the
runbook as a Colab step, but Step02 is CPU-only sequential-bootstrap-bagging
now — no GPU dependency remains, so running directly on the VPS instead of
via Colab is worth considering, purely as an engineering convenience, not a
correctness requirement), then Steps 03-09 per the runbook.

### In progress
- **`candlepiercing_long`**: Nison piercing pattern (Ch.4 p.55) — gap-down
  open + >50% penetration into the prior black body. First cut used Nison's
  literal "opens below the prior low" criterion (true gap) and only found 52
  model rows (0.08% of bars) — too sparse, because true gaps between
  consecutive bars are rare on this repo's continuous intrabar
  `standard_quote_tick` clock (Nison's criterion assumes daily bars with
  overnight gaps). Loosened to "opens at/below the prior close," keeping the
  50%-penetration criterion as the real mechanical definition. The rerun to
  confirm the new event count was killed mid-run when the user asked to stop
  all processes (not a crash — a deliberate interrupt). **Next action**:
  rerun `python assets/US500/edges/candlepiercing_long/runtime.py` and
  confirm event count is now in the low thousands, then proceed through
  Gate A → B → C.

### Killed, but reconsider under the new law
- **`candlehammer_long`**: Nison hammer (Ch.4 p.32-38) at a local swing low.
  Killed under the OLD law (Stage-1 net_mean_R was -0.0047). Under the
  CURRENT law (`CANDIDATE_SELECTION_LAW_v2`), the actual Gate A criterion is
  gross_mean_R >= 0.05 — this edge's gross was +0.0712, which clears it. It
  was never given a real Gate C (Stage-2 exact bid/ask) test. **Next
  action**: run Gate C on it now that Gate A/B says it's worth testing —
  folder already exists at `assets/US500/edges/candlehammer_long`, never
  registered in `edge_universe.json` (fine, do that once/if it locks a
  winner).

### Permanently killed (0/225 Stage-1 grid cells ever positive — a real
signal-absence wall, not a cost-modeling artifact)
- `trendpullbackcontinuation_long`, `volatilityregime_long`, `reversal_long`
  (all pre-existing US500 edges, re-examined and killed this session)
- `compressionbreakoutcontinuation_short`, `dailyrangecompletionfade_long`,
  `rangestructure_short`, `signentropycollapse_short` (Tier-B, killed in an
  earlier part of this session — peak gross only 0.025-0.031, materially
  weaker than the ~0.05+ that survives)

### Deprioritized, not killed
- **`rangestructure_long`**: geometry already matches its own Stage-1 best
  cell (no fix needed there), but its full lifecycle already ran once under
  the OLD XGBoost architecture (before the AFML Ch.6 sequential-bootstrap-
  bagging migration) and showed a genuine meta-labeling failure — calibrated
  precision DECLINED as threshold rose (0.361@0.31 -> 0.312@0.38), unlike
  every working edge's monotonic climb. Worth re-running under the current
  bagging architecture once the active edges above are through, since the
  old failure might have been the same in-sample-refit overfitting problem
  bagging was built to fix — but it's not a priority.

## The three-gate law (full text in the ledger; summary here)

Stage 1 is NEVER a decision layer on its own, in either direction:
- **Gate A** (cheap, full ~225-cell grid): best-cell **gross**_mean_R must be
  >= +0.05 or the edge is killed, full stop. Gross is cost-independent, this
  is a pure signal-existence check.
- **Gate B** (same grid, NOT a kill gate): rank by net_R_per_mean_hold_bar
  using the asset's real p95-p99 empirical spread band as `--cost-bps` (see
  `per_asset_empirical_spread_bps_2026-09-14` in the ledger — US500 is
  ~1.6-1.8bps, NOT the flat 3bps used for every edge locked in tonight before
  this was measured). Top 3 cells become Stage-2 finalists regardless of
  whether their own net values are positive or negative.
- **Gate C** (expensive, exact bid/ask tick replay — the only authoritative
  layer): filter finalists to executable_r (exec_mean_R) STRICTLY > 0; if
  none clear it, kill the edge here regardless of Gates A/B. Winner among
  survivors = argmax(R/occupied_day), tie-broken by shorter mean_hold_h.

Gate C now has the same artifact-first-with-live-fallback behavior as Gate A
(see "Code fix" below) — you no longer need to run `export_research_dataset.py`
first just to unblock it on a brand-new edge.

## Code fix made this session: Gate C live fallback

`shared/research/v3_horizon_lab.py`'s `run_execution_finalist_mode()`
(`--execution-finalists`) used to hard-require a persisted
`research_dataset_manifest.json` to exist, unlike `--hybrid-fast-grid` (Gate
A) which already had a live-fallback to `runtime.build_features()` /
`generate_candidate_events()` for brand-new edges. Added a new
`_xef_prepare_inputs()` that tries the persisted manifest first and falls
back to the same live-build path only when the manifest file genuinely
doesn't exist (a manifest that exists but references a missing/broken output
file still raises — that's a real corruption signal, not "never exported
yet"). Smoke-tested against `candlepiercing_long` (which has zero persisted
export) to confirm the fallback actually engages rather than erroring — that
test was started but its result was never read back before the session
pivoted to other things; **worth re-confirming this actually passed** before
relying on it for a real Gate C run.

## Cross-repo bug fix made this session: `pt_hit_label`

`trend_long`'s (and 23 other files across the `trend_long`,
`downsidevolatilitycascade_short`, `drawupreversalhazard_short`,
`timeregime_short`, `volatilityregime_short` families across BTCUSD, DAX,
GBPJPY, HK, JPN, NASDAQ, USDMXN, USOIL, XAUUSD) `model_rows.py` never
assigned `pt_hit_label` in its triple-barrier labeling output, despite
downstream code requiring that column — `KeyError: ['pt_hit_label']`
whenever `export_research_dataset.py` actually ran. Fixed everywhere it
appeared (all 24 files recompiled clean). This was found by accident while
fixing `trend_long`'s stale feature_matrix — if you're about to touch any
edge from one of those five families on a new asset combo you haven't run
this session, it's already fixed, but worth knowing why if you see a
similar-shaped error somewhere this scan didn't reach.

## Per-asset empirical spread (bps), for Gate B — see ledger for full detail
and the run1-vs-run2 methodology caveat (BTCUSD's number is sensitive to
aggregation method, others are stable):

| Asset | Median | p95 | p99 |
|---|---|---|---|
| BTCUSD | 23.3 | 38.2 | 55.6 |
| USOIL | 6.3 | 8.6 | 9.4 |
| USDMXN | 2.7 | 3.9 | 8.5 |
| GBPJPY | 1.2 | 2.4 | 6.8 |
| HK | 4.8 | 6.6 | 6.7 |
| DAX | 0.9 | 2.8 | 4.9 |
| NASDAQ | 1.1 | 2.8 | 2.9 |
| XAUUSD | 1.7 | 2.3 | 2.9 |
| JPN | 1.8 | 2.8 | 2.9 |
| **US500** | **1.0** | **1.6** | **1.8** |

## AFML/MLAM sourcing discipline (carry forward)

Every new candidate design this session cited exact AFML/Nison page numbers
and was read from the primary PDFs directly, not from memory or a secondhand
summary — the user has been explicit and repeated about this requirement
multiple times across sessions. Keep doing that. PDFs live at:
`C:\Users\wells\Documents\WELLSMART Company\Trading Models\Advances in
Financial Machine Learning by Marcos Lopez de Prado.pdf`,
`...\Machine Learning for Asset Managers by Marcos Lopez de Prado.pdf`, and
the candlestick pattern reference `...\Steve-Nison-Japanese-Candlestick-
Charting-Techniques-Prentice-Hall-Press-2001.pdf` (under a `Downloads\Mobile
Devices\` subfolder).

## VPS-specific notes

- Tick data for US500 has been copied to the VPS via plain file transfer
  (not git — `assets/*/raw/ticks/` is gitignored on purpose, ~5.2GB for
  US500 alone). Path resolution in the codebase uses `pathlib`, relative to
  repo root — no hardcoded Windows paths found in the core scripts, so this
  should just work once the tick folder is in the matching relative
  location.
- VPS is Windows, with both `venv39` and `venv312` already present — verify
  package versions (pandas/numpy especially) match the local venv before
  trusting any run there. A pandas pickle-version mismatch between Python
  environments caused a real, confusing crash earlier this session (bare
  system Python 3.8 vs `venv39`) — same risk class if VPS/local diverge.
- Nothing else about the workflow changes: `export_research_dataset.py` and
  `runtime.py` are still edge-local and hardcoded to their own folder, the
  no-partial-fixes rule still applies, `deployability_pass` is still only
  ever set via `promote_edge_status.py` after a real Step09 PASS.
