"""POST HOC T3r: for each edge, wait for its T3 output, then run t3r (one at a time, BELOW_NORMAL)."""
import os, subprocess, time
from pathlib import Path
V2 = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
ENV = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")
EDGES = ["assets/HK/edges/drawdownhazardcontinuation_short", "assets/GBPJPY/edges/dailyrangecompletionfade_long",
         "assets/JPN/edges/rangestructure_long", "assets/JPN/edges/volatilitycarry_long",
         "assets/XAUUSD/edges/rangestructure_long", "assets/XAUUSD/edges/volatilitycarry_long"]
for e in EDGES:
    name = e.split("/")[1] + "_" + e.split("/")[-1]
    while not (OUT / "t3" / f"{name}.json").exists():
        time.sleep(30)
    print(time.strftime("%H:%M:%S"), "START t3r", name, flush=True)
    with open(OUT / "job_logs" / f"t3r_{name}.log", "w", encoding="utf-8") as f:
        rc = subprocess.call([PY, "shared/research/mlam_ch6_tests.py", "t3r", e, str(OUT)], cwd=V2, env=ENV, stdout=f,
                             stderr=subprocess.STDOUT, creationflags=0x4000)
    print(time.strftime("%H:%M:%S"), "END t3r", name, "rc", rc, flush=True)
print("T3R DONE", flush=True)
