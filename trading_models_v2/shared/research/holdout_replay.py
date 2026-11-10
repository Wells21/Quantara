"""Gate E -- holdout (embargo) replay of frozen edges.

AFML Ch.1 s.1.3.1.6 (p.8): "Initially, the strategy is run on data observed after
the end date of the backtest ... If embargoed performance is consistent with
backtest results, the strategy is promoted to the next stage."

Method: an isolated WORKSPACE (a copy of trading_models_v2 outside the git repo)
whose assets/{A}/raw/ticks holds the research ticks plus the sealed holdout ticks
(hard links, no extra disk). Inside it the SAME research code rebuilds bars, FFD,
HMM and each edge's research dataset; nothing is reimplemented and the real repo
never reads a holdout tick. Then:
  verify  -- workspace rows before the holdout start must equal the real repo's
             (proves the replay reproduces research exactly)
  score   -- holdout candidate events scored by the FROZEN model + calibrator,
             approved at the live threshold from live_manifest.json
  execute -- approved holdout bets resolved by Task 2A exact execution on ticks
             with the active broker cost model and the live entry delay
  judge   -- portfolio_live/decommission_rule.py on the holdout bets (Gate E rule)

Gate E rule (owner decision 2026-10-23, D023): the holdout must be CONSISTENT with
the backtest -- its mean R is compared with the backtest's own outcomes over
periods of the same length (see GATE_E_RULE). PASS if >= 5% of those periods were
as bad or worse; FAIL otherwise; INCONCLUSIVE below 30 bets (may go live, under
the shrink rule). Every holdout look is a trial (one look per frozen edge version).

Run from trading_models_v2 (venv312):
    python ./shared/research/holdout_replay.py setup  --assets XAUUSD JPN GBPJPY HK
    python ./shared/research/holdout_replay.py build  --assets XAUUSD
    python ./shared/research/holdout_replay.py verify --edges XAUUSD/rangestructure_long
    python ./shared/research/holdout_replay.py run    --edges XAUUSD/rangestructure_long
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

V2_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = V2_ROOT.parent
DEFAULT_WORKSPACE = REPO_ROOT.parent / "Quantara_holdout_workspace" / "trading_models_v2"
GATE_E_DIR = V2_ROOT / "portfolio_live" / "outputs" / "gate_e_holdout"

# Copied into the workspace: everything except tick data, outputs, logs and caches.
COPY_EXCLUDE_DIRS = {
    ".git", "__pycache__", "raw", "holdout", "outputs", "logs", "_gate_logs", "_lifecycle_logs",
    "support_bundles", "live_bars", "live_state", "live_logs", "migration_archives", "v3_horizon_lab",
    "alpha_independence_lab", "label_experiment_2026-10-17",
}
# Allocator outputs the replay reads (benchmark ledger is read from the REAL repo instead).
TOP_LEVEL_DIRS = ("shared", "config", "assets", "portfolio_live", "portfolio_allocator", "docs", "tests")


class HoldoutReplayError(RuntimeError):
    pass


def _copytree(src: Path, dst: Path) -> None:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in COPY_EXCLUDE_DIRS]
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            source, target = Path(root) / name, dst / rel / name
            # refresh changed code/config; never copies tick data (excluded dirs)
            if not target.exists() or source.stat().st_mtime > target.stat().st_mtime:
                shutil.copy2(source, target)


def _link_ticks(asset: str, workspace: Path) -> dict[str, Any]:
    """Hard-link raw + holdout tick CSVs into workspace assets/{A}/raw/ticks."""
    raw_dir = V2_ROOT / "assets" / asset / "raw" / "ticks"
    hold_dir = V2_ROOT / "assets" / asset / "holdout" / "ticks"
    manifest = V2_ROOT / "assets" / asset / "holdout" / "holdout_manifest.json"
    if not manifest.exists():
        raise HoldoutReplayError(f"{asset}: no holdout manifest (download + parity first)")
    out = workspace / "assets" / asset / "raw" / "ticks"
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for src in sorted(raw_dir.glob("*.csv")) + sorted(hold_dir.glob("*.csv")):
        dst = out / src.name
        if not dst.exists():
            os.link(src, dst)
            n += 1
    names = sorted(p.name for p in out.glob("*.csv"))
    hold_names = sorted(p.name for p in hold_dir.glob("*.csv"))
    if names[-len(hold_names):] != hold_names:
        raise HoldoutReplayError(f"{asset}: holdout files do not sort after raw files")
    return {"asset": asset, "linked_now": n, "files": len(names), "holdout_files": len(hold_names),
            "holdout_starts_after_raw_tick_utc": json.loads(manifest.read_text(encoding="utf-8"))["starts_after_raw_tick_utc"]}


def setup(assets: list[str], workspace: Path) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    for name in TOP_LEVEL_DIRS:
        _copytree(V2_ROOT / name, workspace / name)
    for f in V2_ROOT.glob("*.py"):
        shutil.copy2(f, workspace / f.name)
    links = [_link_ticks(a, workspace) for a in assets]
    record = {"created_utc": datetime.now(timezone.utc).isoformat(), "source_repo": str(V2_ROOT),
              "workspace": str(workspace), "assets": links}
    (workspace / "HOLDOUT_WORKSPACE.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


BELOW_NORMAL_PRIORITY_CLASS = 0x00004000  # research never outranks the live bot


def _run(cmd: list[str], cwd: Path, log: Path) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n[{datetime.now(timezone.utc).isoformat()}] $ {' '.join(cmd)}\n")
        fh.flush()
        flags = BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0
        env = dict(os.environ, PYTHONIOENCODING="utf-8")  # scripts print emoji; cp1252 log files cannot encode them
        proc = subprocess.run(cmd, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT, creationflags=flags, env=env)
    if proc.returncode != 0:
        raise HoldoutReplayError(f"failed ({proc.returncode}): {' '.join(cmd)} -- see {log}")


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _first(d: Any, key: str) -> Any:
    """First value of `key` anywhere in a nested manifest."""
    if isinstance(d, dict):
        if key in d:
            return d[key]
        for v in d.values():
            found = _first(v, key)
            if found is not None:
                return found
    elif isinstance(d, list):
        for v in d:
            found = _first(v, key)
            if found is not None:
                return found
    return None


def build_asset(asset: str, workspace: Path) -> dict[str, Any]:
    """Rebuild bars, FFD and HMM in the workspace with the REAL repo's recorded build parameters."""
    bars_dir = V2_ROOT / "assets" / asset / "bars"
    bar_m = _json(bars_dir / "standard_quote_tick" / "bar_manifest.json")
    ffd_m = _json(bars_dir / "ffd" / "ffd_manifest.json")
    hmm_m = _json(bars_dir / "regime" / "hmm_regime_manifest.json")
    py = sys.executable
    log = workspace / "holdout_logs" / f"build_{asset}.log"
    steps = [
        # curated tick layer over research + holdout ticks (quality filter, roll adjustment; AFML Ch.2, D026)
        [py, "shared/data/tick_curation.py", "curate", "--assets", asset],
        [py, "shared/bars/forge_asset_standard_quote_tick_bars.py", "--asset", asset, "--overwrite",
         "--engine", "streaming",
         "--target-bars-per-day", str(_first(bar_m, "target_bars_per_day")),
         "--min-warmup-days", str(_first(bar_m, "min_warmup_days")),
         "--daily-tick-ewma-span-days", str(_first(bar_m, "daily_tick_ewma_span_days"))],
        [py, "shared/ffd/build_asset_ffd_features.py", "--asset", asset, "--overwrite",
         # AFML Ch.5 walkthrough: the workspace holds holdout bars, so it takes the repo's frozen d, never re-estimates
         "--frozen-d", repr(float(_first(ffd_m, "frozen_d"))),
         "--ffd-window", str(_first(ffd_m, "ffd_window")),
         "--min-corr-threshold", str(_first(ffd_m, "min_corr_threshold"))]
        + (["--live-buffer-margin", str(_first(ffd_m, "live_buffer_margin"))] if _first(ffd_m, "live_buffer_margin") is not None else []),
        [py, "shared/regime/build_asset_hmm_regime_features.py", "--asset", asset, "--overwrite"]
        + [arg for key, flag in (("n_states", "--n-states"), ("train_window", "--train-window"),
                                 ("refit_frequency", "--refit-frequency"), ("inference_lookback", "--inference-lookback"),
                                 ("max_iter", "--max-iter"), ("min_covar", "--min-covar"), ("random_state", "--random-state"))
           if _first(hmm_m, key) is not None for arg in (flag, str(_first(hmm_m, key)))],
    ]
    # Per-minute volatility cache behind the broker TABLE spread model, rebuilt over research + holdout ticks.
    steps.append([py, "-c", (
        "import sys; from pathlib import Path; sys.path.insert(0, 'portfolio_allocator'); "
        "from broker_cost_model import vol_cache; "
        f"print(vol_cache.build(Path('.').resolve(), '{asset}', Path('portfolio_allocator/outputs/dukascopy_minute_vol')))"
    )])
    for cmd in steps:
        _run(cmd, workspace, log)
    ws_ffd = _json(workspace / "assets" / asset / "bars" / "ffd" / "ffd_manifest.json")
    if float(_first(ws_ffd, "frozen_d")) != float(_first(ffd_m, "frozen_d")):
        raise HoldoutReplayError(f"{asset}: workspace frozen_d {_first(ws_ffd, 'frozen_d')} != repo {_first(ffd_m, 'frozen_d')}")
    return {"asset": asset, "log": str(log), "frozen_d": _first(ffd_m, "frozen_d")}


