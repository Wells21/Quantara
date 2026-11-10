# JPN research re-export on the current (2026-10-23 cleaned) bars, then Task 2A labels and Steps 02-04 (selected
# features kept locked; Step 01 re-selection belongs to the retrain batch). Exports run serially (shared HMM cache).
$ErrorActionPreference = "Continue"
$root = "C:\Users\Administrator\Desktop\Trading Models\trading_models_v2"
$py = "C:\Users\Administrator\Desktop\Trading Models\venv312\Scripts\python.exe"
$log = "$root\logs\jpn_reexport_20261026"
$env:OMP_NUM_THREADS = "2"; $env:MKL_NUM_THREADS = "2"; $env:OPENBLAS_NUM_THREADS = "2"
Set-Location $root
function Step($name, $argsList) {
    "$(Get-Date -Format s) START $name" | Out-File -Append -Encoding utf8 "$log\chain.log"
    $p = Start-Process -FilePath $py -ArgumentList $argsList -WorkingDirectory $root -Wait -PassThru -NoNewWindow `
        -RedirectStandardOutput "$log\$name.out.log" -RedirectStandardError "$log\$name.err.log"
    if ($p.ExitCode -ne 0) { "$(Get-Date -Format s) FAIL $name exit $($p.ExitCode)" | Out-File -Append -Encoding utf8 "$log\chain.log"; exit 1 }
    "$(Get-Date -Format s) OK $name" | Out-File -Append -Encoding utf8 "$log\chain.log"
}
foreach ($e in "rangestructure_long", "volatilitycarry_long") {
    $er = "assets\JPN\edges\$e"
    Step "export_$e" @("$er\export_research_dataset.py", "--overwrite")
    Step "task2a_$e" @("build_mldp_executable_targets.py", "--edge-root", $er)
}
foreach ($e in "rangestructure_long", "volatilitycarry_long") {
    $er = "assets\JPN\edges\$e"
    foreach ($s in "02_train_meta_model_optuna", "03_generate_oof_predictions", "03b_oof_threshold_sweep", "04_fit_sigmoid_calibrator_and_thresholds") {
        Step "${s}_$e" @("$er\research\$s.py")
    }
}
"$(Get-Date -Format s) CHAIN DONE" | Out-File -Append "$log\chain.log"
