# A7 — AFML Ch.17/18/19 Feature Engineering (Structural Breaks, Entropy, Microstructure)

## Status

`SHARED_MODULES_BUILT_AND_TESTED — WIRED INTO BTCUSD/absorption_long ONLY`.
Per the blueprint's §4.11 (AFML Ch.17-19, book p.249-300), this pass adds
three previously-unimplemented AFML feature-engineering chapters as shared,
importable modules, and wires them into the Task 2C pilot edge
(`BTCUSD/absorption_long`) ahead of that edge's first Colab run under the
new target architecture.

## Why these three, and why only the High/Low subset of Ch.19

An audit (see conversation record) confirmed AFML/MLAM do not mandate any
specific feature-matrix contents — they mandate *methodology* for whatever
features are chosen. Entropy, structural-break, and microstructural
features are the book's own toolkit chapters, not a checklist every model
must satisfy. Given the user's explicit request to add them where
feasible with this codebase's actual data:

- **AFML Ch.17 (Structural Breaks, p.249-262)** — SADF needs only Close
  prices. Fully implementable.
- **AFML Ch.18 (Entropy Features, p.263-279)** — needs a return/price
  sequence, ideally FFD-encoded per the book's own recommendation
  (p.270). Fully implementable using the already-installed FFD artifact.
- **AFML Ch.19 (Microstructural Features, p.281-300)** — the book itself
  is explicit (p.282, p.286-292) that Kyle's/Amihud's/Hasbrouck's lambda,
  the tick rule, the Roll model, and PIN/VPIN all require **true trade
  prints** (price + volume + aggressor side). This codebase's raw data is
  Bid/Ask quote ticks for every asset (confirmed with the user — "no asset
  has true trade prints"). Only the Parkinson (1980) high-low volatility
  estimator and the Corwin-Schultz (2012) spread estimator are implemented,
  since the book itself designed those two specifically for markets with
  no order book/trade-print data at all (p.286). Building anything named
  "Kyle's lambda" or "VPIN" from a quote-midpoint proxy would be a
  `DATA_CONSTRAINED_ADAPTATION`, not a faithful reproduction, and is
  deliberately not done.

## What was built

- **`shared/features/afml_ch17_structural_breaks.py`** — SADF
  (Supremum Augmented Dickey-Fuller), porting AFML Snippets 17.1-17.4
  (book p.258-259) function-for-function: `lag_df`, `get_yx`, `get_betas`,
  `get_bsadf` (the book's inner loop), and `sadf_series` (the book's
  "outer loop," not itself printed as a snippet). Fit on log-prices per
  the book's own p.254 argument (log-price specification conditions
  returns' *mean* on price level, not their *volatility*, which is the
  correct assumption for a series spanning multiple regimes).
