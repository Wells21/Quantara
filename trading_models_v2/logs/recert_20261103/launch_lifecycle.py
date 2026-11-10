"""RECERT step 3b: Steps 01-09 (run_edge_lifecycle_01_09.sh, venv312) then Gate D per edge. 4 lanes (one per asset),
QUANTARA_STEP01_WORKERS=2 (Steps 02-09 are single-process), BELOW_NORMAL, 1 BLAS thread per process."""
import os, subprocess, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
V2 = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "step3"
BASH = r"C:\Program Files\Git\bin\bash.exe"
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
ENV = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8",
           QUANTARA_STEP01_WORKERS="2")
EDGES = [("GBPJPY", "dailyrangecompletionfade_long"), ("HK", "drawdownhazardcontinuation_short"),
         ("JPN", "rangestructure_long"), ("XAUUSD", "rangestructure_long")]
def log(m): print(time.strftime("%Y-%m-%dT%H:%M:%S"), m, flush=True)
def lane(a, f):
    tag = f"{a}_{f}"
    log(f"START lifecycle {tag}")
    rc = subprocess.call([BASH, "run_edge_lifecycle_01_09.sh", a, f], cwd=V2, env=ENV, creationflags=0x4000,
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    log(f"END lifecycle {tag} rc {rc}")
    if rc != 0:
        return
    log(f"START gate_d {tag}")
    with open(OUT / f"gate_d_{tag}.log", "w", encoding="utf-8") as fh:
        rc = subprocess.call([PY, "shared/edges/gate_d_calibration_monotonicity.py", "--edge-root", f"assets/{a}/edges/{f}",
                              "--write-report"], cwd=V2, env=ENV, stdout=fh, stderr=subprocess.STDOUT, creationflags=0x4000)
    log(f"END gate_d {tag} rc {rc}")
with ThreadPoolExecutor(4) as ex:
    list(ex.map(lambda e: lane(*e), EDGES))
log("ALL DONE")
