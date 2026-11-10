"""Collect every Gate A hybrid-fast-grid result that used the runbook's 405-cell grid (owner question 2026-10-31).

Sources: v3_hybrid_fast_grid_*.json deleted from the working tree (recovered from the parent of the deleting commit) and
the *.csv grids still on disk. Every grid is kept (one row per cell); runbook_grid marks the runbook's exact grid:
lookbacks 1,2,4,6,8 h x EWM 20,50,100 x verticals 1,2,4,8,16,24,48,96,168 h x pt:sl 1:1, 1.5:1, 2:1 (405 cells,
calendar time). Older grids (bar-count lookbacks, other spans) still cross the dimensions and are analysed per grid.

usage (from trading_models_v2):  python shared/research/grid_dimension_sensitivity/collect_gate_a_grids.py OUT.parquet|csv
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

V2 = Path(__file__).resolve().parents[3]
REPO = V2.parent
LOOKBACKS = {1.0, 2.0, 4.0, 6.0, 8.0}
SPANS = {20, 50, 100}
VERTICALS = {1.0, 2.0, 4.0, 8.0, 16.0, 24.0, 48.0, 96.0, 168.0}
RATIOS = {1.0, 1.5, 2.0}
COLS = ["edge_id", "event_vol_lookback", "event_vol_ewm_span", "vertical_horizon", "pt", "sl", "rows", "gross_mean_R",
        "net_mean_R", "cost_bps"]
OPTIONAL = ["pt_rate", "sl_rate", "vertical_rate", "calendar_time"]


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-c", "diff.renameLimit=5000", *args], cwd=REPO, capture_output=True, check=True).stdout


def is_runbook_grid(df: pd.DataFrame) -> bool:
    if len(df) != 405:
        return False
    cells = set(zip(df.event_vol_lookback.astype(float), df.event_vol_ewm_span.astype(int),
                    df.vertical_horizon.astype(float), (df.pt / df.sl).round(3)))
    want = {(lb, s, v, r) for lb in LOOKBACKS for s in SPANS for v in VERTICALS for r in RATIOS}
    return cells == want


def keep(df: pd.DataFrame) -> pd.DataFrame:
    out = df[COLS + [c for c in OPTIONAL if c in df.columns]].copy()
    for c in OPTIONAL:
        if c not in out.columns:
            out[c] = pd.NA
    return out


def main(out: Path) -> None:
    frames, seen, skipped = [], set(), 0
    log = git("log", "--all", "--diff-filter=D", "--name-only", "--format=COMMIT %H").decode()
    commit = None
    for line in log.splitlines():
        if line.startswith("COMMIT "):
            commit = line.split()[1]
            continue
        if not (line.endswith(".json") and "/v3_hybrid_fast_grid_" in line):
            continue
        stamp = Path(line).stem.rsplit("_", 1)[-1]
        try:
            doc = json.loads(git("show", f"{commit}^:{line}"))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            skipped += 1
            continue
        df = pd.DataFrame(doc.get("rows") or [])
        if df.empty or not set(COLS) <= set(df.columns):
            skipped += 1
            continue
        key = (df.edge_id.iloc[0], stamp)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        frames.append(keep(df).assign(stamp=stamp, source="git_deleted", runbook_grid=is_runbook_grid(df)))
    for path in sorted(V2.glob("assets/*/edges/*/artifacts/v3_horizon_lab/v3_hybrid_fast_grid_*.csv")):
        df = pd.read_csv(path)
        stamp = path.stem.rsplit("_", 1)[-1]
        key = (df.edge_id.iloc[0], stamp)
        if key in seen or not set(COLS) <= set(df.columns):
            skipped += 1
            continue
        seen.add(key)
        frames.append(keep(df).assign(stamp=stamp, source="on_disk", runbook_grid=is_runbook_grid(df)))
    allg = pd.concat(frames, ignore_index=True)
    allg.to_csv(out, index=False)
    rb = allg[allg.runbook_grid]
    print(f"grid runs kept: {len(frames)} on {allg.edge_id.nunique()} thesis sides; runbook 405-cell runs: "
          f"{rb.groupby(['edge_id', 'stamp']).ngroups} on {rb.edge_id.nunique()} sides; duplicates / unreadable: {skipped}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
