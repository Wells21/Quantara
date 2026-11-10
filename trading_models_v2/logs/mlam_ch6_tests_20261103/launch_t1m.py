"""POST HOC T1m: after the main launcher prints ALL DONE, run T1 sigma 0.1 with mwfl 0 (BELOW_NORMAL)."""
import os, subprocess, time
from pathlib import Path
V2 = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
ENV = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8", CH6_TREE_MWFL="0")
while "ALL DONE" not in (OUT / "launcher.log").read_text(encoding="utf-8", errors="replace"):
    time.sleep(30)
print(time.strftime("%H:%M:%S"), "START t1m", flush=True)
with open(OUT / "job_logs" / "t1m_s0.1_mwfl0.log", "w", encoding="utf-8") as f:
    rc = subprocess.call([PY, "shared/research/mlam_ch6_tests.py", "t1", "0.1", str(OUT)], cwd=V2, env=ENV, stdout=f,
                         stderr=subprocess.STDOUT, creationflags=0x4000)
print(time.strftime("%H:%M:%S"), "END t1m rc", rc, flush=True)
