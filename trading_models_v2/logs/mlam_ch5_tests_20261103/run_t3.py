"""MLAM Ch.5 T3 chain: Gate A (both XAUUSD edges concurrently) then Gate C (--all-cells, serially), seasonal barriers.
BELOW_NORMAL priority, 1 BLAS thread per process. Gate C goes through gate_c_runner with LAB = the seasonal wrapper."""
import json, os, re, subprocess, sys, time
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PY = str(V2.parent / "venv312" / "Scripts" / "python.exe")
WRAP = "shared/research/mlam_ch5_seasonal_lab.py"
EDGES = ["assets/XAUUSD/edges/rangestructure_long", "assets/XAUUSD/edges/volatilitycarry_long"]
COST_MODEL = "portfolio_allocator/config/broker_profiles/moneta_cost_models/XAUUSD.json"
GRID = ["--event-vol-lookbacks", "1,2,4,6,8", "--event-vol-ewm-spans", "100,150,200",
        "--vertical-horizons", "1,2,4,8,16,24,48,96,168", "--barriers", "1:1,1.5:1,2:1",
        "--cost-bps", "0.2583", "--hybrid-fast-grid", "--save"]
ENV = dict(os.environ, OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONIOENCODING="utf-8")


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), msg, flush=True)


procs = {}
for e in EDGES:
    name = e.split("/")[-1]
    f = open(OUT / f"gateA_{name}.out.log", "w", encoding="utf-8")
    procs[e] = (subprocess.Popen([PY, WRAP, "--edge-root", e] + GRID, cwd=V2, env=ENV, stdout=f, stderr=subprocess.STDOUT,
                                 creationflags=0x4000), f)
    log(f"START gateA {name}")
stamps = {}
for e, (p, f) in procs.items():
    rc = p.wait(); f.close()
    name = e.split("/")[-1]
    txt = (OUT / f"gateA_{name}.out.log").read_text(encoding="utf-8", errors="replace")
    m = re.findall(r"v3_hybrid_fast_grid_(\d{8}T\d{6}Z)\.csv", txt)
    log(f"END gateA {name} rc {rc} stamp {m[-1] if m else None}")
    if rc != 0 or not m:
        sys.exit(f"Gate A failed for {name}")
    stamps[e] = m[-1]
(OUT / "t3_gateA_stamps.json").write_text(json.dumps(stamps, indent=1))

runner_shim = ("import sys; from pathlib import Path; sys.path.insert(0, r'%s'); "
               "import shared.research.gate_c_runner as g; g.LAB = Path(r'%s'); sys.exit(g.main(sys.argv[1:]))"
               % (V2, V2 / WRAP))
for e in EDGES:
    name = e.split("/")[-1]
    log(f"START gateC {name}")
    with open(OUT / f"gateC_{name}.out.log", "w", encoding="utf-8") as f:
        rc = subprocess.call([PY, "-c", runner_shim, "--edge-root", e, "--grid-stamp", stamps[e], "--cost-model", COST_MODEL,
                              "--out-jsonl", str(OUT / f"t3_{name}_gatec.jsonl"), "--all-cells", "--batch-size", "135",
                              "--python", PY], cwd=V2, env=ENV, stdout=f, stderr=subprocess.STDOUT, creationflags=0x4000)
    log(f"END gateC {name} rc {rc}")
log("ALL DONE")