- **`shared/features/afml_ch18_entropy.py`** — `pmf1`/`plug_in` (Snippet
  18.1), `lempel_ziv_lib` (Snippet 18.2), `match_length` (Snippet 18.3),
  `kontoyiannis_entropy` (Snippet 18.4, the book's preferred/most-refined
  estimator), `binary_sign_encode` (Section 18.5.1's encoding scheme), and
  `rolling_entropy` (a rolling-window wrapper selecting among the three
  estimators).
- **`shared/features/afml_ch19_microstructure.py`** — `parkinson_volatility`
  (book p.283-284, no snippet printed, formula reproduced exactly),
  `corwin_schultz_spread` and `becker_parkinson_volatility` (Snippets
  19.1-19.2, book p.285-286).
- **Tests** (`tests/test_afml_ch17_sadf.py`, `tests/test_afml_ch18_entropy.py`,
  `tests/test_afml_ch19_microstructure.py`, 22 tests total): SADF verified
  to spike during an injected explosive/bubble segment vs. a random walk
  (the book's own Figure 17.1 qualitative claim); entropy verified lower
  for a periodic/repetitive message than a random one; Parkinson and
  Corwin-Schultz verified to **recover an injected known volatility/spread**
  from synthetic OHLC data, not merely to match arithmetic.

## Two real bugs found and fixed during implementation (both caught before merging)

1. **Corwin-Schultz's Becker-Parkinson volatility constant.** The book's
   Snippet 19.2 uses `(2**-.5 - 1)` (≈ -0.293), which I initially
   mistranscribed as `(sqrt(2) - 1)` (≈ +0.414) — a sign-and-magnitude
   error. Caught by numerically comparing my port against the book's
   literal formula on random inputs before shipping (max abs diff after
   fix: 0.0).
2. **`get_yx`'s dropna ordering.** My first draft called `.dropna()` on the
   lag-shifted design matrix *after* overwriting column 0 with the lagged
   price level, whereas the book's Snippet 17.2 drops NaN rows *first*,
   then overwrites column 0 using the post-dropna shape. Fixed to match
   the book's exact order of operations.

## Computational-tractability adaptations (documented, not silent)

SADF is inherently expensive — the book's own complexity analysis (p.254-255)
shows a full-history, every-bar SADF series is `O(T^3)`. Calibrated directly
against this codebase's BTCUSD bar history (~78k bars): at `lookback=50`,
each point costs ~27ms, so a naive every-bar pass would take ~35 minutes.
Two independent, explicitly-documented adaptations keep this tractable:

- `lookback` bounds each `SADF_t` to a trailing window (50 bars in the
  wired feature) instead of the full since-inception history.
- `stride` computes `SADF_t` only every Nth bar (10, in the wired feature)
  and forward-fills between, cutting the wired feature's cost to
  ~3.5 minutes for one local (non-Colab) `export_research_dataset.py` run.

Both parameters change **where/how often** the exact book formula is
evaluated, not the formula itself — `stride=1` recovers the book's literal
per-bar `SADF_t`.

## What was wired into BTCUSD/absorption_long

`feature_expansion.py::add_v1_feature_expansion` now computes, after the
existing FFD merge:

- `SADF_ct_50` — SADF on `log(Close)`, constant+trend spec, 1 lag,
  `min_sl=10`, `lookback=50`, `stride=10`.
- `Entropy_Kontoyiannis_100` — Kontoyiannis entropy rate over a trailing
  100-bar window of a binary-sign-encoded message built from
  `Close_FFD`'s period-over-period change (per the book's p.270
  recommendation to encode FFD, not integer-differenced, series).
- `Parkinson_Vol_20`, `Corwin_Schultz_Spread_1`, `Becker_Parkinson_Vol_1` —
  rolling High/Low-only estimators.

All five are registered in `contracts/feature_schema.json` under new
feature groups `afml_structural_breaks`, `afml_entropy`, and
`afml_microstructure_hl` (`live_available: true`, `allowed_for_model_input:
true`, `uses_future_information: false` — all are strictly causal/rolling).

**Live-hotpath note (not a correctness blocker):** `feature_expansion.py`
is also called from `runtime.py`, which the live streaming hotpath
(`shared/edges/native_one_row_feature_engine.py`) wraps for low-latency
incremental scoring. That engine already has a documented fail-closed
fallback to the slower full-batch-recompute adapter for any feature it
does not have a hand-optimized incremental form for. These five new
features have not been ported into that native engine; live scoring for
an edge using them will use the slower fallback path rather than crash.
This is not a concern for the current research/training work (no V3 edge
is promoted to live authority during this migration per the existing
promotion freeze), but is flagged here for whoever eventually re-certifies
`BTCUSD/absorption_long` for live trading.

## What remains explicitly out of scope for A7

- **Propagation to other edges.** Per this session's established
  "dynamic and scalable" requirement, Step 01/02 were propagated to all
  141 edges because they are a byte-identical shared template. Feature
  engineering is different: each edge's `feature_expansion.py` is edge-
  specific code (different event/feature logic per edge). Wiring these
  three modules into other edges is a per-edge decision, not a mechanical
  propagation, and was not done here.
- **Kyle's/Amihud's/Hasbrouck's lambda, tick rule, Roll model, PIN/VPIN**
  — not implemented anywhere, per the data-constraint finding above.
- **Live hotpath native incremental ports** for these five features (see
  note above) — deferred to whenever live re-certification is scoped.
- **MLAM Ch.6 PCA-based orthogonal feature construction** — determined
  not needed: MLAM §6.5.2's Clustered Feature Importance (ONC-based,
  already wired into Step 01 per A4/Task 2C) is the books' own later,
  preferred refinement over AFML Ch.8's PCA-orthogonalization remedy for
  the same substitution-effect problem. Implementing both would be
  redundant, not more complete.
