# A4 — Marcenko-Pastur Denoising / Detoning / ONC Clustering Utility

## Status

`BUILT AND TESTED — NOT YET WIRED INTO ANY CONSUMER`. This is a shared
primitive only. It is not yet used by Step 01's clustered feature
importance (blueprint §4.8, Stage B) or the portfolio allocator's clustering
layer (blueprint §4.9/§4.10, Stage D) — wiring it in is scoped to those
later stages, per the ordered migration plan, not to this one.

## What was built

Per blueprint §4.9 (MLAM Chapters 2 and 4), the single highest-leverage,
cheapest-to-implement fix identified in the whole transformation:

- **Marcenko-Pastur denoising** (`denoise_correlation`, method
  `constant_residual_eigenvalue` — MLAM's own preferred/canonical method):
  fits the noise/signal eigenvalue cutoff by minimizing the SSE between the
  theoretical Marcenko-Pastur PDF and a KDE of the empirical eigenvalue
  distribution, then replaces every eigenvalue below that cutoff with their
  common average (preserving the matrix's trace). An alternative
  `targeted_shrinkage` method is also implemented, per the book's own
  presentation of it as an alternative, not a replacement.
- **Detoning** (`detone_corr`): optionally removes the dominant "market"
  eigenvector(s) after denoising, for clustering use specifically. Correctly
  produces a singular matrix by construction (verified by test) — MLAM is
  explicit this must not be used directly for mean-variance optimization.
- **ONC — Optimal Number of Clusters** (`cluster_kmeans_top`): the full
  two-stage algorithm — silhouette-quality-scored base clustering across a
  double loop of k-means initializations and candidate cluster counts, then
  recursive re-clustering of only the below-average-quality clusters,
  accepted only if it genuinely improves quality (else falls back to the
  base clustering unchanged).

## Where it lives, and why (the Colab constraint applies here too)

Per the operational constraint the user flagged after A3
(`docs/mldp/A3_PURGE_EMBARGO_VERIFICATION.md`'s "Operational note"): Step 01
is exactly the station this utility is destined for (clustered feature
importance), and Step 01 runs standalone on Google Colab. This utility was
therefore designed the same way from the start, not retrofitted after the
fact:

- **`shared/colab_portable/denoise_onc_snippet.py`** is the single canonical
  implementation — dependency-free beyond numpy/pandas/scipy/scikit-learn
  (all present in a stock Colab runtime), with no imports from the rest of
  `shared/`. This is the file a future edge script should copy functions
  from verbatim.
- **`shared/models/denoise_onc.py`** is a thin re-export wrapper, for
  locally-run consumers (e.g. `portfolio_allocator/` scripts, or shared/
  test code) to import from, so there is one clear import path for code that
  *can* run locally.
- Unlike the purge/embargo case, **no edge currently consumes this utility**,
  so there is nothing to drift yet and no sync test was needed this round.
  When Stage B wires it into an edge's Step 01 script, that is exactly when
  a `tests/test_colab_portable_snippet_sync.py`-style drift-detection test
  must be added for this utility too, extending the established pattern
  rather than reinventing it.

## Verification

`tests/test_denoise_onc.py`, validated against MLAM's own described
experiments rather than only hand-picked toy cases:

1. **Marcenko-Pastur fit on a pure-random matrix** recovers a fitted noise
   variance close to 1 (almost all variance correctly attributed to noise).
2. **Marcenko-Pastur fit on a diluted-signal matrix** (20 injected signal
   factors mixed with a random component, mirroring MLAM's own worked
   example on p.27-28) recovers a plausible, nonzero factor count in the
   right neighborhood.
3. **Denoising** measurably lowers the correlation matrix's condition number
   and produces a valid correlation matrix (unit diagonal, symmetric).
4. **Detoning** produces a matrix with a near-zero smallest eigenvalue
   (singular by construction, as MLAM states it must be).
5. **ONC recovers injected block structure after shuffling** — a synthetic
   24-item, 4-block correlation matrix (modeled on MLAM's own Code Snippet
   4.3 block-diagonal generator) is randomly column/row-shuffled, and ONC is
   confirmed to recover exactly 4 clusters, each one a subset of exactly one
   true injected block.

One real bug was found and fixed during this verification, not just assumed
away: the `detone_corr` guard originally rejected `market_components ==
n_facts` (detoning *all* detected signal) as an error. That is actually a
valid MLAM case — if the only detected signal factor is the market factor
itself, detoning it entirely and leaving a fully singular, noise-only matrix
is correct, not a misuse. The guard now only rejects `market_components >
n_facts` (asking to detone more components than were actually found).

All 5 tests pass. The full touched/added test set for this session
(`test_denoise_onc.py`, `test_mldp_financial_cv.py`,
`test_colab_portable_snippet_sync.py`, `test_task2b_unresolved_disposition.py`,
`test_mldp_executable_target_engine.py`,
`test_mldp_executable_target_discovery.py`) was re-run together: 40 passed.
