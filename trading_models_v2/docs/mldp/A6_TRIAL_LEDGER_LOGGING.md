# A6 — Research Trial Ledger Writer

## Status

`WRITER BUILT AND TESTED — LEDGER STILL EMPTY, CORRECTLY`. Per the
blueprint's P0.4 (AFML Ch.11's "record every backtest ever run"; MLAM Ch.8's
False Strategy theorem, which requires an honest count of trials to compute
`E[max_k{SR_k}]` and the Deflated Sharpe Ratio): every outcome-informed
research choice is a trial and must be appended, never left unrecorded. The
schema (`config/research_trial_ledger.schema.json`) has existed since Task 1;
until this pass, nothing wrote to it.

## What was built, and why it looks like A3/A4 again

Step 02 (Optuna hyperparameter search plus CPCV model training) is exactly
the station that generates the most trials per edge, and it is one of the
two stations (with Step 01) that run standalone on Google Colab and cannot
import the rest of `shared/` at runtime. The same pattern established in A3
(`docs/mldp/A3_PURGE_EMBARGO_VERIFICATION.md`) and A4
(`docs/mldp/A4_DENOISE_ONC_UTILITY.md`) applies here:

- **`shared/colab_portable/trial_ledger_snippet.py`** — the canonical,
  dependency-free (stdlib only: `json`, `uuid`, `datetime`, `pathlib`) writer:
  `build_trial_record`, `validate_trial_record` (a lightweight, hardcoded
  check of the schema's required fields/types -- no `jsonschema` package
  dependency, since that isn't guaranteed on Colab), `append_trial_record`
  (single JSON line per trial, genuinely append-only), and
  `read_trial_records`.
- **`shared/research/trial_ledger.py`** — the local re-export wrapper, for
  locally-run stations (portfolio_allocator scripts, shared/ code). Adds two
  things that only make sense running locally:
  - `validate_against_json_schema`, which reads
    `config/research_trial_ledger.schema.json` directly rather than relying
    on the Colab snippet's hardcoded field list -- stronger, and it
    automatically tracks any future schema change with no separate sync
    step, since it re-reads the real file on every call.
  - `merge_trial_records`, for combining trial records a Colab-run Step
    01/02 script wrote to its own local JSONL (as part of that station's
    downloaded artifacts) into the canonical ledger, de-duplicating by
    `trial_id` against what the ledger already contains.
- **`tests/test_trial_ledger_schema_sync.py`** — the same drift-detection
  discipline as `tests/test_colab_portable_snippet_sync.py`: asserts the
  Colab snippet's hardcoded `REQUIRED_FIELDS` never drifts from the real
  schema file's `required` array.
- **`tests/test_trial_ledger.py`** (14 tests) — build/validate/append/read
  round trips, rejection of invalid `trial_count_quality` and wrong field
  types, append-only-ness (an existing line is never rewritten), the
  `log_trial` convenience wrapper, and three merge scenarios (appending from
  multiple sources, de-duplication by `trial_id`, and skipping an invalid
  record without raising or corrupting the ledger).

All 17 tests (16 + the schema-sync test) pass.

**Correction made after initial implementation:** `shared/research/trial_ledger.py`
already existed in git history with real functionality this pass had not
accounted for -- file-locking around each append (exclusive-create lock
file, so two concurrent writers cannot interleave partial lines), an
`fsync` before releasing the lock, and duplicate-`trial_id` detection on
read. That file was apparently absent from the working tree when this pass
began (probably removed in the same manual cleanup that also removed the
root-level `patch_*.py` scripts, since it was recreated the same way) and so
was rewritten from scratch without that context. Once found (via `git show
HEAD:...` while preparing to commit), the original's locking/fsync/duplicate-
detection behavior was folded into `shared/colab_portable/trial_ledger_snippet.py`
alongside this pass's additions (the Colab-portable split, live-schema-file
validation, the merge utility) rather than either discarding it or silently
keeping a weaker rewrite. Two tests were added to cover it explicitly
(`test_append_cleans_up_its_lock_file`, `test_read_raises_on_duplicate_trial_id`).

## Why the ledger is still empty, and that this is correct

Stage A (A1-A6) built shared infrastructure and made governance decisions --
it did not run any Optuna search, select any threshold, or make any other
outcome-informed research choice about a specific edge. There is nothing
genuine to log yet, and per the same "never invent semantics" principle this
whole migration is built on, no placeholder or synthetic trial record was
written just to populate the ledger. The writer is ready; Stage B's actual
pilot-edge rebuild (Task 2C) is where the first real entries will come from.

## What Stage B must actually do with this

When Task 2C rebuilds `BTCUSD/absorption_long`'s Step 02 script:

1. Copy `shared/colab_portable/trial_ledger_snippet.py`'s functions
   (verbatim, per the established Colab-portable pattern) into the Colab-run
   script, or write trial records to a local JSONL there using the same
   format.
2. Log **one trial record per Optuna study** (not one per individual
   internal Optuna trial) -- `parameters` should capture the search space
   and the number of trials run; `result_artifacts` should point at the full
   per-trial Optuna results file/study database, from which the exact
   internal trial count can always be reconstructed later if needed. Set
   `trial_count_quality: "exact"` for this, since Optuna's own study object
   does record every individual trial exactly, even though the ledger only
   carries one summary row per study -- this keeps the ledger from
   ballooning by hundreds of rows per edge while still preserving full
   traceability via `result_artifacts`.
3. After downloading Step 02's artifacts back, run
   `shared.research.trial_ledger.merge_trial_records` locally to fold that
   trial record into the canonical `config/research_trial_ledger.jsonl`.
4. Any other outcome-informed choice made during the pilot rebuild --
   threshold selection, geometry decisions, feature-set surgery -- gets its
   own trial record the same way, logged locally via `log_trial` if the
   station making that choice runs locally, or via the Colab pattern above
   if it doesn't.

## What remains explicitly out of scope for A6

**Historical backfill** (`historical_trial_ledger_complete: false` in
`docs/mldp/TRANSFORMATION_STATE.json`) -- reconstructing trial records for
V3's existing historical Optuna/geometry-scan/threshold-selection artifacts
across the full edge universe -- is a separate, much larger effort and was
not attempted here. It does not block Stage B: new trials from this point
forward will be completely and honestly recorded regardless of whether the
historical backlog is ever reconstructed, and where it cannot be
reconstructed exactly, `trial_count_quality: "unknown"` or `"lower_bound"`
plus `historical_backfill: true` is the correct, honest way to record it --
per the blueprint's own guidance, not invented here.
