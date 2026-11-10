#!/usr/bin/env bash
# History-extension rebuild for one asset (owner approval 2026-11-07). Same commands and parameters as the
# 2026-10-23 curated rebuild (logs/rebuild_curated_JPN_20261023.log) and the Ch.5 FFD/HMM rebuild. Stops on the
# first failure. Archives the current artifacts first (copy, outside the repo).
# usage: bash docs/tools/rebuild_asset_history_20261107.sh NASDAQ [FROM_STEP]   (FROM_STEP 0-8, default 0)
set -euo pipefail
A="$1"; FROM="${2:-0}"; a=$(echo "$A" | tr '[:upper:]' '[:lower:]')
cd "/c/Users/Administrator/Desktop/Trading Models/trading_models_v2"
PY="../venv312/Scripts/python.exe"
export PYTHONIOENCODING=utf-8   # builders print emoji; cp1252 redirected stdout raised (JPN 2026-11-07)
ARCH="/c/Users/Administrator/Desktop/Quantara_archive_bars_pre_history_20261107/${A}"
LOG="logs/history_rebuild_20261107/${A}.log"
mkdir -p logs/history_rebuild_20261107
# research at BelowNormal (live bot shares the machine); child processes inherit the priority class
powershell.exe -NoProfile -Command "(Get-Process -Id $(cat /proc/$$/winpid)).PriorityClass = 'BelowNormal'" || true
step() { [ "$1" -ge "$FROM" ]; }
run() { echo "[$(date -u +%FT%TZ)] \$ $*" >> "$LOG"; "$@" >> "$LOG" 2>&1; }

echo "[$(date -u +%FT%TZ)] START ${A} from step ${FROM}" >> "$LOG"
# 0. archive (copy) current bars, curated reports, live continuation files, minute-vol cache
if step 0; then
mkdir -p "${ARCH}/curated" "${ARCH}/live_feature_continuation" "${ARCH}/dukascopy_minute_vol"
cp -r "assets/${A}/bars" "${ARCH}/bars"
cp assets/${A}/curated/*.json "${ARCH}/curated/"
cp portfolio_live/outputs/live_feature_continuation/${a}_* "${ARCH}/live_feature_continuation/" 2>/dev/null || true
cp portfolio_allocator/outputs/dukascopy_minute_vol/minute_rv1d_${a}.npz "${ARCH}/dukascopy_minute_vol/" 2>/dev/null || true
echo "[$(date -u +%FT%TZ)] archived to ${ARCH}" >> "$LOG"
fi
# 1. move staged history into raw/ticks
step 1 && run "$PY" docs/tools/move_history_staging_to_raw.py --asset "$A"
# 2. curate (causal quote filter; USOIL backward roll adjustment)
step 2 && run "$PY" shared/data/tick_curation.py curate --assets "$A"
# 3. forge bars
step 3 && run "$PY" shared/bars/forge_asset_standard_quote_tick_bars.py --asset "$A" --overwrite --engine streaming \
    --target-bars-per-day 50 --min-warmup-days 20 --daily-tick-ewma-span-days 20
# 4. bar quality report
step 4 && run "$PY" shared/bars/bar_quality_report.py --assets "$A"
# 5. FFD (d on full research history, frozen), 6. HMM, 7. entropy encodings
step 5 && run "$PY" shared/ffd/build_asset_ffd_features.py --asset "$A" --overwrite
step 6 && run "$PY" shared/regime/build_asset_hmm_regime_features.py --asset "$A" --overwrite
step 7 && run "$PY" shared/features/build_asset_entropy_encoding.py "$A"
# 8. broker cost model minute-vol cache
step 8 && run "$PY" -c "import sys; from pathlib import Path; sys.path.insert(0, 'portfolio_allocator'); from broker_cost_model import vol_cache; print(vol_cache.build(Path('.').resolve(), '${A}', Path('portfolio_allocator/outputs/dukascopy_minute_vol')))"
echo "[$(date -u +%FT%TZ)] DONE ${A}" >> "$LOG"
