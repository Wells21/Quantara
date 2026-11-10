# EWM span test (PREREGISTRATION.md): Gate A 540 cells (EWM 100,150,200,300) then Gate C all cells, per edge.
# Lab calls go through grid_dimension_sensitivity/lab_with_schema_at_commit.py: feature schema as of 63f4a570 (the
# 168-entry schema the stored research datasets and the old full replays used; current schema needs the RECERT re-export).
# One lane per asset; same-asset edges serially (shared tick store / HMM cache).
param([string]$Which, [string]$Only = "")   # -Only <edge>: run just that edge of the asset (its own lane)
$ErrorActionPreference = "Continue"
$root = "C:\Users\Administrator\Desktop\Trading Models\trading_models_v2"
$py = "C:\Users\Administrator\Desktop\Trading Models\venv312\Scripts\python.exe"
$log = "$root\logs\ewm_span_test_20261031"
$cm = "portfolio_allocator\config\broker_profiles\moneta_cost_models"
Set-Location $root
$lanes = @{
    "GBPJPY" = @(,@("dailyrangecompletionfade_long", "0.7388"));
    "HK"     = @(,@("drawdownhazardcontinuation_short", "2.5179"));
    "JPN"    = @(@("rangestructure_long", "1.9231"), @("volatilitycarry_long", "1.9231"));
    "XAUUSD" = @(@("rangestructure_long", "0.2583"), @("volatilitycarry_long", "0.2583"))
}
function Run($name, $argsList) {
    "$(Get-Date -Format s) START $name" | Out-File -Append -Encoding utf8 "$log\chain_$Which$Only.log"
    $p = Start-Process -FilePath $py -ArgumentList $argsList -WorkingDirectory $root -PassThru -NoNewWindow `
        -RedirectStandardOutput "$log\$name.out.log" -RedirectStandardError "$log\$name.err.log"
    $null = $p.Handle   # cache the handle so ExitCode is available after the wait
    try { $p.PriorityClass = "BelowNormal" } catch {}
    $p.WaitForExit()
    "$(Get-Date -Format s) END $name exit $($p.ExitCode)" | Out-File -Append -Encoding utf8 "$log\chain_$Which$Only.log"
    return $p.ExitCode
}
foreach ($pair in $lanes[$Which]) {
    if ($Only -and $pair[0] -ne $Only) { continue }
    $e = $pair[0]; $cost = $pair[1]
    $er = "assets\$Which\edges\$e"
    $art = "$root\$er\artifacts\v3_horizon_lab"
    $before = @(Get-ChildItem $art -Filter "v3_hybrid_fast_grid_*.csv" | ForEach-Object Name)
    Run ("gateA_" + $Which + "_" + $e) @("shared\research\grid_dimension_sensitivity\lab_with_schema_at_commit.py", "--edge-root", $er, "--event-vol-lookbacks", "1,2,4,6,8", "--event-vol-ewm-spans", "100,150,200,300", "--vertical-horizons", "1,2,4,8,16,24,48,96,168", "--barriers", "1:1,1.5:1,2:1", "--cost-bps", $cost, "--hybrid-fast-grid", "--save") | Out-Null
    $new = @(Get-ChildItem $art -Filter "v3_hybrid_fast_grid_*.csv" | ForEach-Object Name | Where-Object { $before -notcontains $_ })
    if ($new.Count -ne 1) { "$(Get-Date -Format s) FAIL no new Gate A grid for $e" | Out-File -Append -Encoding utf8 "$log\chain_$Which$Only.log"; continue }
    $stamp = $new[0].Replace("v3_hybrid_fast_grid_", "").Replace(".csv", "")
    "$(Get-Date -Format s) GRID $e $stamp" | Out-File -Append -Encoding utf8 "$log\chain_$Which$Only.log"
    Run ("gatec_" + $Which + "_" + $e) @("shared\research\grid_dimension_sensitivity\gate_c_runner_schema_at_commit.py", "--edge-root", $er, "--grid-stamp", $stamp, "--cost-model", "$cm\$Which.json", "--out-jsonl", ("logs\ewm_span_test_20261031\" + $Which + "_" + $e + "_ewm.jsonl"), "--batch-size", "135", "--all-cells") | Out-Null
}
"$(Get-Date -Format s) CHAIN $Which DONE" | Out-File -Append -Encoding utf8 "$log\chain_$Which$Only.log"
