"""RECERT step 2: re-export the 4 edges (187-entry schema). Different assets -> run in parallel (same-asset exports
would share the HMM continuation cache). BELOW_NORMAL, 1 BLAS thread each."""
import os, subprocess, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
V2 = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "step2"
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
ENV = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")
EDGES = ["GBPJPY/edges/dailyrangecompletionfade_long", "HK/edges/drawdownhazardcontinuation_short",
         "JPN/edges/rangestructure_long", "XAUUSD/edges/rangestructure_long"]
def run(e):
    tag = e.split("/")[0] + "_" + e.split("/")[-1]
    print(time.strftime("%H:%M:%S"), "START", tag, flush=True)
    with open(OUT / f"{tag}.log", "w", encoding="utf-8") as f:
        rc = subprocess.call([PY, f"assets/{e}/export_research_dataset.py", "--overwrite"], cwd=V2, env=ENV,
                             stdout=f, stderr=subprocess.STDOUT, creationflags=0x4000)
    print(time.strftime("%H:%M:%S"), "END", tag, "rc", rc, flush=True)
with ThreadPoolExecutor(4) as ex:
    list(ex.map(run, EDGES))
print("ALL DONE", flush=True)
