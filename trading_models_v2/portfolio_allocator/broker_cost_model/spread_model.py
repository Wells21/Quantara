"""Broker SPREAD model: clock alignment, model fit, validation, level, live-entry calibration (RUNBOOK v5.1).

Decisions (all measured on Moneta 2026-10-16, see config/us500_edge_search_ledger.json RUNBOOK_v5.1):
  * Dukascopy's own spread is NOT used as a predictor (it is a fixed constant on several CFDs); the broker's own ticks are.
  * FIXED_POINTS when the broker's point spread is ~constant (p90/p10 <= 1.10), else TABLE = mean spread by UTC
    hour-of-week x tercile of trailing-1-day realized vol (vol from the accurate mid path). Unit (points or bps) =
    the one with the lower month-to-month coefficient of variation.
  * Level = last 14 days (walk-forward: best 1-4 week forecaster on all 10 Moneta assets); uncertainty band = the
    2-week window's walk-forward error at 8 and 13 week horizons (max).
  * Live-entry calibration = actual/pred spread at the firm's real executed entries, shrunk by n/(n+30).
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from . import server_tz as TZ
from .vol_cache import rv1d_from_minute_mid


def align(broker: pd.DataFrame, duka: pd.DataFrame | None, rule: str) -> pd.DataFrame:
    """SERVER-clock broker minutes -> UTC. Per W-SAT week: measured hour shift (max 1-min mid-return correlation vs the
    Dukascopy mid, must be > 0.3) where Dukascopy overlaps; otherwise the detected server_tz rule."""
    srv = pd.DatetimeIndex(broker.index); srv = srv.tz_localize(None) if srv.tz is not None else srv
    b = broker.copy(); b.index = srv
    lr = lambda x: np.log(x["mid"]).diff()
    parts = []
    for _, g in b.groupby(b.index.to_period("W-SAT")):
        best = None
        if duka is not None and len(duka):
            for h in (1, 2, 3, 4):
                gs = g.copy(); gs.index = (gs.index - pd.Timedelta(hours=h)).tz_localize("UTC")
                j = pd.concat([lr(gs), lr(duka.reindex(gs.index))], axis=1).dropna()
                if len(j) > 300:
                    c = j.corr().iloc[0, 1]
                    if best is None or c > best[1]:
                        best = (h, c)
        if best and best[1] > 0.3:
            gs = g.copy(); gs.index = (gs.index - pd.Timedelta(hours=best[0])).tz_localize("UTC")
            parts.append(gs.assign(shift_h=best[0], shift_source="measured"))
        else:
            utc = TZ.server_to_utc(g.index, rule)
            gs = g.copy(); gs["shift_h"] = ((g.index - utc.tz_localize(None)) / pd.Timedelta(hours=1)).astype(int)
            gs.index = utc; parts.append(gs.assign(shift_source="rule"))
    out = pd.concat(parts).sort_index()
    return out[~out.index.duplicated(keep="last")]


def add_state(df: pd.DataFrame) -> pd.DataFrame:
    """Volatility state per minute. Minutes right after a market re-open (trailing 1440-minute window still spanning the
    weekend -> rv undefined) carry the last valid value forward -- exactly what the Gate C / 00d loader does (it looks up
    the latest cached minute) -- so the wide-spread open minutes are KEPT (dropping them biased the level down 10-14%)."""
    df = df.copy()
    df["rv1d"] = rv1d_from_minute_mid(df["mid"]).reindex(df.index).ffill()
    df["how"] = df.index.dayofweek * 24 + df.index.hour
    df["pts"] = df.spread_bps * df["mid"] / 1e4
    return df.dropna(subset=["rv1d"])   # only the very first minutes of the history (no prior vol) remain undefined


def _table(tr: pd.DataFrame, unit: str, q: list[float]):
    st = np.digitize(tr.rv1d, q)
    t = tr.assign(st=st).groupby(["how", "st"])[unit].agg(["mean", "size"])
    hour = tr.groupby("how")[unit].mean(); mult = tr.assign(st=st).groupby("st")[unit].mean() / tr[unit].mean()
    def pred(df: pd.DataFrame) -> np.ndarray:
        s = np.digitize(df.rv1d, q); out = np.empty(len(df))
        for i, (h, k) in enumerate(zip(df.how.to_numpy(), s)):
            ok = (h, k) in t.index and t["size"].get((h, k), 0) >= 30
            out[i] = t["mean"].get((h, k)) if ok else hour.get(h, tr[unit].mean()) * mult.get(k, 1.0)
        return out
    return t, hour, mult, pred


def predict_bps(cm: dict[str, Any], how: int, state: int, mid: float) -> float:
    if cm["model"] == "FIXED_POINTS":
        return cm["fixed_points"] / mid * 1e4
    k = f"{how}|{state}"
    v = cm["table"].get(k) if int(cm.get("table_counts", {}).get(k, 0)) >= 30 else None
    if v is None:
        v = float(cm["hour_mean"].get(str(how), np.mean(list(cm["hour_mean"].values())))) * float(cm["state_multiplier"].get(str(state), 1.0))
    return float(v) if cm["unit"] == "spread_bps" else float(v) / mid * 1e4


def fit(full: pd.DataFrame) -> dict[str, Any]:
    pts = full.pts; fixed = bool(pts.quantile(.9) / max(pts.quantile(.1), 1e-12) <= 1.10)
    mon = full.groupby(full.index.tz_localize(None).to_period("M"))
    cv = {u: float(mon[u].mean().std() / mon[u].mean().mean()) for u in ("pts", "spread_bps")}
    unit = "pts" if cv["pts"] <= cv["spread_bps"] else "spread_bps"
    q = [float(x) for x in np.quantile(full.rv1d, [1 / 3, 2 / 3])]
    cm: dict[str, Any] = {"model": "FIXED_POINTS" if fixed else "TABLE", "unit": "pts" if fixed else unit, "monthly_cv": cv,
                          "vol_state_thresholds_rv1d_bps": q, "minutes": int(len(full)),
                          "span": [str(full.index.min()), str(full.index.max())],
                          "mean_spread_bps_time_weighted_full_sample": round(float(full.spread_bps.mean()), 4)}
    if fixed:
        cm["fixed_points"] = float(pts.median())
    else:
        t, hour, mult, _ = _table(full, unit, q)
        cm["table"] = {f"{h}|{k}": float(v) for (h, k), v in t["mean"].items()}
        cm["table_counts"] = {f"{h}|{k}": int(v) for (h, k), v in t["size"].items()}
        cm["hour_mean"] = {str(int(h)): float(v) for h, v in hour.items()}
        cm["state_multiplier"] = {str(int(k)): float(v) for k, v in mult.items()}
    # blocked-by-week 5-fold CV of the mean (tests the conditional relation, not a single regime shift)
    weeks = full.index.tz_localize(None).to_period("W"); uw = np.array(sorted(set(weeks))); res = []
    for f in np.array_split(uw, 5):
        te, tr = full[np.isin(weeks, f)], full[~np.isin(weeks, f)]
        if not len(te) or not len(tr):
            continue
        if fixed:
            pr = float(tr.pts.median()) / te["mid"].to_numpy() * 1e4
        else:
            _, _, _, p2 = _table(tr, unit, [float(x) for x in np.quantile(tr.rv1d, [1 / 3, 2 / 3])])
            pr = p2(te) if unit == "spread_bps" else p2(te) / te["mid"].to_numpy() * 1e4
        res.append(round(float(np.mean(pr) / te.spread_bps.mean() - 1) * 100, 2))
    cm["cv_mean_bias_pct_by_week_fold"] = res
    return cm


def walkforward_band(full: pd.DataFrame, window_weeks: int = 2, horizons: tuple[int, ...] = (8, 13)) -> dict[str, Any]:
    g = full.groupby(full.index.tz_localize(None).to_period("W")); wk = g.spread_bps.mean()[g.size() > 1000]
    out = {}
    for h in horizons:
        e = [abs(wk.iloc[max(0, i - window_weeks):i].mean() / wk.iloc[i:i + h].mean() - 1) for i in range(2, len(wk) - h + 1)]
        out[f"h{h}w_mape_pct"] = round(float(np.mean(e)) * 100, 1) if e else None
    vals = [v for v in out.values() if v is not None]
    out["level_uncertainty_pct"] = max(vals) if vals else None
    out["weeks"] = int(len(wk))
    return out


def rescale_to_recent(cm: dict[str, Any], full: pd.DataFrame, days: int = 14) -> dict[str, Any]:
    recent = full[full.index >= full.index.max() - pd.Timedelta(days=days)]
    cm = dict(cm); recent_mean = float(recent.spread_bps.mean())
    if cm["model"] == "FIXED_POINTS":
        newp = float((recent.spread_bps * recent["mid"] / 1e4).median()); scale = newp / cm["fixed_points"]; cm["fixed_points"] = newp
    else:
        scale = recent_mean / float(cm["mean_spread_bps_time_weighted_full_sample"])
        cm["table"] = {k: v * scale for k, v in cm["table"].items()}; cm["hour_mean"] = {k: v * scale for k, v in cm["hour_mean"].items()}
    cm.update(level_rescale_factor_to_recent=round(scale, 4), mean_spread_bps_time_weighted=round(recent_mean, 4),
              ref_price=float(recent["mid"].median()), level_window_days=days)
    return cm


def live_entry_calibration(cm: dict[str, Any], full: pd.DataFrame, entries: pd.DataFrame, point: float, prior_n: int = 30) -> dict[str, Any]:
    """entries: the firm's executed trades for this asset with columns t (UTC), plan_spread_points, plan_entry_price."""
    cm = dict(cm)
    if entries is None or not len(entries):
        cm.update(entry_calibration_factor=1.0, entry_calibration_note="no live entries for this firm/asset yet -> 1.0")
        return cm
    q = np.asarray(cm["vol_state_thresholds_rv1d_bps"]); act, pred = [], []
    for _, r in entries.iterrows():
        i = int(full.index.searchsorted(r.t, side="right")) - 1
        if i < 0:
            continue
        row = full.iloc[i]; st = int(np.digitize([row.rv1d], q)[0])
        a = float(r.plan_spread_points) * point / float(r.plan_entry_price) * 1e4
        if np.isfinite(a):
            act.append(a); pred.append(predict_bps(cm, int(r.t.dayofweek * 24 + r.t.hour), st, float(row["mid"])))
    n = len(act)
    if not n:
        cm.update(entry_calibration_factor=1.0, entry_calibration_note="entries could not be matched -> 1.0")
        return cm
    ratio = float(np.mean(act) / np.mean(pred)); f = 1 + (n / (n + prior_n)) * (ratio - 1)
    if cm["model"] == "FIXED_POINTS":
        cm["fixed_points"] *= f
    else:
        cm["table"] = {k: v * f for k, v in cm["table"].items()}; cm["hour_mean"] = {k: v * f for k, v in cm["hour_mean"].items()}
    cm.update(entry_calibration_factor=round(f, 4), entry_calibration_n=n, entry_calibration_raw_ratio=round(ratio, 4),
              entry_calibration_note=f"actual/pred={ratio:.3f} on n={n} live entries, shrunk toward 1 by n/(n+{prior_n})")
    return cm
