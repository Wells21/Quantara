cd "/c/Users/Administrator/Desktop/Trading Models/trading_models_v2"
powershell.exe -NoProfile -Command "(Get-Process -Id $(cat /proc/$$/winpid)).PriorityClass = 'BelowNormal'" || true
OUT=docs/mldp/campaign/NASDAQ_2026-11/T14_high_vol_liquidity_provision
export OMP_NUM_THREADS=2 MKL_NUM_THREADS=2
../venv312/Scripts/python.exe shared/research/gate0/gate0_tool.py T14 $OUT "" "" "" > $OUT/run.log 2>&1 &
../venv312/Scripts/python.exe shared/research/gate0/gate0_tool.py T14 $OUT 2014-01-01 2022-01-01 _2014_2021 > $OUT/run_a.log 2>&1 &
../venv312/Scripts/python.exe shared/research/gate0/gate0_tool.py T14 $OUT 2022-01-01 "" _2022_on > $OUT/run_b.log 2>&1 &
wait
echo ALLDONE
