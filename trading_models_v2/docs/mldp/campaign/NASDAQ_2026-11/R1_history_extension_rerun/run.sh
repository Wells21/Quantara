cd "/c/Users/Administrator/Desktop/Trading Models/trading_models_v2"
powershell.exe -NoProfile -Command "(Get-Process -Id $(cat /proc/$$/winpid)).PriorityClass = 'BelowNormal'" || true
OUT=docs/mldp/campaign/NASDAQ_2026-11/R1_history_extension_rerun
for T in T1c T1bc T2 T3 T4 T5 T6 T7 T10 T11; do
  ../venv312/Scripts/python.exe shared/research/gate0/gate0_tool.py $T $OUT "" "" "" > $OUT/run_$T.log 2>&1 || echo "FAIL $T"
  ../venv312/Scripts/python.exe shared/research/gate0/gate0_tool.py $T $OUT 2014-01-01 2022-01-01 _2014_2021 > $OUT/run_${T}_2014_2021.log 2>&1 || echo "FAIL $T a"
  ../venv312/Scripts/python.exe shared/research/gate0/gate0_tool.py $T $OUT 2022-01-01 "" _2022_on > $OUT/run_${T}_2022_on.log 2>&1 || echo "FAIL $T b"
  echo "done $T"
done
