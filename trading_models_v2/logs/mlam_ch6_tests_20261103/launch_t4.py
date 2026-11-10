"""MLAM Ch.6 T4 (POST HOC): Step 01 on the 6 edges, D041 arm (mwfl 0.05) and Snippet 6.8 arm (mwfl 0), cached D077 ONC,
at most 4 jobs at once, BELOW_NORMAL priority, 1 BLAS thread per process."""
import os, subprocess, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
DRV = "shared/research/mlam_ch6_tests.py"
BASE = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")
BASE.pop("CH6_TREE_MWFL", None)
EDGES = ["assets/GBPJPY/edges/dailyrangecompletionfade_long", "assets/HK/edges/drawdownhazardcontinuation_short",
         "assets/JPN/edges/rangestructure_long", "assets/JPN/edges/volatilitycarry_long",
         "assets/XAUUSD/edges/rangestructure_long", "assets/XAUUSD/edges/volatilitycarry_long"]


def log(m):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), m, flush=True)


def run(e, mwfl):
    tag = f"t4_{e.split('/')[1]}_{e.split('/')[-1]}_{'mwfl0' if mwfl else 'd041'}"
    env = dict(BASE, CH6_TREE_MWFL="0") if mwfl else BASE
    (OUT / "job_logs").mkdir(exist_ok=True)
    with open(OUT / "job_logs" / f"{tag}.log", "w", encoding="utf-8") as f:
        log(f"START {tag}")
        rc = subprocess.call([PY, DRV, "t4", e, str(OUT)], cwd=V2, env=env, stdout=f, stderr=subprocess.STDOUT, creationflags=0x4000)
        log(f"END {tag} rc {rc}")
    return rc


with ThreadPoolExecutor(4) as ex:
    futs = [ex.submit(run, e, m) for m in (True, False) for e in EDGES]
    [f.result() for f in futs]
log("ALL DONE")