def _holdout_start(asset: str):
    import pandas as pd

    m = _json(V2_ROOT / "assets" / asset / "holdout" / "holdout_manifest.json")
    return pd.Timestamp(m["starts_after_raw_tick_utc"]).tz_convert("UTC")


def _live_threshold(edge_id: str) -> float:
    manifest = _json(V2_ROOT / "portfolio_live" / "live_manifest.json")
    for entry in manifest.get("selected_edges") or []:
        if entry.get("edge_id") == edge_id:
            return float(entry["final_live_threshold"])
    raise HoldoutReplayError(f"{edge_id}: not in live_manifest.json selected_edges (no live threshold)")


def _in_workspace(workspace: Path, edge_id: str, step: str, **kw: Any) -> None:
    """Run `step` for `edge_id` with the WORKSPACE copy of this tool, so edge code resolves workspace data."""
    shutil.copy2(Path(__file__), workspace / "shared" / "research" / Path(__file__).name)
    asset, name = edge_id.split("/")
    cmd = [sys.executable, "shared/research/holdout_replay.py", "_ws", "--edge", edge_id, "--step", step]
    for key, value in kw.items():
        cmd += [f"--{key.replace('_', '-')}", str(value)]
    _run(cmd, workspace, workspace / "holdout_logs" / f"{asset}__{name}.log")


