# AFML s.11.6 all-N PBO (owner 2026-10-26): fresh Gate C replay of ALL 405 cells, one consistent run per edge.
# GBPJPY / HK: cell list from their 2026-10-17 Gate A grids (research data unchanged). JPN: fresh Gate A first (data
# re-exported 2026-10-26), then all 405 cells; JPN edges serially (shared tick store).
param([string]$Which)
$ErrorActionPreference = "Continue"
$root = "C:\Users\Administrator\Desktop\Trading Models\trading_models_v2"
$py = "C:\Users\Administrator\Desktop\Trading Models\venv312\Scripts\python.exe"
$log = "$root\logs\ch11_full405_20261026"
$cm = "portfolio_allocator\config\broker_profiles\moneta_cost_models"
Set-Location $root
function Run($name, $argsList) {
    "$(Get-Date -Format s) START $name" | Out-File -Append -Encoding utf8 "$log\chain_$Which.log"
    $p = Start-Process -FilePath $py -ArgumentList $argsList -WorkingDirectory $root -Wait -PassThru -NoNewWindow `
        -RedirectStandardOutput "$log\$name.out.log" -RedirectStandardError "$log\$name.err.log"
    "$(Get-Date -Format s) END $name exit $($p.ExitCode)" | Out-File -Append -Encoding utf8 "$log\chain_$Which.log"
    return $p.ExitCode
}
if ($Which -eq "GBPJPY") {
    Run "gatec_GBPJPY" @("shared\research\gate_c_runner.py", "--edge-root", "assets\GBPJPY\edges\dailyrangecompletionfade_long", "--grid-stamp", "20261017T124333Z", "--cost-model", "$cm\GBPJPY.json", "--out-jsonl", "logs\ch11_full405_20261026\GBPJPY_full405.jsonl", "--batch-size", "120", "--all-cells") | Out-Null
}
if ($Which -eq "HK") {
    Run "gatec_HK" @("shared\research\gate_c_runner.py", "--edge-root", "assets\HK\edges\drawdownhazardcontinuation_short", "--grid-stamp", "20261017T124631Z", "--cost-model", "$cm\HK.json", "--out-jsonl", "logs\ch11_full405_20261026\HK_full405.jsonl", "--batch-size", "120", "--all-cells") | Out-Null
}
if ($Which -eq "JPN") {
    foreach ($e in "rangestructure_long", "volatilitycarry_long") {
        $er = "assets\JPN\edges\" + $e
        $art = "$root\$er\artifacts\v3_horizon_lab"
        $before = @(Get-ChildItem $art -Filter "v3_hybrid_fast_grid_*.csv" | ForEach-Object Name)
        Run ("gateA_JPN_" + $e) @("shared\research\v3_horizon_lab.py", "--edge-root", $er, "--event-vol-lookbacks", "1,2,4,6,8", "--event-vol-ewm-spans", "20,50,100", "--vertical-horizons", "1,2,4,8,16,24,48,96,168", "--barriers", "1:1,1.5:1,2:1", "--cost-bps", "1.9231", "--hybrid-fast-grid", "--save") | Out-Null
        $new = @(Get-ChildItem $art -Filter "v3_hybrid_fast_grid_*.csv" | ForEach-Object Name | Where-Object { $before -notcontains $_ })
        if ($new.Count -ne 1) { "$(Get-Date -Format s) FAIL no new Gate A grid for $e" | Out-File -Append -Encoding utf8 "$log\chain_$Which.log"; continue }
        $stamp = $new[0].Replace("v3_hybrid_fast_grid_", "").Replace(".csv", "")
        Run ("gatec_JPN_" + $e) @("shared\research\gate_c_runner.py", "--edge-root", $er, "--grid-stamp", $stamp, "--cost-model", "$cm\JPN.json", "--out-jsonl", ("logs\ch11_full405_20261026\JPN_" + $e + "_full405.jsonl"), "--batch-size", "120", "--all-cells") | Out-Null
    }
}
"$(Get-Date -Format s) CHAIN $Which DONE" | Out-File -Append -Encoding utf8 "$log\chain_$Which.log"
