"""AFML Ch.10 walkthrough, test step 1 (owner-approved 2026-10-26): how often do trades give back floating profit?

For every resolved research trade of an edge (calibrated OOF file, research period only -- the sealed holdout is NOT
read), the Dukascopy MID tick path is replayed from the trade's entry time with its own take-profit / stop-loss
distances and vertical deadline (the mid-path label contract). For each trade we record the outcome and the maximum
favourable excursion (MFE) as a fraction of the take-profit distance, reached before the exit.

Question: among losing trades, what share first reached 50 / 75 / 90 % of the way to the take-profit?
Two references for "what a random market would do":
  * gambler's ruin (driftless, no vertical barrier): among stop-loss exits the share that first reached k of the TP
    distance is b (1 - k) / (k a + b), a = TP distance, b = SL distance (2:1 -> 25.0 % / 10.0 % / 3.6 %);
  * random entries: for every real trade, N_RANDOM entries at random bar closes in the same weekly tick file, same
    side, same TP / SL distances as a fraction of price and the same vertical duration, resolved on the same ticks.
Consistency check: the recomputed real outcome must match the stored mid label (label_r sign, label_t1).

usage (from trading_models_v2, venv312):
  python -m shared.research.ch10_path_giveback_audit --edge XAUUSD/rangestructure_long --out <json>
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[2]
ENTRY_DELAY_MS = 5700  # measured live bar-close -> fill latency (same delay the labels use)
N_RANDOM = 5
LEVELS = (0.5, 0.75, 0.9)
SEED = 20261026
FILE_RE = re.compile(r"_ticks_(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})\.csv$")


def _ts(s: pd.Series) -> np.ndarray:
    return pd.to_datetime(s, utc=True, format="ISO8601").to_numpy(dtype="datetime64[ns]").astype(np.int64)


def load_ticks(path: Path) -> tuple[np.ndarray, np.ndarray]:
    d = pd.read_csv(path, usecols=["timestamp", "bidPrice", "askPrice"])
    t = _ts(d["timestamp"])
    mid = ((d["bidPrice"].to_numpy(float) + d["askPrice"].to_numpy(float)) / 2.0)
    order = np.argsort(t, kind="stable")
    return t[order], mid[order]


def resolve(t: np.ndarray, mid: np.ndarray, entry_ns: int, side: int, tp: float, sl: float, vert_ns: int):
    """First touch on the mid path from the first tick >= entry_ns. Returns outcome, R, MFE/TP, exit_ns."""
    i0 = np.searchsorted(t, entry_ns, side="left")
    i1 = np.searchsorted(t, vert_ns, side="right")
    if i0 >= len(t) or i1 <= i0:
        return None
    e = mid[i0]
    path = side * (mid[i0:i1] - e)
    hit_pt = np.flatnonzero(path >= tp)
    hit_sl = np.flatnonzero(path <= -sl)
    k_pt = hit_pt[0] if len(hit_pt) else np.inf
    k_sl = hit_sl[0] if len(hit_sl) else np.inf
    if k_pt == np.inf and k_sl == np.inf:
        k, outcome = len(path) - 1, "vertical"
    elif k_pt < k_sl:
        k, outcome = int(k_pt), "pt"
    else:
        k, outcome = int(k_sl), "sl"
    r = float(path[k] / sl)
    mfe = float(max(path[: k + 1].max(), 0.0) / tp)
    return outcome, r, min(mfe, 1.0), int(t[i0 + k]), float(e)


def summarise(df: pd.DataFrame, a_over_b: float) -> dict:
    out = {"n": int(len(df)), "share_pt": float((df.outcome == "pt").mean()), "share_sl": float((df.outcome == "sl").mean()),
           "share_vertical": float((df.outcome == "vertical").mean()), "mean_r": float(df.r.mean())}
    sl = df[df.outcome == "sl"]
    losers = df[df.r <= 0]
    for k in LEVELS:
        key = f"{int(k * 100)}"
        out[f"sl_exits_reached_{key}pct_tp"] = float((sl.mfe >= k).mean()) if len(sl) else None
        out[f"losers_reached_{key}pct_tp"] = float((losers.mfe >= k).mean()) if len(losers) else None
        out[f"all_trades_reached_{key}pct_then_lost"] = float(((df.mfe >= k) & (df.r <= 0)).mean())
        out[f"gamblers_ruin_sl_exits_{key}pct"] = float((1 - k) / (k * a_over_b + 1))
    out["n_sl_exits"] = int(len(sl))
    out["n_losers"] = int(len(losers))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    asset, edge = args.edge.split("/")
    root = V2 / "assets" / asset / "edges" / edge
    cal = next((root / "artifacts" / "calibration").glob("*_calibrated_oof_predictions.csv"))
    ev = pd.read_csv(cal, usecols=["entry_time", "vertical_deadline", "label_r", "label_t1", "side",
                                   "take_profit_distance_price", "stop_distance_price", "live_calibrated_probability",
                                   "execution_status"])
    ev = ev[ev.execution_status.eq("resolved")].reset_index(drop=True)
    manifest = json.loads((V2 / "portfolio_live" / "live_manifest.json").read_text(encoding="utf-8"))
    thr = next(e["final_live_threshold"] for e in manifest["selected_edges"] if e["edge_id"] == args.edge)
    ev["entry_ns"] = _ts(ev.entry_time)
    ev["vert_ns"] = _ts(ev.vertical_deadline)
    ev["label_t1_ns"] = _ts(ev.label_t1)
    bars = pd.read_csv(V2 / "assets" / asset / "bars" / "standard_quote_tick" / f"{asset.lower()}_standard_quote_tick_bars.csv",
                       usecols=["Datetime"])
    bar_ns = _ts(bars.Datetime)

    files = sorted((V2 / "assets" / asset / "curated" / "ticks").glob("*_ticks_*.csv"))
    spans = []
    for f in files:
        m = FILE_RE.search(f.name)
        spans.append((pd.Timestamp(m.group(1), tz="UTC").value, pd.Timestamp(m.group(2), tz="UTC").value))
    starts = np.array([s for s, _ in spans])
    ev["file_idx"] = np.searchsorted(starts, ev.entry_ns.to_numpy(), side="right") - 1
    max_hold = int((ev.vert_ns - ev.entry_ns).max())
    rng = np.random.default_rng(SEED)
    real_rows, rand_rows = [], []
    cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    started = time.time()
    for fi in sorted(ev.file_idx.unique()):
        need = [j for j in range(fi, len(files)) if starts[j] <= spans[fi][1] + max_hold]
        for j in list(cache):
            if j not in need:
                del cache[j]
        for j in need:
            if j not in cache:
                cache[j] = load_ticks(files[j])
        t = np.concatenate([cache[j][0] for j in need])
        mid = np.concatenate([cache[j][1] for j in need])
        week_bars = bar_ns[(bar_ns >= spans[fi][0]) & (bar_ns < spans[fi][1])]
        for _, r in ev[ev.file_idx == fi].iterrows():
            res = resolve(t, mid, int(r.entry_ns), int(r.side), float(r.take_profit_distance_price),
                          float(r.stop_distance_price), int(r.vert_ns))
            if res is None:
                continue
            outcome, rr, mfe, exit_ns, e = res
            real_rows.append({"outcome": outcome, "r": rr, "mfe": mfe, "approved": bool(r.live_calibrated_probability >= thr),
                              "label_match": bool((rr > 0) == (r.label_r > 0)),
                              "t1_diff_s": abs(exit_ns - int(r.label_t1_ns)) / 1e9})
            tp_frac, sl_frac = r.take_profit_distance_price / e, r.stop_distance_price / e
            hold = int(r.vert_ns - r.entry_ns)
            if len(week_bars):
                for b in rng.choice(week_bars, size=N_RANDOM, replace=True):
                    en = int(b) + ENTRY_DELAY_MS * 1_000_000
                    i0 = np.searchsorted(t, en)
                    if i0 >= len(t):
                        continue
                    e2 = mid[i0]
                    res2 = resolve(t, mid, en, int(r.side), tp_frac * e2, sl_frac * e2, en + hold)
                    if res2 is not None:
                        rand_rows.append({"outcome": res2[0], "r": res2[1], "mfe": res2[2]})
        print(f"[{args.edge}] file {fi + 1}/{len(files)} trades {len(real_rows)} elapsed {time.time() - started:.0f}s", flush=True)
    real = pd.DataFrame(real_rows)
    rand = pd.DataFrame(rand_rows)
    a_over_b = float((ev.take_profit_distance_price / ev.stop_distance_price).median())
    report = {
        "edge": args.edge, "test": "AFML Ch.10 walkthrough step 1: floating-profit giveback on mid tick paths (research period only)",
        "live_threshold": thr, "tp_over_sl": a_over_b, "entry_delay_ms": ENTRY_DELAY_MS, "n_random_per_trade": N_RANDOM,
        "label_consistency": {"share_outcome_sign_matches_label": float(real.label_match.mean()),
                              "share_exit_within_1s_of_label_t1": float((real.t1_diff_s <= 1).mean())},
        "all_trades": summarise(real, a_over_b),
        "approved_trades": summarise(real[real.approved], a_over_b),
        "random_entries": summarise(rand, a_over_b),
    }
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
