"""Curated quote-tick layer: data quality filter + futures roll adjustment.

AFML Ch.1 s.1.3.1.1 (p.7, data curators) and Ch.2: research must start from correctly processed raw data.
Two problems found in the Ch.2 walkthrough (2026-10-23):

1. Bad quotes. On 2025-08-26 06:24-09:20 UTC Dukascopy served JPN quotes near 15,150 instead of ~42,400
   (another instrument's price). They entered bars, Event_Vol, FFD, HMM and model rows. Nothing filtered them.
2. Futures rolls (AFML s.2.4.3 p.36-37). Dukascopy's USOIL CFD follows the WTI front month and jumps at each
   monthly roll; research saw those jumps as returns.

QuoteTickQualityFilter is CAUSAL and STATEFUL, so research and live apply the identical rule (live cannot wait to
see whether a jump reverts):
    reject a quote whose mid jumps more than max(floor, k x daily sigma) from the last ACCEPTED mid, where daily
    sigma is an EWMA of the asset's own past daily log returns (accepted closes). If rejected quotes keep a new
    level for `reanchor_hours`, the new level is accepted (a genuine regime jump pauses data, it is not lost).

Roll adjustment: roll dates and gaps as in Snippet 2.2 (next open vs previous close), applied MULTIPLICATIVELY and
backward. AFML p.37 / Snippet 2.3 computes returns as the rolled price change over the previous RAW price, i.e. it
keeps every percentage return; scaling each pre-roll price by (resume mid / stop mid) does exactly that while the
latest prices still equal the raw feed (matchEnd), so live raw prices continue the curated history. The additive
Snippet 2.2 series was rejected: on USOIL 2022-2026 the 43 gaps sum to -30.64 USD, which lowered 2022 prices from
~86 to ~56 and inflated their percentage returns ~1.5x. Roll dates are DETECTED from the data, see detect_rolls().
Months without a detectable roll are reported as undetected -- never guessed.

Layout: assets/{ASSET}/curated/ticks/*.csv (same names and columns as raw/ticks), curated/curation_manifest.json,
curated/quality_report.json, curated/roll_calendar.json. Which assets are curated and which are roll-adjusted is
config/asset_data_policy.json; AssetLayout.tick_root follows it, so every research consumer reads curated ticks.

Run from trading_models_v2:
    python ./shared/data/tick_curation.py report --assets ALL          # filter in report mode, writes nothing
    python ./shared/data/tick_curation.py curate --assets ALL          # writes the curated layer
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

V2_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = V2_ROOT / "config" / "asset_data_policy.json"
TICK_COLUMNS = ["timestamp", "bidPrice", "askPrice", "bidVolume", "askVolume"]


# ---------------------------------------------------------------------------------------- quality filter
@dataclass
class FilterParams:
    k_daily_sigma: float = 10.0       # jump limit in multiples of the asset's daily sigma
    floor: float = 0.03               # minimum jump limit (log return), also used during warm-up
    ewm_span_days: int = 20           # daily sigma EWMA span
    warmup_days: int = 5              # days before the daily sigma is trusted
    reanchor_hours: float = 4.0       # a rejected level that persists this long is accepted


@dataclass
class FilterState:
    last_mid: float | None = None
    day: str | None = None
    day_open_close: float | None = None   # previous day's last accepted mid
    day_last_mid: float | None = None
    ewm_var: float | None = None
    days_seen: int = 0
    pending_since_ns: int | None = None
    pending_mid: float | None = None


@dataclass
class FilterLog:
    accepted: int = 0
    rejected: int = 0
    reanchors: list[dict[str, Any]] = field(default_factory=list)
    episodes: list[dict[str, Any]] = field(default_factory=list)


class QuoteTickQualityFilter:
    def __init__(self, params: FilterParams | None = None, state: FilterState | None = None) -> None:
        self.p = params or FilterParams()
        self.s = state or FilterState()
        self.log = FilterLog()
        self._alpha = 2.0 / (self.p.ewm_span_days + 1.0)
        self._episode: dict[str, Any] | None = None

    def threshold(self) -> float:
        if self.s.ewm_var is None or self.s.days_seen < self.p.warmup_days:
            return self.p.floor
        return max(self.p.floor, self.p.k_daily_sigma * math.sqrt(self.s.ewm_var))

    def _roll_day(self, day: str) -> None:
        if self.s.day is not None and self.s.day_last_mid is not None:
            if self.s.day_open_close is not None:
                r = math.log(self.s.day_last_mid / self.s.day_open_close)
                self.s.ewm_var = r * r if self.s.ewm_var is None else (1 - self._alpha) * self.s.ewm_var + self._alpha * r * r
            self.s.day_open_close = self.s.day_last_mid
            self.s.days_seen += 1
        self.s.day = day

    def _close_episode(self) -> None:
        if self._episode is not None:
            self.log.episodes.append(self._episode)
            self._episode = None

    def keep_mask(self, ts_ns: np.ndarray, mid: np.ndarray, days: np.ndarray) -> np.ndarray:
        """Accept/reject each quote in order; updates state. ts_ns int64 UTC ns, days 'YYYY-MM-DD' strings.

        Fast path (identical result): the jump limit is never below `floor`, so when no consecutive quote in the
        chunk -- nor the first quote against the last accepted mid -- moves by `floor` or more and nothing is
        pending, every quote is accepted; only the per-day state needs updating.
        """
        if len(mid) and self.s.pending_since_ns is None:
            lm = np.log(mid.astype("float64"))
            first = abs(lm[0] - math.log(self.s.last_mid)) if self.s.last_mid is not None else 0.0
            if first < self.p.floor and (len(lm) < 2 or np.max(np.abs(np.diff(lm))) < self.p.floor):
                return self._accept_all(mid, days)
        return self._keep_mask_slow(ts_ns, mid, days)

    def _accept_all(self, mid: np.ndarray, days: np.ndarray) -> np.ndarray:
        change = np.flatnonzero(np.r_[True, days[1:] != days[:-1]])
        ends = np.r_[change[1:] - 1, len(mid) - 1]
        for start, end in zip(change, ends):
            if days[start] != self.s.day:
                self._roll_day(str(days[start]))
            self.s.day_last_mid = float(mid[end])
        self.s.last_mid = float(mid[-1])
        self.s.day_last_mid = float(mid[-1])
        self.log.accepted += len(mid)
        self._close_episode()
        return np.ones(len(mid), dtype=bool)

    def _keep_mask_slow(self, ts_ns: np.ndarray, mid: np.ndarray, days: np.ndarray) -> np.ndarray:
        keep = np.zeros(len(mid), dtype=bool)
        reanchor_ns = int(self.p.reanchor_hours * 3600e9)
        for i in range(len(mid)):
            m = float(mid[i])
            if days[i] != self.s.day:
                self._roll_day(str(days[i]))
            if self.s.last_mid is None:
                ok = True
            else:
                thr = self.threshold()
                ok = abs(math.log(m / self.s.last_mid)) <= thr
                if not ok:
                    if self.s.pending_since_ns is None or abs(math.log(m / self.s.pending_mid)) > thr:
                        self.s.pending_since_ns, self.s.pending_mid = int(ts_ns[i]), m
                    elif int(ts_ns[i]) - self.s.pending_since_ns >= reanchor_ns:
                        self.log.reanchors.append({"ts": str(pd.Timestamp(int(ts_ns[i]), tz="UTC")),
                                                   "from_mid": self.s.last_mid, "to_mid": m})
                        ok = True
            if ok:
                keep[i] = True
                self.s.last_mid = m
                self.s.day_last_mid = m
                self.s.pending_since_ns = self.s.pending_mid = None
                self.log.accepted += 1
                self._close_episode()
            else:
                self.log.rejected += 1
                ts = str(pd.Timestamp(int(ts_ns[i]), tz="UTC"))
                dev = math.log(m / self.s.last_mid)
                if self._episode is None:
                    self._episode = {"start": ts, "end": ts, "n": 0, "last_good_mid": self.s.last_mid,
                                     "max_abs_dev": 0.0, "threshold": self.threshold()}
                self._episode["end"] = ts
                self._episode["n"] += 1
                self._episode["max_abs_dev"] = max(self._episode["max_abs_dev"], abs(dev))
        return keep

    def finish(self) -> FilterLog:
        self._close_episode()
        return self.log

    def state_dict(self) -> dict[str, Any]:
        return {"params": asdict(self.p), "state": asdict(self.s)}

    @classmethod
    def from_state_dict(cls, d: dict[str, Any]) -> "QuoteTickQualityFilter":
        return cls(FilterParams(**d["params"]), FilterState(**d["state"]))


# ---------------------------------------------------------------------------------------- roll detection
def detect_rolls(tick_files: list[Path], months: list[str] | None = None) -> dict[str, Any]:
    """Detect futures-roll gaps from the CFD's own quote breaks.

    Signature (measured on USOIL 2022-2026): on roll days the quotes stop 8-21 minutes before the usual daily
    close and resume at the normal reopen, giving a 66-90 minute weekday break, and the reopen price jumps by the
    contract spread. Holiday early closes (breaks of 3+ hours) are not rolls. One roll per month at most; the gap
    is mid(first tick after the break) - mid(last tick before it), as in Snippet 2.2 (next open - previous close).
    """
    breaks = []
    prev_ts = prev_mid = None
    for f in tick_files:
        d = pd.read_csv(f, usecols=["timestamp", "bidPrice", "askPrice"])
        if d.empty:
            continue
        ts = pd.to_datetime(d["timestamp"], utc=True, format="ISO8601")
        mid = ((d["bidPrice"] + d["askPrice"]) / 2.0).to_numpy()
        gap_s = ts.diff().dt.total_seconds().to_numpy().copy()
        if prev_ts is not None:
            gap_s[0] = (ts.iloc[0] - prev_ts).total_seconds()
        for i in np.flatnonzero(gap_s >= 30 * 60):
            p_ts = ts.iloc[i - 1] if i > 0 else prev_ts
            p_mid = mid[i - 1] if i > 0 else prev_mid
            breaks.append({"stop": p_ts, "resume": ts.iloc[i], "minutes": gap_s[i] / 60.0,
                           "stop_mid": float(p_mid), "resume_mid": float(mid[i])})
        prev_ts, prev_mid = ts.iloc[-1], mid[-1]
    b = pd.DataFrame(breaks)
    if b.empty:
        return {"rolls": [], "undetected_months": months or []}
    daily = b[(b["minutes"] < 600) & (b["stop"].dt.dayofweek < 5)].copy()
    daily["stop_min_of_hour"] = daily["stop"].dt.minute
    usual_close_minute = int(daily.loc[daily["minutes"] < 66, "stop_min_of_hour"].mode().iloc[0])  # e.g. 59
    early = (usual_close_minute - daily["stop_min_of_hour"]).between(8, 21)
    cand = daily[early & daily["minutes"].between(66, 90)].copy()
    cand["month"] = cand["stop"].dt.strftime("%Y-%m")
    cand["abs_jump"] = (np.log(cand["resume_mid"] / cand["stop_mid"])).abs()
    picked = cand.sort_values("abs_jump", ascending=False).groupby("month").head(1).sort_values("stop")
    rolls = [{"month": r.month, "stop_utc": str(r.stop), "resume_utc": str(r.resume), "break_minutes": round(r.minutes, 1),
              "stop_mid": r.stop_mid, "resume_mid": r.resume_mid, "gap": r.resume_mid - r.stop_mid,
              "log_jump": math.log(r.resume_mid / r.stop_mid)} for r in picked.itertuples()]
    first, last = b["stop"].min(), b["resume"].max()
    all_months = list(pd.period_range(first.strftime("%Y-%m"), last.strftime("%Y-%m"), freq="M").strftime("%Y-%m"))
    detected = {r["month"] for r in rolls}
    return {"signature": {"usual_close_minute": usual_close_minute, "early_stop_minutes": [8, 21],
                          "break_minutes": [66, 90]},
            "rolls": rolls, "undetected_months": [m for m in all_months if m not in detected]}


def cumulative_backward_factor(ts_ns: np.ndarray, rolls: list[dict[str, Any]]) -> np.ndarray:
    """Multiplicative backward roll factor per tick: product of (resume_mid / stop_mid) over rolls AFTER the tick.

    Curated price = raw price x factor. Within-contract percentage returns are unchanged (AFML Snippet 2.3), the
    roll jump disappears, and ticks after the last roll keep factor 1 (latest prices equal raw, matchEnd=True).
    """
    factor = np.ones(len(ts_ns), dtype="float64")
    for r in rolls:
        resume_ns = pd.Timestamp(r["resume_utc"]).value
        factor[ts_ns < resume_ns] *= r["resume_mid"] / r["stop_mid"]
    return factor


# ---------------------------------------------------------------------------------------- policy + curation
def load_policy() -> dict[str, Any]:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def asset_policy(asset: str) -> dict[str, Any]:
    pol = load_policy()
    out = dict(pol.get("default", {}))
    out.update(pol.get("assets", {}).get(asset, {}))
    return out


def live_filter_for_asset(asset: str, stored: dict[str, Any] | None) -> QuoteTickQualityFilter:
    """The live (Moneta) filter for `asset`: same rule and parameters as research.

    Restores the stored live state when present. Otherwise it is seeded with the daily sigma from the curated
    research report (so it does not start in warm-up), but NOT with the research last price: the live feed's
    price level can differ (another broker, roll-adjusted research history), so the first live quote anchors it.
    """
    if stored:
        return QuoteTickQualityFilter.from_state_dict(stored)
    params = FilterParams(**asset_policy(asset).get("quality_filter", {}))
    state = FilterState()
    report = V2_ROOT / "assets" / asset / "curated" / "quality_report.json"
    if report.exists():
        seed = json.loads(report.read_text(encoding="utf-8")).get("final_filter_state", {}).get("state", {})
        state.ewm_var = seed.get("ewm_var")
        state.days_seen = int(seed.get("days_seen") or 0)
    return QuoteTickQualityFilter(params, state)


def _tick_files(folder: Path) -> list[Path]:
    return sorted(folder.glob("*_ticks_*_to_*.csv"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def curate_asset(asset: str, v2_root: Path = V2_ROOT, *, write: bool) -> dict[str, Any]:
    pol = asset_policy(asset)
    raw_dir = v2_root / "assets" / asset / "raw" / "ticks"
    out_root = v2_root / "assets" / asset / "curated"
    files = _tick_files(raw_dir)
    if not files:
        raise SystemExit(f"{asset}: no raw tick files in {raw_dir}")
    params = FilterParams(**pol.get("quality_filter", {}))
    roll = detect_rolls(files) if pol.get("roll_adjust") else None
    rolls = roll["rolls"] if roll else []
    filt = QuoteTickQualityFilter(params)
    if write:
        (out_root / "ticks").mkdir(parents=True, exist_ok=True)
    out_files = []
    for f in files:
        d = pd.read_csv(f)
        if d.empty:
            continue
        ts = pd.to_datetime(d["timestamp"], utc=True, format="ISO8601")
        ts_ns = ts.astype("int64").to_numpy() if ts.dt.unit == "ns" else ts.dt.as_unit("ns").astype("int64").to_numpy()
        valid = (d["bidPrice"] > 0) & (d["askPrice"] >= d["bidPrice"])
        d, ts_ns, ts = d[valid], ts_ns[valid.to_numpy()], ts[valid]
        mid = ((d["bidPrice"] + d["askPrice"]) / 2.0).to_numpy()
        keep = filt.keep_mask(ts_ns, mid, ts.dt.strftime("%Y-%m-%d").to_numpy())
        factor = cumulative_backward_factor(ts_ns[keep], rolls) if rolls else None
        unchanged = bool(keep.all() and valid.all() and (factor is None or np.all(factor == 1.0)))
        if write:
            target = out_root / "ticks" / f.name
            if target.exists():
                target.unlink()
            if unchanged:
                # identical to the raw file: hard link, no extra disk (same volume)
                os.link(f, target)
                out_files.append({"name": f.name, "rows": int(len(d)), "identical_to_raw": True})
                continue
            d = d[keep].copy()
            if factor is not None:
                d["bidPrice"] = d["bidPrice"] * factor
                d["askPrice"] = d["askPrice"] * factor
            d[TICK_COLUMNS].to_csv(target, index=False)
            out_files.append({"name": f.name, "rows": int(len(d)), "identical_to_raw": False, "sha256": _sha256(target),
                              "rejected_ticks": int((~keep).sum()), "roll_adjusted": factor is not None and bool(np.any(factor != 1.0))})
    log = filt.finish()
    report = {
        "asset": asset, "generated_utc": datetime.now(timezone.utc).isoformat(), "params": asdict(params),
        "raw_files": len(files), "accepted_ticks": log.accepted, "rejected_ticks": log.rejected,
        "rejected_episodes": log.episodes, "reanchors": log.reanchors,
        "roll_adjust": bool(pol.get("roll_adjust")),
        "rolls_detected": len(rolls), "undetected_roll_months": roll["undetected_months"] if roll else [],
        "final_filter_state": filt.state_dict(),
    }
    if write:
        (out_root / "quality_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        if roll is not None:
            (out_root / "roll_calendar.json").write_text(json.dumps(roll, indent=2, default=str), encoding="utf-8")
        manifest = {"asset": asset, "source": "raw/ticks", "files": out_files, "generated_utc": report["generated_utc"],
                    "quality_filter": asdict(params), "roll_adjust": bool(pol.get("roll_adjust")),
                    "roll_method": "multiplicative backward (AFML Snippet 2.2 roll gaps, Snippet 2.3 returns preserved; latest prices equal raw)" if rolls else None}
        (out_root / "curation_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["report", "curate", "rolls"])
    ap.add_argument("--assets", nargs="+", required=True)
    ap.add_argument("--v2-root", type=Path, default=V2_ROOT)
    a = ap.parse_args()
    assets = sorted(p.name for p in (a.v2_root / "assets").iterdir() if (p / "raw" / "ticks").exists()) if a.assets == ["ALL"] else a.assets
    for asset in assets:
        if a.command == "rolls":
            r = detect_rolls(_tick_files(a.v2_root / "assets" / asset / "raw" / "ticks"))
            print(json.dumps({"asset": asset, "rolls": len(r["rolls"]), "undetected": r["undetected_months"]}), flush=True)
            continue
        rep = curate_asset(asset, a.v2_root, write=(a.command == "curate"))
        eps = rep["rejected_episodes"]
        print(json.dumps({"asset": asset, "accepted": rep["accepted_ticks"], "rejected": rep["rejected_ticks"],
                          "episodes": len(eps), "reanchors": len(rep["reanchors"]), "rolls": rep["rolls_detected"],
                          "worst_episodes": sorted(eps, key=lambda e: -e["n"])[:3]}, default=str), flush=True)


if __name__ == "__main__":
    main()
