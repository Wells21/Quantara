"""Run the MLAM Ch.4 test jobs, 8 at a time, BELOW_NORMAL priority, 1 BLAS/OpenMP thread each."""
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
jobs += [("t2_" + e.split("/")[1] + "_" + ch, ["t2", e, ch, OUT]) for e in EDGES4 for ch in ("0,1,2,3,4", "5,6,7,8,9")]
jobs += [("t1_N80_K" + str(k), ["t1", "80", "20", OUT, str(k)]) for k in range(3, 41, 3)]
jobs += [("t1_N40", ["t1", "40", "20", OUT]), ("t1_N20", ["t1", "20", "20", OUT])]
jobs += [("t3_" + e.split("/")[1] + "_" + e.split("/")[3], ["t3", e, OUT]) for e in EDGES6]
env = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")
running, queue = [], list(jobs)
logs = V2 / OUT / "job_logs"
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
