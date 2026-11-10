$ErrorActionPreference = "Stop"
$v2 = "C:\Users\Administrator\Desktop\Trading Models\trading_models_v2"
$py = "C:\Users\Administrator\Desktop\Trading Models\venv312\Scripts\python.exe"
$out = Join-Path $v2 "logs\mlam_ch2_tests_20261102\d076_verify"
$jobs = @(
 @("assets/GBPJPY/edges/dailyrangecompletionfade_long","20261026T181822Z,20261026T183748Z,20261026T185441Z,20261026T190522Z","GBPJPY_dailyrangecompletionfade_long"),
 @("assets/HK/edges/drawdownhazardcontinuation_short","20261026T180733Z,20261026T181639Z,20261026T182525Z,20261026T183152Z","HK_drawdownhazardcontinuation_short"),
 @("assets/JPN/edges/rangestructure_long","20261026T181130Z,20261026T182111Z,20261026T183137Z,20261026T183701Z","JPN_rangestructure_long"),
 @("assets/XAUUSD/edges/rangestructure_long","20261017T130525Z,20261017T132521Z,20261017T134354Z,20261017T135723Z","XAUUSD_rangestructure_long")
)
$env:OMP_NUM_THREADS = "2"; $env:MKL_NUM_THREADS = "2"; $env:OPENBLAS_NUM_THREADS = "2"
$procs = @()
foreach ($j in $jobs) {
  $p = Start-Process -FilePath $py -ArgumentList @("-m","shared.research.dsr_all_trials","--edge-root",$j[0],"--stamps",$j[1],"--out",("logs/mlam_ch2_tests_20261102/d076_verify/dsr_" + $j[2] + ".json")) -WorkingDirectory $v2 -NoNewWindow -PassThru -RedirectStandardOutput (Join-Path $out ($j[2] + ".log")) -RedirectStandardError (Join-Path $out ($j[2] + ".err"))
  $p.PriorityClass = "BelowNormal"; $procs += $p
}
$procs | Wait-Process
"done"
