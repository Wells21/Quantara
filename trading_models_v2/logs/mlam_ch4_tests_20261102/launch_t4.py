"""T4 jobs (4 edges), BELOW_NORMAL, 1 thread each."""
import os, subprocess
from pathlib import Path
V2 = Path(__file__).resolve().parents[2]
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
OUT = "logs/mlam_ch4_tests_20261102"
env = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
ps = []
for e in ["assets/GBPJPY/edges/dailyrangecompletionfade_long", "assets/HK/edges/drawdownhazardcontinuation_short",
          "assets/JPN/edges/rangestructure_long", "assets/XAUUSD/edges/rangestructure_long"]:
    f = open(V2 / OUT / "job_logs" / f"t4_{e.split('/')[1]}.log", "w", encoding="utf-8")
    ps.append(subprocess.Popen([PY, "shared/research/mlam_ch4_tests.py", "t4", e, OUT], cwd=V2, env=env, stdout=f,
                               stderr=subprocess.STDOUT, creationflags=0x4000))
for p in ps:
    p.wait()
print("T4 DONE", [p.returncode for p in ps], flush=True)
