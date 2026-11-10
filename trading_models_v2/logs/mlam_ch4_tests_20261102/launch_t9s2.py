"""Run the MLAM Ch.4 T9 stage-2 selection jobs, 4 at a time (2 fold workers each), BELOW_NORMAL priority, 1 BLAS/OpenMP thread each."""
import os, subprocess, sys, time
from pathlib import Path
V2 = Path(__file__).resolve().parents[2]
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
OUT = "logs/mlam_ch4_tests_20261102"
DRV = "shared/research/mlam_ch4_tests.py"
EDGES4 = ["assets/GBPJPY/edges/dailyrangecompletionfade_long", "assets/HK/edges/drawdownhazardcontinuation_short",
          "assets/JPN/edges/rangestructure_long", "assets/XAUUSD/edges/rangestructure_long"]
EDGES6 = EDGES4 + ["assets/JPN/edges/volatilitycarry_long", "assets/XAUUSD/edges/volatilitycarry_long"]
SEEDS = {"GBPJPY/edges/dailyrangecompletionfade_long": [9, 11], "HK/edges/drawdownhazardcontinuation_short": [20],
         "JPN/edges/rangestructure_long": [6, 15, 27], "JPN/edges/volatilitycarry_long": [10],
         "XAUUSD/edges/volatilitycarry_long": [5]}
jobs = [(f"t8_{e.split('/')[0]}_{e.split('/')[2]}_s{sd}", ["t8", "assets/" + e, str(sd), OUT]) for e, ss in SEEDS.items() for sd in ss]
env = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8", QUANTARA_STEP01_WORKERS="2")
running, queue = [], list(jobs)
logs = V2 / OUT / "job_logs_t9s2"
logs.mkdir(parents=True, exist_ok=True)
while queue or running:
    while queue and len(running) < 4:
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
