$root = "C:\Users\Administrator\Desktop\Trading Models\trading_models_v2"
$py = "C:\Users\Administrator\Desktop\Trading Models\venv312\Scripts\python.exe"
Set-Location $root
foreach ($pair in @(@("rangestructure_long","20261026T175846Z"), @("volatilitycarry_long","20261026T180045Z"))) {
    $e = $pair[0]; $s = $pair[1]
    "$(Get-Date -Format s) START gatec_JPN_$e" | Out-File -Append -Encoding utf8 "logs\ch11_full405_20261026\chain_JPN_gatec.log"
    $p = Start-Process -FilePath $py -ArgumentList @("shared\research\gate_c_runner.py", "--edge-root", ("assets\JPN\edges\" + $e), "--grid-stamp", $s, "--cost-model", "portfolio_allocator\config\broker_profiles\moneta_cost_models\JPN.json", "--out-jsonl", ("logs\ch11_full405_20261026\JPN_" + $e + "_full405.jsonl"), "--batch-size", "120", "--all-cells") -WorkingDirectory $root -Wait -PassThru -NoNewWindow -RedirectStandardOutput ("logs\ch11_full405_20261026\gatec_JPN_" + $e + ".out.log") -RedirectStandardError ("logs\ch11_full405_20261026\gatec_JPN_" + $e + ".err.log")
    "$(Get-Date -Format s) END gatec_JPN_$e exit $($p.ExitCode)" | Out-File -Append -Encoding utf8 "logs\ch11_full405_20261026\chain_JPN_gatec.log"
}
