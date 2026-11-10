#!/usr/bin/env bash
# Runs the Step01-09 research lifecycle for one edge, matching the exact
# log/status format already present in _lifecycle_logs/ from prior sessions.
# Usage: ./run_edge_lifecycle_01_09.sh ASSET FAMILY_SIDE
set -uo pipefail

ASSET="$1"
FAMILY="$2"
EDGE_ROOT="assets/${ASSET}/edges/${FAMILY}"
RESEARCH_DIR="${EDGE_ROOT}/research"
LOG_DIR="_lifecycle_logs/${ASSET}_${FAMILY}"
PYTHON_ABS="$(pwd)/../venv312/Scripts/python.exe"

mkdir -p "$LOG_DIR"
TIMING="$LOG_DIR/timing.log"
STATUS="$LOG_DIR/status.txt"

STEPS=(
  "01_feature_surgery_clustered_mda.py"
  "02_train_meta_model_optuna.py"
  "03_generate_oof_predictions.py"
  "03b_oof_threshold_sweep.py"
  "04_fit_sigmoid_calibrator_and_thresholds.py"
  "04b_lock_selected_features_and_model_input_schema.py"
  "05_training_artifact_verifier.py"
  "06_verify_runtime_scoring_ready.py"
  "07_runtime_score_smoke_test.py"
  "08_probability_calibration_afml_replay_audit.py"
  "09_export_edge_manifest.py"
)

echo "asset=${ASSET} edge=${FAMILY} started_at=$(date -Iseconds)" > "$TIMING"
rm -f "$STATUS"

for step in "${STEPS[@]}"; do
  script_path="${RESEARCH_DIR}/${step}"
  log_path="${LOG_DIR}/${step%.py}.log"
  echo "---- ${step} start $(date -Iseconds) ----" >> "$TIMING"
  start_ts=$(date +%s)
  PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 "$PYTHON_ABS" "./${script_path}" > "$log_path" 2>&1
  exit_code=$?
  end_ts=$(date +%s)
  elapsed=$((end_ts - start_ts))
  echo "${step} exit=${exit_code} elapsed_seconds=${elapsed}" >> "$TIMING"
  if [ "$exit_code" -ne 0 ]; then
    echo "STEPS_01_09_FAILED_AT_${step%.py}" > "$STATUS"
    echo "asset=${ASSET} edge=${FAMILY} finished_at=$(date -Iseconds) result=FAILED_AT_${step%.py}" >> "$TIMING"
    exit 1
  fi
done

echo "STEPS_01_09_COMPLETE" > "$STATUS"
echo "asset=${ASSET} edge=${FAMILY} finished_at=$(date -Iseconds) result=ALL_STEPS_01_09_COMPLETE" >> "$TIMING"
exit 0