# ------------------------------------------------------------- workspace side
def _ws_export(edge_id: str) -> None:
    asset, name = edge_id.split("/")
    _run([sys.executable, f"assets/{asset}/edges/{name}/export_research_dataset.py", "--overwrite"],
         V2_ROOT, V2_ROOT / "holdout_logs" / f"{asset}__{name}__export.log")


def _ws_replay(edge_id: str, holdout_start: str, live_threshold: float, out_csv: Path) -> None:
    """Score holdout events with the frozen model; resolve approved bets with Task 2A + broker costs."""
    import pandas as pd

    sys.path.insert(0, str(V2_ROOT))
    from shared.edges.edge_layout import get_edge_layout_from_edge_root
    from shared.edges.trade_geometry import load_trade_geometry
    from shared.execution import executable_target_dataset as etd
    from shared.execution import oof_exact_execution as exact

    asset, name = edge_id.split("/")
    start = pd.Timestamp(holdout_start)
    layout = get_edge_layout_from_edge_root(V2_ROOT / "assets" / asset / "edges" / name)
    geometry = load_trade_geometry(layout.edge_yaml, expected_edge_id=layout.edge_id, expected_side_name=layout.side_name)
    runtime, model_rows_module = exact._import_edge_modules(layout.edge_root)

    features = runtime.build_features(drop_warmup_rows=False)
    events = runtime.generate_candidate_events(features)
    scored, _ = runtime.score(features=features, events=events)
    scored["Datetime"] = pd.to_datetime(scored["Datetime"], utc=True)
    hold = scored.loc[scored["Datetime"] > start].copy()
    # live rule: portfolio_live_dashboard_orchestrator -> calibrated_probability >= final_live_threshold
    hold["approved_live_threshold"] = pd.to_numeric(hold["calibrated_probability"], errors="coerce") >= float(live_threshold)
    keep = [c for c in ("Datetime", "side", "Event_Vol", "raw_probability", "calibrated_probability",
                        "afml_bet_size", "approved_live_threshold") if c in hold.columns]
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    hold[keep].to_csv(out_csv.with_name(out_csv.stem.replace("_bets", "_scored_events") + ".csv"), index=False)

    _, gross, _ = etd.load_gross_model_rows(layout.edge_root, str(geometry["geometry_hash"]))
    approved_times = set(hold.loc[hold["approved_live_threshold"], "Datetime"])
    g = gross.loc[gross[exact.TIME_COL].isin(approved_times)].copy()

    firm, delay_ms, cm, _ = etd.research_cost_setup(layout.asset)
    max_age = exact._effective_live_max_signal_age_seconds(layout.v2_root)
    bars_dt, bars_close = exact._load_bars(runtime)
    catalog = exact._build_tick_catalog(model_rows_module)
    bars_dt_ns = bars_dt.as_unit("ns").asi8 if bool(geometry.get("calendar_time")) else None
    kw = dict(bars_dt=bars_dt, bars_close=bars_close, catalog=catalog, geometry=geometry,
              max_signal_age_seconds=max_age, bars_dt_ns=bars_dt_ns)
    if g.empty:
        ex = pd.DataFrame(columns=[exact.TIME_COL, "execution_status", "executed_r"])
    else:
        ex = etd._apply_carry(etd._replay(g, cm, delay_ms, label="holdout", **kw), cm)
    ex["edge_id"] = edge_id
    ex["cost_firm"] = firm
    ex["entry_delay_ms"] = delay_ms
    prob = hold.set_index("Datetime")[["calibrated_probability", "raw_probability", "afml_bet_size"]]
    ex = ex.merge(prob, left_on=exact.TIME_COL, right_index=True, how="left")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    ex.to_csv(out_csv, index=False)
    summary = {
        "edge_id": edge_id, "holdout_start_utc": str(start), "live_threshold": float(live_threshold),
        "holdout_candidate_events": int(len(hold)),
        "approved_at_live_threshold": int(hold["approved_live_threshold"].sum()),
        "gross_rows_matched": int(len(g)),
        "resolved": int(ex["execution_status"].astype(str).eq("resolved").sum()) if len(ex) else 0,
        "first_holdout_event_utc": str(hold["Datetime"].min()) if len(hold) else None,
        "last_holdout_event_utc": str(hold["Datetime"].max()) if len(hold) else None,
    }
    out_csv.with_suffix(".json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)


# ------------------------------------------------------------------ repo side
def verify_edge(edge_id: str, workspace: Path) -> dict[str, Any]:
    """Workspace rows up to the repo's research end must equal the real repo's research rows."""
    import numpy as np
    import pandas as pd

    asset, name = edge_id.split("/")
    start = _holdout_start(asset)
    rel = Path("assets") / asset / "edges" / name / "artifacts" / "research_dataset"
    stem = f"{asset.lower()}_{name}"
    report: dict[str, Any] = {"edge_id": edge_id, "holdout_start_utc": str(start)}
    for kind in ("candidate_events", "model_rows"):
        a = pd.read_csv(V2_ROOT / rel / f"{stem}_{kind}.csv", low_memory=False)
        b = pd.read_csv(workspace / rel / f"{stem}_{kind}.csv", low_memory=False)
        for df in (a, b):
            df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True, errors="coerce")
        repo_end = a["Datetime"].max()
        b_pre = b.loc[b["Datetime"] <= repo_end].reset_index(drop=True)
        a = a.reset_index(drop=True)
        same_times = len(a) == len(b_pre) and bool((a["Datetime"].values == b_pre["Datetime"].values).all())
        numeric = [c for c in a.columns if c in b_pre.columns
                   and pd.api.types.is_numeric_dtype(a[c]) and pd.api.types.is_numeric_dtype(b_pre[c])]
        max_diff = None
        if same_times and numeric:
            diffs = (a[numeric].astype("float64") - b_pre[numeric].astype("float64")).abs().to_numpy()
            max_diff = float(np.nanmax(diffs)) if np.isfinite(diffs).any() else 0.0
        report[kind] = {"repo_rows": int(len(a)), "workspace_rows_same_period": int(len(b_pre)),
                        "workspace_rows_after_holdout_start": int((b["Datetime"] > start).sum()),
                        "same_timestamps": same_times, "max_abs_numeric_diff": max_diff}
    ok = all(report[k]["same_timestamps"] and (report[k]["max_abs_numeric_diff"] or 0.0) < 1e-6
             for k in ("candidate_events", "model_rows"))
    report["verdict"] = "IDENTICAL" if ok else "MISMATCH"
    return report


