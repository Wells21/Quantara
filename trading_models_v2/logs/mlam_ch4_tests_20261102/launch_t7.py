"""Run the MLAM Ch.4 T7 convergence jobs, 8 at a time, BELOW_NORMAL priority, 1 BLAS/OpenMP thread each."""
import os, subprocess, sys, time
from pathlib import Path
V2 = Path(__file__).resolve().parents[2]
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
OUT = "logs/mlam_ch4_tests_20261102"
DRV = "shared/research/mlam_ch4_tests.py"
EDGES4 = ["assets/GBPJPY/edges/dailyrangecompletionfade_long", "assets/HK/edges/drawdownhazardcontinuation_short",
          "assets/JPN/edges/rangestructure_long", "assets/XAUUSD/edges/rangestructure_long"]
EDGES6 = EDGES4 + ["assets/JPN/edges/volatilitycarry_long", "assets/XAUUSD/edges/volatilitycarry_long"]
jobs = []
jobs += [(f"t7_{e.split('/')[1]}_s{sd}", ["t7", e, "50", str(sd), OUT]) for e in EDGES4 for sd in range(5)]
env = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")
running, queue = [], list(jobs)
logs = V2 / OUT / "job_logs_t7"
logs.mkdir(parents=True, exist_ok=True)
while queue or running:
    while queue and len(running) < 8:
        name, args = queue.pop(0)
        f = open(logs / f"{name}.log", "w", encoding="utf-8")
        p = subprocess.Popen([PY, DRV] + args, cwd=V2, env=env, stdout=f, stderr=subprocess.STDOUT, creationflags=0x4000)
        running.append((name, p, f))
        print("start", name, flush=True)
    for item in list(running):
        name, p, f = item
        if p.poll() is not None:
            f.close(); running.remove(item)
            print("done", name, "rc", p.returncode, flush=True)
    time.sleep(5)
print("ALL DONE", flush=True)
