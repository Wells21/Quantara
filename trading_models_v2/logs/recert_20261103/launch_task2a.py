"""RECERT step 3a: Task 2A (label / economics split, D016) for the 4 edges in parallel (different assets), BELOW_NORMAL."""
import os, subprocess, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
V2 = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "step3"
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
ENV = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")
EDGES = ["GBPJPY/edges/dailyrangecompletionfade_long", "HK/edges/drawdownhazardcontinuation_short",
         "JPN/edges/rangestructure_long", "XAUUSD/edges/rangestructure_long"]
def run(e):
    tag = e.split("/")[0] + "_" + e.split("/")[-1]
    print(time.strftime("%H:%M:%S"), "START task2a", tag, flush=True)
    with open(OUT / f"task2a_{tag}.log", "w", encoding="utf-8") as f:
        rc = subprocess.call([PY, "build_mldp_executable_targets.py", "--edge-root", f"assets/{e}"], cwd=V2, env=ENV,
                             stdout=f, stderr=subprocess.STDOUT, creationflags=0x4000)
    print(time.strftime("%H:%M:%S"), "END task2a", tag, "rc", rc, flush=True)
with ThreadPoolExecutor(4) as ex:
    list(ex.map(run, EDGES))
print("ALL DONE", flush=True)