# AFML Ch.1 p.8: promote "if embargoed performance is consistent with backtest results". The backtest is one
# path among many (p.8; s.11.6 p.155) and CPCV judges a strategy by the distribution of its outcomes across paths
# (s.12.4 p.166). So the holdout is compared with the distribution of the backtest's own outcomes over periods
# of the SAME length, not with the multi-year average (owner decision 2026-10-23, D023).
GATE_E_MIN_BETS = 30
GATE_E_MIN_WINDOW_BETS = 20
GATE_E_WINDOW_STEP_DAYS = 7
GATE_E_CONSISTENCY_LEVEL = 0.05
GATE_E_RULE = (
    "Consistency with the backtest: windows of the holdout's span, stepped every 7 days through the backtest "
    "(meta-approved bets at the live threshold, broker costs, >= 20 bets per window); share_as_bad = fraction of "
    "windows whose mean R <= holdout mean R. PASS if >= 30 holdout bets and share_as_bad >= 0.05; FAIL if "
    "share_as_bad < 0.05; INCONCLUSIVE below 30 bets. PSR[0] and PSR[backtest] are reported as diagnostics."
)


def consistency_with_backtest(holdout: "pd.DataFrame", backtest: "pd.DataFrame") -> dict[str, Any]:
    """Where the holdout's mean R falls among same-length windows of the backtest.

    Both frames need columns t (entry time, UTC) and r (net R per bet).
    """
    import numpy as np
    import pandas as pd

    span = holdout["t"].max() - holdout["t"].min()
    mean_r = float(holdout["r"].mean())
    bt = backtest.sort_values("t")
    means = []
    for start in pd.date_range(bt["t"].min(), bt["t"].max() - span, freq=f"{GATE_E_WINDOW_STEP_DAYS}D"):
        window = bt.loc[(bt["t"] >= start) & (bt["t"] < start + span), "r"]
        if len(window) >= GATE_E_MIN_WINDOW_BETS:
            means.append(float(window.mean()))
    means = np.asarray(means)
    if means.size == 0:
        return {"holdout_span_days": span.days, "holdout_mean_r": mean_r, "n_windows": 0, "share_as_bad": None}
    return {
        "holdout_span_days": int(span.days), "holdout_mean_r": mean_r, "n_windows": int(means.size),
        "share_as_bad": float((means <= mean_r).mean()),
        "window_mean_r_p5": float(np.percentile(means, 5)), "window_mean_r_median": float(np.median(means)),
        "window_mean_r_min": float(means.min()), "share_windows_negative": float((means < 0).mean()),
        "backtest_mean_r": float(bt["r"].mean()),
    }


