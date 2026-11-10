# JPN Steps 01-04 after the 2026-10-26 re-export (Step 01 = the approved Ch.8 clustered method; Step 02 requires its
# provenance). Run serially per edge.
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
    $er = "assets\JPN\edges\" + $e
    foreach ($s in "01_feature_surgery_clustered_mda", "02_train_meta_model_optuna", "03_generate_oof_predictions", "03b_oof_threshold_sweep", "04_fit_sigmoid_calibrator_and_thresholds") {
        Step ($s + "_" + $e) @($er + "\research\" + $s + ".py")
    }
}
"$(Get-Date -Format s) CHAIN2 DONE" | Out-File -Append -Encoding utf8 "$log\chain.log"
