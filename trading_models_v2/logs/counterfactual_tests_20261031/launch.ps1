$root = "C:\Users\Administrator\Desktop\Trading Models\trading_models_v2"
$py = "C:\Users\Administrator\Desktop\Trading Models\venv312\Scripts\python.exe"
$log = "$root\logs\counterfactual_tests_20261031"
$jobs = @(@("GBPJPY","dailyrangecompletionfade_long"),@("HK","drawdownhazardcontinuation_short"),@("JPN","rangestructure_long"),@("JPN","volatilitycarry_long"),@("XAUUSD","rangestructure_long"),@("XAUUSD","volatilitycarry_long"))
foreach ($j in $jobs) {
  $n = "$($j[0])_$($j[1])"
  $p = Start-Process -FilePath $py -ArgumentList @("shared\research\counterfactual_tests\run_counterfactuals.py",$j[0],$j[1],"logs\counterfactual_tests_20261031") -WorkingDirectory $root -PassThru -NoNewWindow -RedirectStandardOutput "$log\$n.out.log" -RedirectStandardError "$log\$n.err.log"
  try { $p.PriorityClass = "BelowNormal" } catch {}
  "$n pid $($p.Id)"
}