def judge(edge_ids: list[str], workspace: Path) -> list[dict[str, Any]]:
    """Gate E consistency test; PSR from the decommission rule is reported alongside as a diagnostic."""
    import pandas as pd

    sys.path.insert(0, str(V2_ROOT / "portfolio_live"))
    import decommission_rule

    cfg = _json(decommission_rule.CONFIG_PATH)
    spec = cfg["backtest_benchmark"]
    ledger = pd.read_csv(V2_ROOT / spec["ledger"], usecols=["edge_id", "entry_time", spec["return_column"]], low_memory=False)
    ledger = ledger.rename(columns={spec["return_column"]: "r"})
    ledger["t"] = pd.to_datetime(ledger["entry_time"], utc=True, format="ISO8601")
    frames = {}
    for eid in edge_ids:
        asset, name = eid.split("/")
        df = pd.read_csv(workspace / "holdout_outputs" / f"{asset}__{name}__holdout_bets.csv")
        df = df.loc[df["execution_status"].astype(str).eq("resolved")]
        frames[eid] = pd.DataFrame({"edge_id": eid, "r": pd.to_numeric(df["executed_r"], errors="coerce"),
                                    "t": pd.to_datetime(df.get("entry_time"), utc=True, format="ISO8601")}).dropna()
    psr = {v.edge_id: v for v in decommission_rule.run(cfg, pd.concat(frames.values(), ignore_index=True), "holdout")}
    out = []
    for eid in edge_ids:
        hold = frames[eid]
        record: dict[str, Any] = {"edge_id": eid, "n_bets": int(len(hold))}
        if len(hold) < GATE_E_MIN_BETS:
            record["gate_e"] = "INCONCLUSIVE"
        else:
            record.update(consistency_with_backtest(hold, ledger.loc[ledger["edge_id"] == eid, ["t", "r"]]))
            share = record.get("share_as_bad")
            record["gate_e"] = "FAIL" if share is None or share < GATE_E_CONSISTENCY_LEVEL else "PASS"
        v = psr.get(eid)
        record["diagnostics"] = None if v is None else {
            "mean_r": v.mean_r, "psr_vs_zero": v.psr_vs_zero, "psr_vs_backtest": v.psr_vs_backtest,
            "backtest_sr_per_bet": v.backtest_sr_per_bet}
        out.append(record)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["setup", "build", "export", "verify", "replay", "judge", "_ws"])
    parser.add_argument("--assets", nargs="+", default=[])
    parser.add_argument("--edges", nargs="+", default=[])
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--edge")
    parser.add_argument("--step")
    parser.add_argument("--holdout-start")
    parser.add_argument("--live-threshold", type=float)
    parser.add_argument("--out-csv", type=Path)
    args = parser.parse_args()
    if args.command == "setup":
        print(json.dumps(setup(args.assets, args.workspace), indent=2))
    elif args.command == "build":
        for asset in args.assets:
            print(json.dumps(build_asset(asset, args.workspace)), flush=True)
    elif args.command == "export":
        for eid in args.edges:
            _in_workspace(args.workspace, eid, "export")
            print(f"{eid}: exported in workspace", flush=True)
    elif args.command == "verify":
        for eid in args.edges:
            print(json.dumps(verify_edge(eid, args.workspace)), flush=True)
    elif args.command == "replay":
        for eid in args.edges:
            asset, name = eid.split("/")
            out_csv = args.workspace / "holdout_outputs" / f"{asset}__{name}__holdout_bets.csv"
            _in_workspace(args.workspace, eid, "replay", holdout_start=_holdout_start(asset).isoformat(),
                          live_threshold=_live_threshold(eid), out_csv=out_csv)
            print(out_csv.with_suffix(".json").read_text(encoding="utf-8"), flush=True)
    elif args.command == "judge":
        result = judge(args.edges, args.workspace)
        GATE_E_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = GATE_E_DIR / f"gate_e_holdout_{stamp}.json"
        path.write_text(json.dumps({"generated_utc": stamp, "rule": GATE_E_RULE, "results": result}, indent=2),
                        encoding="utf-8")
        print(json.dumps(result, indent=2))
        print(f"report: {path}")
    elif args.command == "_ws":  # runs INSIDE the workspace
        if args.step == "export":
            _ws_export(args.edge)
        elif args.step == "replay":
            _ws_replay(args.edge, args.holdout_start, args.live_threshold, args.out_csv)


if __name__ == "__main__":
    main()
