"""MLAM Ch.7 launcher: 6 lanes, BELOW_NORMAL, 1 BLAS thread per process. Lanes 1-4: book DGP chunks (25 sims each);
lane 5: rnd DGP (Ex.2); lane 6: book100 then book10 (Ex.4) then T3 (our inputs, venv39)."""
import os, subprocess, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PY312 = str(V2.parent / "venv312" / "Scripts" / "python.exe")
PY39 = str(V2.parent / "venv39" / "Scripts" / "python.exe")
DRV = "shared/research/mlam_ch7_tests.py"
ENV = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")


def log(m):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), m, flush=True)


def run(tag, cmd):
    (OUT / "job_logs").mkdir(exist_ok=True)
    with open(OUT / "job_logs" / f"{tag}.log", "w", encoding="utf-8") as f:
        log(f"START {tag}")
        rc = subprocess.call(cmd, cwd=V2, env=ENV, stdout=f, stderr=subprocess.STDOUT, creationflags=0x4000)
        log(f"END {tag} rc {rc}")
    return rc


def tail_lane():
    return [run("ex4_book100", [PY312, DRV, "sim", "book100", "0", "1", str(OUT)]),
            run("ex4_book10", [PY312, DRV, "sim", "book10", "0", "1", str(OUT)]),
            run("t3_ours", [PY39, "shared/research/mlam_ch7_ours.py", str(OUT / "t3_ours.json")])]


with ThreadPoolExecutor(6) as ex:
    futs = [ex.submit(run, f"book_{c}of4", [PY312, DRV, "sim", "book", str(c), "4", str(OUT)]) for c in range(4)]
    futs += [ex.submit(run, "ex2_rnd", [PY312, DRV, "sim", "rnd", "0", "1", str(OUT)]), ex.submit(tail_lane)]
    [f.result() for f in futs]
run("merge", [PY312, DRV, "merge", str(OUT)])
log("ALL DONE")
