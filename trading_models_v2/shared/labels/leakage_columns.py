"""Canonical forbidden/leakage column list for the MLDP executable-target
architecture (Task 2A/2B/A2).

Every column here is either a training-target/eligibility column, a gross-
layer duplicate, or a Task 2A execution-provenance column present in
`<edge_stem>_mldp_execution_resolution.csv` -- none of these may ever be a
selected model-input feature, since each one either encodes the label
itself or information only knowable after the event resolved (a direct
lookahead/leakage channel).

WHY THIS FILE IS NOT IMPORTED BY STEP 01/02: those two research stations
run standalone on Google Colab and cannot import `shared/` at runtime (see
docs/mldp/A3_PURGE_EMBARGO_VERIFICATION.md's "Operational note"). They
carry their own inlined copy of this exact set instead, verified against
this file by tests/test_leakage_columns_sync.py -- the same drift-detection
discipline used for the purge/embargo, denoise/ONC, and trial-ledger
Colab-portable snippets. Every OTHER station in the edge pipeline (03, 03B,
04, 05, 07, 08, ...) runs locally and imports this module directly instead
of maintaining its own copy.
"""
from __future__ import annotations

TARGET_COL = "quote_executable_meta_label"
ELIGIBILITY_COL = "mldp_target_eligible"
GROSS_TARGET_COL = "gross_meta_label"
GROSS_REALIZED_R_COL = "gross_r"
T1_COL = "gross_t1"

SCORE_OUTPUT_OVERLAP: frozenset[str] = frozenset(
    {
        # Deliberate score-flow output columns (see shared/calibration/sigmoid_afml.py
        # and REQUIRED_SCORE_COLUMNS in shared/edges/runtime_score_smoke_test.py):
        # these names also appear in LEAKAGE_COLS below because the same strings
        # are diagnostic/label-adjacent columns in RESEARCH data, but they are the
        # intended output of a live score row, not leaked labels.
        "raw_probability",
        "calibrated_probability",
        "afml_bet_size",
        "approved_signal",
        # Passthrough/provenance columns EdgeScorer.score() and runtime.score()
        # always attach to a live scored row (see shared/scoring/edge_scorer.py):
        # Bar_Start comes straight from the input bar row, execution_contract_version
        # is a deliberate provenance stamp -- neither is label leakage.
        "Bar_Start",
        "execution_contract_version",
    }
)
"""Columns that are forbidden as a Step01/02/03 selected model-input FEATURE
(via LEAKAGE_COLS below) but are legitimate, intended columns of a live
runtime score OUTPUT row. Any check against a scored/live row (Step 07, 08)
must subtract this set from LEAKAGE_COLS before intersecting; any check
against selected features or research model-row inputs (Step 01/02/03/05)
must use LEAKAGE_COLS unmodified."""

LEAKAGE_COLS: frozenset[str] = frozenset(
    {
        TARGET_COL,
        ELIGIBILITY_COL,
        GROSS_TARGET_COL,
        GROSS_REALIZED_R_COL,
        T1_COL,
        "t1",
        "barrier_hit",
        "Bar_Start",
        "entry_price",
        "pt_price",
        "sl_price",
        "realized_r",
        "meta_label",
        "pt_hit_label",
        "executed_meta_label",
        "resolution_source",
        "raw_probability",
        "calibrated_probability",
        "afml_bet_size",
        "approved_signal",
        # Task 2A/2B executable-target resolution columns:
        "source_event_row_number",
        "trgt",
        "geometry_hash",
        "execution_status",
        "execution_contract_version",
        "execution_geometry_hash",
        "signal_mid",
        "vertical_deadline",
        "entry_time",
        "entry_bid",
        "entry_ask",
        "entry_spread",
        "entry_spread_bps",
        "entry_lag_ms",
        "entry_drift_bps",
        "stop_distance_price",
        "take_profit_distance_price",
        "exit_time",
        "exit_price",
        "exit_reason",
        "executed_r",
        "hold_seconds",
        "entry_age_seconds",
        "max_signal_age_seconds",
        "quote_executable_r",
        "quote_executable_t1",
        "gross_to_quote_label_flip",
        "mldp_training_authority",
    }
)
