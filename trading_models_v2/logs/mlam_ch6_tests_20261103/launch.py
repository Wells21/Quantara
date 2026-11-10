"""MLAM Ch.6 test launcher: T1 (sigma 0.1, 0) and per-edge lanes (t2 then t3, sharing the cached ONC partition),
at most 4 lanes at once, BELOW_NORMAL priority, 1 BLAS thread per process."""
import os, subprocess, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
DRV = "shared/research/mlam_ch6_tests.py"
ENV = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")
EDGES = ["assets/GBPJPY/edges/dailyrangecompletionfade_long", "assets/HK/edges/drawdownhazardcontinuation_short",
         "assets/JPN/edges/rangestructure_long", "assets/JPN/edges/volatilitycarry_long",
         "assets/XAUUSD/edges/rangestructure_long", "assets/XAUUSD/edges/volatilitycarry_long"]


def log(m):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), m, flush=True)


def run(tag, args):
    (OUT / "job_logs").mkdir(exist_ok=True)
    with open(OUT / "job_logs" / f"{tag}.log", "w", encoding="utf-8") as f:
        log(f"START {tag}")
        rc = subprocess.call([PY, DRV] + args, cwd=V2, env=ENV, stdout=f, stderr=subprocess.STDOUT, creationflags=0x4000)
        log(f"END {tag} rc {rc}")
    return rc


def lane(e):
    name = e.split("/")[1] + "_" + e.split("/")[-1]
    return [run(f"t2_{name}", ["t2", e, str(OUT)]), run(f"t3_{name}", ["t3", e, str(OUT)])]


with ThreadPoolExecutor(4) as ex:
    futs = [ex.submit(run, "t1_s0.1", ["t1", "0.1", str(OUT)]), ex.submit(run, "t1_s0", ["t1", "0", str(OUT)])]
    futs += [ex.submit(lane, e) for e in EDGES]
    [f.result() for f in futs]
log("ALL DONE")
