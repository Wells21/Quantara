"""RECERT step 1 stress launcher: 4 edges in parallel, BELOW_NORMAL, 1 BLAS thread each."""
import os, subprocess, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
V2 = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "stress"
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
ENV = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")
EDGES = ["GBPJPY/dailyrangecompletionfade_long", "HK/drawdownhazardcontinuation_short", "JPN/rangestructure_long", "XAUUSD/rangestructure_long"]
def run(e):
    print(time.strftime("%H:%M:%S"), "START", e, flush=True)
    rc = subprocess.call([PY, "shared/research/recert_step1_stress.py", e, str(OUT)], cwd=V2, env=ENV, creationflags=0x4000,
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    print(time.strftime("%H:%M:%S"), "END", e, "rc", rc, flush=True)
with ThreadPoolExecutor(4) as ex:
    list(ex.map(run, EDGES))
print("ALL DONE", flush=True)
