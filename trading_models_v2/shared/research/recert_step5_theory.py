"""RECERT step 5: D075 counterfactuals + raw drift baseline at the locked geometry (logs/recert_20261103/step5/
PREREGISTRATION.md). Reuses run_gate0_panel.py stage1 / stage2 (cell "L") for the event sets and labels.

run OUT        stage1 + stage2 L for the 4 edges (in parallel, BelowNormal)
analyse OUT    D075 statistics (one-sided 1.645; pooled rangestructure over JPN + XAUUSD)

usage (from trading_models_v2, venv312): python shared/research/recert_step5_theory.py run|analyse OUT
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

V2 = Path(__file__).resolve().parents[2]
CF = V2 / "shared" / "research" / "counterfactual_tests"
sys.path.insert(0, str(CF))
import run_counterfactuals as rc  # noqa: E402
import run_drift_baseline as db  # noqa: E402
from run_gate0_panel import joint_diff  # noqa: E402

EDGES = [("GBPJPY", "dailyrangecompletionfade_long"), ("HK", "drawdownhazardcontinuation_short"),
         ("JPN", "rangestructure_long"), ("XAUUSD", "rangestructure_long")]
POOLED = {"rangestructure_long": ["JPN", "XAUUSD"]}
Z = 1.645
SEED = 20261104
PANEL = str(CF / "run_gate0_panel.py")


def run(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)

    def go(cmd, log):
        fh = open(out / log, "w", encoding="utf-8")
        return subprocess.Popen([sys.executable, PANEL, *cmd], cwd=str(V2), stdout=fh, stderr=subprocess.STDOUT,
                                creationflags=0x4000), fh
    ps = [go(["stage1", a, e, str(out)], f"stage1_{a}_{e}.log") for a, e in EDGES]
    for p, fh in ps:
        p.wait(); fh.close()
    print("stage1 rc", [p.returncode for p, _ in ps], flush=True)
    ps = [go(["stage2", a, e, "L", str(out)], f"stage2_{a}_{e}_L.log") for a, e in EDGES]
    for p, fh in ps:
        p.wait(); fh.close()
    print("stage2 rc", [p.returncode for p, _ in ps], flush=True)


def _sets(out: Path, a: str, e: str) -> dict:
    df = pd.read_csv(out / f"cell_{a}_{e}_L.csv")
    df["Datetime"] = to_datetime_iso(df["Datetime"], utc=True)
    df = df[np.isfinite(df.realized_r)]
    return {k: v.copy() for k, v in df.groupby("set")}


def _month(s: pd.Series) -> np.ndarray:
    return s.dt.tz_convert(None).dt.to_period("M").astype(str).to_numpy()


def _week(s: pd.Series) -> np.ndarray:
    return s.dt.tz_convert(None).dt.to_period("W").astype(str).to_numpy()


def drift_terms(edge: pd.DataFrame, pool: pd.DataFrame, rng) -> tuple[pd.Series, float, float]:
    """Per-event (r - own month's random mean), the month-matched draws' mean and SE (each draw = one random bar per
    event from its month)."""
    pm = pool.assign(m=_month(pool.Datetime)).groupby("m").realized_r.mean()
    em = _month(edge.Datetime)
    resid = edge.realized_r.to_numpy() - pd.Series(em).map(pm).to_numpy()
    draws = db.matched_draws(em, _month(pool.Datetime), pool.realized_r.to_numpy(dtype="float64"), rng)
    return pd.Series(resid, index=edge.index), float(draws.mean()), float(draws.std(ddof=1))


def one_sided(d: float, se: float) -> dict:
    return {"diff": round(d, 5), "se": round(se, 5), "lower_bound_1p645": round(d - Z * se, 5), "PASS": bool(d - Z * se > 0)}


def analyse(out: Path) -> None:
    rng = np.random.default_rng(SEED)
    res: dict = {}
    cache: dict = {}
    for a, e in EDGES:
        s = _sets(out, a, e)
        cache[(a, e)] = s
        edge, pool = s["edge"], s["random"]
        st = pd.read_csv(V2 / "assets" / a / "edges" / e / "artifacts" / "research_dataset" / f"{a.lower()}_{e}_model_rows.csv",
                         usecols=["Datetime", "realized_r"])
        st["Datetime"] = to_datetime_iso(st["Datetime"], utc=True)
        m = st.merge(edge, on="Datetime", suffixes=("_stored", ""))
        rep = {"stored": int(len(st)), "matched": int(len(m)), "max_abs_diff": float((m.realized_r - m.realized_r_stored).abs().max())}
        rep["PASS"] = bool(rep["matched"] == rep["stored"] and rep["max_abs_diff"] < 1e-9)
        er = {"reproduction": rep, "n_edge": int(len(edge)), "edge_mean_R": round(float(edge.realized_r.mean()), 5),
              "random_all_mean_R": round(float(pool.realized_r.mean()), 5)}
        ce = rc.clustered_ci(edge.realized_r, edge.Datetime)
        se_edge = (ce["mean_R"] - ce["ci_lo"]) / 1.96
        _, dmean, dse = drift_terms(edge, pool, rng)
        er["drift_month_matched"] = {"baseline_mean_R": round(dmean, 5),
                                     **one_sided(float(edge.realized_r.mean()) - dmean, float(np.sqrt(se_edge ** 2 + dse ** 2)))}
        er["counterfactuals"] = {}
        for t in [k for k in s if k not in ("edge", "random")]:
            inv = s[t]
            jd = joint_diff(edge, inv)
            se = (jd["diff"] - jd["ci_lo"]) / 1.96
            v = one_sided(jd["diff"], se)
            v.update(n_inverted=int(len(inv)), inverted_mean_R=round(float(inv.realized_r.mean()), 5))
            if len(inv) < rc.MIN_EVENTS:
                v["PASS"] = None
            er["counterfactuals"][t] = v
        res[f"{a}/{e}"] = er
    # pooled theses
    for fam, assets in POOLED.items():
        evs, invs, resid, dvar, n_tot = [], {}, [], 0.0, 0
        sizes = {a: len(cache[(a, fam)]["edge"]) for a in assets}
        n_tot = sum(sizes.values())
        for a in assets:
            s = cache[(a, fam)]
            r, _, dse = drift_terms(s["edge"], s["random"], rng)
            resid.append(pd.DataFrame({"Datetime": s["edge"].Datetime.values, "x": r.values}))
            dvar += (sizes[a] / n_tot) ** 2 * dse ** 2
            evs.append(s["edge"])
            for t in [k for k in s if k not in ("edge", "random")]:
                invs.setdefault(t, []).append(s[t])
        R = pd.concat(resid)
        d = float(R.x.mean())
        g = ((R.x - d) / len(R)).groupby(_week(to_datetime_iso(R.Datetime, utc=True))).sum()
        se = float(np.sqrt((g ** 2).sum() + dvar))
        pr = {"assets": assets, "n_edge": int(n_tot), "drift_month_matched": one_sided(d, se), "counterfactuals": {}}
        E = pd.concat(evs)
        for t, lst in invs.items():
            I = pd.concat(lst)
            jd = joint_diff(E, I)
            v = one_sided(jd["diff"], (jd["diff"] - jd["ci_lo"]) / 1.96)
            v["n_inverted"] = int(len(I))
            pr["counterfactuals"][t] = v
        pr["per_asset_diffs_positive"] = {a: {"drift": res[f"{a}/{fam}"]["drift_month_matched"]["diff"] > 0,
                                              **{t: res[f"{a}/{fam}"]["counterfactuals"][t]["diff"] > 0
                                                 for t in res[f"{a}/{fam}"]["counterfactuals"]}} for a in assets}
        res[f"POOLED/{fam}"] = pr
    # verdicts
    verdicts = {}
    for a, e in EDGES:
        key = f"{a}/{e}"
        src = res[f"POOLED/{e}"] if e in POOLED else res[key]
        ok = [src["drift_month_matched"]["PASS"]] + [c["PASS"] for c in src["counterfactuals"].values()]
        own = (all(res[f"POOLED/{e}"]["per_asset_diffs_positive"][a].values()) if e in POOLED else True)
        verdicts[key] = ("INCONCLUSIVE" if any(x is None for x in ok) and all(x is not False for x in ok) else
                         "PASS" if all(ok) and own else "FAIL")
        if not res[key]["reproduction"]["PASS"]:
            verdicts[key] = "VOID (reproduction failed)"
    res["_verdicts"] = verdicts
    (out / "step5_theory_results.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print(json.dumps(res, indent=1, default=str), flush=True)


if __name__ == "__main__":
    {"run": run, "analyse": analyse}[sys.argv[1]](Path(sys.argv[2]))
