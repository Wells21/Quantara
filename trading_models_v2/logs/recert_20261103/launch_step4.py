"""RECERT step 4: label audit (all 4 edges, one call) then CV leakage audits (4 in parallel), BELOW_NORMAL."""
import os, subprocess, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
V2 = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "step4"
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
ENV = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")
EDGES = ["GBPJPY/dailyrangecompletionfade_long", "HK/drawdownhazardcontinuation_short", "JPN/rangestructure_long", "XAUUSD/rangestructure_long"]
def log(m): print(time.strftime("%H:%M:%S"), m, flush=True)
def run(tag, args):
    log(f"START {tag}")
    with open(OUT / f"{tag}.log", "w", encoding="utf-8") as f:
        rc = subprocess.call([PY] + args, cwd=V2, env=ENV, stdout=f, stderr=subprocess.STDOUT, creationflags=0x4000)
    log(f"END {tag} rc {rc}")
run("label_audit", ["shared/research/label_audit.py", "--edges"] + EDGES)
with ThreadPoolExecutor(4) as ex:
    list(ex.map(lambda e: run("cv_leakage_" + e.replace("/", "_"), ["shared/research/cv_leakage_audit.py", "--edges", e]), EDGES))
log("ALL DONE")
