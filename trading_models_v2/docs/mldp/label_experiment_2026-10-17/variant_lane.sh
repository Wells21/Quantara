#!/usr/bin/env bash
# usage: variant_lane.sh ASSET EDGE   -- backup, train Steps 01-03 under labels A,B,C, save OOF, restore.
S="/c/Users/ADMINI~1/AppData/Local/Temp/claude/C--Users-Administrator-Desktop-Trading-Models/8d611d2f-e7c6-4755-97e5-a6f32a8699aa/scratchpad"
SW="$(cygpath -w "$S")"; PY="/c/Users/Administrator/Desktop/Trading Models/venv312/Scripts/python.exe"
R="/c/Users/Administrator/Desktop/Trading Models/trading_models_v2"
wmic process where ProcessId=$(cat /proc/$$/winpid) CALL setpriority 16384 >/dev/null 2>&1
export PYTHONPATH="$(cygpath -w "$R")" PYTHONIOENCODING=utf-8
A=$1; E=$2; cd "$R" || exit 1
ER="assets/$A/edges/$E"; RES=$(ls $ER/artifacts/research_dataset/*_mldp_execution_resolution.csv)
B="${S}/labelexp/backup/${A}_${E}"; X="${S}/labelexp/exp/${A}_${E}"; mkdir -p "$X"
if [ ! -f "$B/DONE" ]; then
  mkdir -p "$B/artifacts"
  for x in calibration feature_surgery model oof thresholds selected_features.json edge_manifest.json; do
    [ -e "$ER/artifacts/$x" ] && cp -a "$ER/artifacts/$x" "$B/artifacts/"; done
  [ -d "$ER/reports" ] && cp -a "$ER/reports" "$B/reports"
  cp -a "$RES" "$B/resolution.csv"; touch "$B/DONE"; echo "$(date +%T) BACKUP $A/$E"
fi
for V in A B C; do
  "$PY" "${SW}/labelexp/set_label.py" "$(cygpath -w "$B/resolution.csv")" "$RES" $V "${SW}/labelexp/repriced" $A $E > "$X/${V}_label.log" 2>&1 || { echo "$(date +%T) FAIL $A/$E $V set_label"; continue; }
  ok=1
  for st in 01_feature_surgery_clustered_mda 02_train_meta_model_optuna 03_generate_oof_predictions; do
    "$PY" "$ER/research/$st.py" > "$X/${V}_$st.log" 2>&1 || { echo "$(date +%T) FAIL $A/$E $V $st :: $(tail -2 "$X/${V}_$st.log" | tr '\n' ' ' | cut -c1-200)"; ok=0; break; }
  done
  if [ $ok = 1 ]; then
    cp "$ER"/artifacts/oof/*_oof_predictions.csv "$X/${V}_oof.csv"
    cp "$ER"/artifacts/selected_features.json "$X/${V}_selected_features.json" 2>/dev/null
    cp "$ER"/artifacts/model/*_best_params.json "$X/${V}_best_params.json" 2>/dev/null
    echo "$(date +%T) VARIANT_DONE $A/$E $V :: $(cat "$X/${V}_label.log" | tr '\n' ' ')"
  fi
done
# restore originals exactly
cp -a "$B/resolution.csv" "$RES"
for x in calibration feature_surgery model oof thresholds; do [ -e "$B/artifacts/$x" ] && { rm -rf "${ER:?}/artifacts/$x"; cp -a "$B/artifacts/$x" "$ER/artifacts/"; }; done
for x in selected_features.json edge_manifest.json; do [ -e "$B/artifacts/$x" ] && cp -a "$B/artifacts/$x" "$ER/artifacts/"; done
[ -d "$B/reports" ] && { rm -rf "${ER:?}/reports"; cp -a "$B/reports" "$ER/reports"; }
cmp -s "$B/resolution.csv" "$RES" && echo "$(date +%T) LANE_DONE $A/$E restored_ok" || echo "$(date +%T) LANE_DONE $A/$E RESTORE_MISMATCH"
