# A3 — Purge/Embargo Primitive Verification

## Status

`VERIFIED CORRECT — CONSOLIDATION DEFERRED TO STAGE B/C`. See
`config/mldp_deviation_register.json` item `D013`.

## What this was supposed to find

Per the blueprint (§4.5, P0.3) and the AFML-extraction agents that produced
it, AFML's fully general purge routine is `getTrainTimes` (Snippet 7.1): for
every training label, purge it if it satisfies any of three named
interval-overlap conditions against any testing label. `PurgedKFold.split()`
(Snippet 7.3) is a *different*, simplified routine valid only for contiguous
KFold blocks. The extraction agent's warning was that if Quantara's shared
helper mirrored the simplified shortcut, that would be the exact mechanical
cause of the previously-flagged "not an exact interval-overlap
implementation" finding. This was treated as the single highest-priority
mechanical fix in the blueprint.

## What was actually found

**The purge math itself is not broken.** Two independent implementations
were checked, both against a literal, independently-written brute-force
reference implementing the union of AFML's three named conditions
(`cond1 or cond2 or cond3`, checked against every individual test interval,
not merged):

1. **`shared/models/financial_cv.py::purge_overlapping_train_indices`** — an
   O(n log n) implementation (merges test intervals, binary-searches per
   training interval). Fuzz-tested across 2,000 randomized interval
   configurations (later reduced to 500 in the permanent test suite to keep
   CI fast) plus explicit fixtures for each of the three named conditions
   individually and a boundary-touching case. **Zero mismatches.**
2. **The edge-local `purge_train_overlaps` function**, copy-pasted into
   every edge's Step 01/02/03 research scripts — a vectorized O(n·m)
   implementation checking every training interval against every test
   interval directly (no merging). A hash-scan
   (`grep -rl "def purge_train_overlaps" assets/*/edges/*/research/*.py`)
   found **423 occurrences across the repository, all byte-identical** (one
   SHA1 hash for the purge function, one for the paired `apply_embargo`
   function, across all 423 files — no divergent forks). This exact function
   was independently fuzz-tested the same way. **Zero mismatches.**

Both implementations are mathematically exact matches to AFML's own
algorithm. The `PurgedKFold.split()`-shortcut failure mode the blueprint
predicted does not appear to be present anywhere in this codebase — neither
implementation resembles that simplified, contiguous-block-only routine.

`shared/models/cpcv_training.py::CombinatorialPurgedCV` (the higher-level
class that wraps `financial_cv.py`'s two primitives into a usable CPCV
splitter) was also re-run via its own self-test after the test-suite changes
below; it still passes.

`skfolio.model_selection.CombinatorialPurgedCV` (a third-party library) is
used by edge Step 02 scripts, but only for generating the combinatorial
train/test **fold structure** (`purged_size=0` — its own purge is disabled).
The actual purge/embargo is always the local, hand-copied
`purge_train_overlaps`/`apply_embargo` pair described above, not anything
from `skfolio` itself.

## What is still wrong, and the fix, corrected for a real operational constraint

There is no single authoritative splitter, in the sense of one piece of code
literally executing everywhere. That initially looked like uncontrolled
technical debt: the same correct logic exists in two independently-written
places (the shared module, and 423 copies of a different-but-also-correct
implementation), and nothing was checking that the 423 copies stayed in
agreement with each other.

**The user then clarified a permanent operational constraint this
verification had not accounted for**: edge Step 01/02 research scripts are
never run from this repo checkout. They are uploaded standalone to Google
Colab — only the single script plus the data files it needs travel there,
never the rest of the `shared/` package — run there, and the resulting
artifacts are downloaded back. This is why the purge/embargo logic is
duplicated in the first place, and it means the originally-planned fix
("redirect each edge's Step 02 script to `import shared.models.financial_cv`
during its Stage B/C rebuild") is wrong — that import would simply fail on
Colab. Any future station in this migration that touches Step 01/02 must
design for standalone, dependency-free execution as a first-class
requirement, not an afterthought.

**The actual fix, respecting that constraint:** a single canonical copy of
the already-verified-correct function pair now lives at
`shared/colab_portable/purge_embargo_snippet.py`. A new test,
`tests/test_colab_portable_snippet_sync.py`, independently re-parses every
edge script under `assets/*/edges/*/research/*.py` and asserts each one's
local `purge_train_overlaps`/`apply_embargo` definitions are byte-identical
(after whitespace normalization) to that canonical file. This gives the same
guarantee the "one authoritative source" principle was after — no silent
divergence — through an automated equality check the test suite runs every
time, rather than through a shared runtime import that the actual execution
environment cannot support. All 423 copies currently pass this check.

`D013` is now marked resolved on this basis. It stays resolved only as long
as the sync test keeps passing; if a future edit to any edge script's local
copy (or to the canonical file itself, without updating the edges) breaks
the test, that is exactly the drift this mechanism exists to catch.

## What was added to the permanent test suite

`tests/test_mldp_financial_cv.py` now contains, in addition to the two tests
that existed before this verification pass:

- Three fixtures reproducing AFML's three named overlap conditions
  individually (train starts inside test; train ends inside test; train
  envelops test).
- A boundary-touching fixture confirming the shared inequality is inclusive
  (`<=`), matching the book.
- A no-overlap fixture confirming a genuinely disjoint training interval
  survives.
- `test_fuzz_matches_literal_get_train_times` — 500 randomized trials cross-
  checked against an independent literal implementation of `getTrainTimes`,
  the load-bearing regression test for this entire finding.

All 8 tests pass; the pre-existing `financial_cv`/`executable_target_engine`
test files were also re-run together and remain green.

`tests/test_colab_portable_snippet_sync.py` (2 tests, both passing) adds the
drift-detection mechanism described above: it fails loudly, naming every
mismatching file, the moment any edge script's local purge/embargo copy
diverges from `shared/colab_portable/purge_embargo_snippet.py`.

## Operational note for all future stages of this migration

Any shared utility this migration builds that Step 01 or Step 02 needs
(feature-importance clustering, denoising, ONC, etc.) must be designed the
same way from the start: a canonical, fully-tested implementation under
`shared/`, a Colab-portable copy of it under `shared/colab_portable/` (or
inlined directly into the edge script from that canonical text), and a sync
test enforcing agreement — not a plan that assumes Step 01/02 can import
`shared/` at runtime.
