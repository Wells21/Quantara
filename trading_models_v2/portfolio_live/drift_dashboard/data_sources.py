"""
Data loading for the live-vs-backtest drift dashboard.

CRITICAL DESIGN PRINCIPLE (see docs/dashboards/LIVE_VS_BACKTEST_DRIFT_DASHBOARD_PLAN.md):
scope is derived dynamically from portfolio_live/live_manifest.json's
selected_edges field, read fresh on every call. Never hardcode an edge list,
an asset list, or a "current" cutoff date. Adding a new edge or asset means
updating live_manifest.json the way promotion already does -- this module
never needs to change.

Any live trade whose edge_id is not currently selected is excluded from all
statistics but is counted and reported separately (visibly, not silently
dropped) via ScopedTrades.excluded_count / excluded_edge_ids.
"""

from __future__ import annotations

import glob
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

_THIS_FILE = Path(__file__).resolve()
_PORTFOLIO_LIVE_DIR = _THIS_FILE.parents[1]
_V2_ROOT = _THIS_FILE.parents[2]
for _path in (_PORTFOLIO_LIVE_DIR, _V2_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

import live_settings as settings  # noqa: E402

CLOSED_TRADES_JSONL = settings.OUTPUT_DIR / "portfolio_execution_bot" / "portfolio_closed_trades.jsonl"
LIVE_SIGNAL_AUDIT_CSV = settings.OUTPUT_DIR / "live_dashboard" / "live_signal_audit.csv"


class DriftDataError(RuntimeError):
    """Raised when drift-dashboard data loading must fail closed."""


def _edge_id_of(entry: Any) -> str | None:
    if isinstance(entry, dict):
        return entry.get("edge_id")
    if isinstance(entry, str):
        return entry
    return None


def load_current_live_edge_ids(manifest_path: Path | None = None) -> set[str]:
    """The single source of truth for 'which edges currently count'.

    Reads portfolio_live/live_manifest.json fresh -- never cache this across
    a long-running process, since promotion/demotion can change it between
    dashboard refreshes.
    """
    path = manifest_path or settings.LIVE_MANIFEST_PATH
    if not path.exists():
        raise DriftDataError(f"live_manifest.json not found at {path}")
    with open(path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    selected = manifest.get("selected_edges")
    if selected is None:
        raise DriftDataError("live_manifest.json has no 'selected_edges' field")
    edge_ids = {_edge_id_of(entry) for entry in selected}
    edge_ids.discard(None)
    if not edge_ids:
        raise DriftDataError("live_manifest.json 'selected_edges' resolved to zero edge_ids")
    return edge_ids


@dataclass
class ScopedTrades:
    """Closed trades split into THREE explicit buckets, none silently
    dropped:

    - in_scope: edge_id present AND currently in live_manifest.json's
      selected_edges -- the only trades any statistic may be computed from.
    - excluded_stale: edge_id present but NOT in the current live book
      (a real, known edge that has since been demoted/deregistered/is a
      prior version). ~87.8% of history as of 2026-09-24.
    - excluded_unresolved: edge_id is null and metadata_source=="unresolved"
      -- the closed-trade ledger's OWN admission that it could not match
      this position to any signal/edge at all (confirmed 2026-09-24: these
      are pre-V3 legacy positions on assets like BTCUSD/USDMXN/USOUSD that
      aren't even in the current 7-asset universe). Distinct from
      excluded_stale because we don't even know which edge to blame here.
    """

    in_scope: pd.DataFrame
    excluded_stale: pd.DataFrame
    excluded_unresolved: pd.DataFrame
    live_edge_ids: set[str]

    @property
    def total_count(self) -> int:
        return len(self.in_scope) + len(self.excluded_stale) + len(self.excluded_unresolved)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded_stale) + len(self.excluded_unresolved)

    @property
    def excluded_pct(self) -> float:
        total = self.total_count
        return 0.0 if total == 0 else 100.0 * self.excluded_count / total

    def summary(self) -> dict[str, Any]:
        stale_edge_ids = set(self.excluded_stale["edge_id"].unique()) if not self.excluded_stale.empty else set()
        return {
            "total_closed_trades": self.total_count,
            "in_scope_trades": len(self.in_scope),
            "excluded_stale_trades": len(self.excluded_stale),
            "excluded_unresolved_trades": len(self.excluded_unresolved),
            "excluded_pct_total": round(self.excluded_pct, 1),
            "live_edge_count": len(self.live_edge_ids),
            "distinct_traded_edge_count": self.in_scope["edge_id"].nunique() if not self.in_scope.empty else 0,
            "excluded_stale_edge_ids": sorted(str(e) for e in stale_edge_ids),
        }


def load_closed_trades(jsonl_path: Path | None = None) -> pd.DataFrame:
    path = jsonl_path or CLOSED_TRADES_JSONL
    if not path.exists():
        raise DriftDataError(f"Closed-trade ledger not found at {path}")
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    if not rows:
        raise DriftDataError(f"Closed-trade ledger at {path} is empty")
    df = pd.DataFrame(rows)
    required = ["edge_id", "asset", "entry_time", "exit_time", "exit_reason", "realized_r"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise DriftDataError(f"Closed-trade ledger missing required columns: {missing}")
    df["entry_time"] = to_datetime_iso(df["entry_time"], utc=True, errors="coerce")
    df["exit_time"] = to_datetime_iso(df["exit_time"], utc=True, errors="coerce")
    df["hold_hours"] = (df["exit_time"] - df["entry_time"]).dt.total_seconds() / 3600.0
    for col in ("realized_r", "mfe_r", "mae_r", "bars_held"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def scope_closed_trades(
    df: pd.DataFrame | None = None,
    live_edge_ids: set[str] | None = None,
) -> ScopedTrades:
    """Split closed trades into in-scope (current live book) vs. excluded.

    in_scope = trades of a currently selected edge placed by its CURRENT
    certified version (portfolio_live/edge_versions.py: the trade's
    live_manifest_hash must be registered with the edge's current model +
    calibrator). Trades of earlier versions of the same edge_id go to
    excluded_stale -- they say nothing about the version now trading.
    Every report must show ScopedTrades.summary()'s excluded count visibly.
    """
    from portfolio_live import edge_versions

    trades = df if df is not None else load_closed_trades()
    edge_ids = live_edge_ids if live_edge_ids is not None else load_current_live_edge_ids()

    unresolved_mask = trades["edge_id"].isna()
    if "metadata_source" in trades.columns:
        unresolved_mask = unresolved_mask | (trades["metadata_source"] == "unresolved")

    edge_versions.register_current_manifest()
    versions = edge_versions.current_versions()
    current_version_mask = pd.Series(False, index=trades.index)
    for eid in edge_ids:
        if eid not in versions:
            continue
        hashes = edge_versions.manifest_hashes_for_version(eid, versions[eid]["version_id"])
        current_version_mask |= (trades["edge_id"] == eid) & trades.get("live_manifest_hash", pd.Series(index=trades.index, dtype=object)).isin(hashes)

    in_scope_mask = (~unresolved_mask) & current_version_mask
    stale_mask = (~unresolved_mask) & (~current_version_mask)

    return ScopedTrades(
        in_scope=trades.loc[in_scope_mask].copy(),
        excluded_stale=trades.loc[stale_mask].copy(),
        excluded_unresolved=trades.loc[unresolved_mask].copy(),
        live_edge_ids=edge_ids,
    )


def resolve_gate_c_summary_path(edge_id: str) -> Path | None:
    """Dynamically resolve the most recent Gate C execution-velocity summary
    for a given edge_id, by glob -- never a hardcoded per-asset file list.
    Returns None if no Gate C summary has been persisted for this edge
    (a real, visible gap -- callers must surface this, not silently skip).
    """
    if "/" not in edge_id:
        raise DriftDataError(f"Malformed edge_id (expected ASSET/family_side): {edge_id!r}")
    asset, family_side = edge_id.split("/", 1)
    pattern = str(
        _V2_ROOT / "assets" / asset / "edges" / family_side / "artifacts"
        / "v3_horizon_lab" / "v3_execution_velocity_summary_*.csv"
    )
    matches = sorted(glob.glob(pattern))
    return Path(matches[-1]) if matches else None


def resolve_gate_a_grid_path(edge_id: str) -> Path | None:
    """Dynamically resolve the most recent Gate A hybrid-fast-grid CSV for a
    given edge_id, by glob. Returns None if none has been persisted (a real
    gap -- e.g. confirmed 2026-09-24 for 3 of the 21 currently-deployed
    edges)."""
    if "/" not in edge_id:
        raise DriftDataError(f"Malformed edge_id (expected ASSET/family_side): {edge_id!r}")
    asset, family_side = edge_id.split("/", 1)
    pattern = str(
        _V2_ROOT / "assets" / asset / "edges" / family_side / "artifacts"
        / "v3_horizon_lab" / "v3_hybrid_fast_grid_*.csv"
    )
    matches = sorted(glob.glob(pattern))
    return Path(matches[-1]) if matches else None


def load_gate_c_reference(edge_id: str, label: str | None = None) -> dict[str, Any] | None:
    """Gate C summary row(s) for one edge, or None if none persisted (visible gap).

    Gate C runs in batches, so an edge's winning label can sit in any
    v3_execution_velocity_summary_*.csv, not only the newest. The row for the
    edge's CURRENT geometry label (read from its own code when label is None) is
    taken from the newest summary that contains it.
    """
    if "/" not in edge_id:
        raise DriftDataError(f"Malformed edge_id (expected ASSET/family_side): {edge_id!r}")
    if label is None:
        from portfolio_live.drift_dashboard.edge_reference import read_current_edge_geometry

        label = read_current_edge_geometry(edge_id).config_label
    asset, family_side = edge_id.split("/", 1)
    pattern = str(
        _V2_ROOT / "assets" / asset / "edges" / family_side / "artifacts"
        / "v3_horizon_lab" / "v3_execution_velocity_summary_*.csv"
    )
    for path in sorted(glob.glob(pattern), reverse=True):
        df = pd.read_csv(path)
        if "label" not in df.columns:
            continue
        rows = df.loc[df["label"] == label]
        if "is_control" in rows.columns and (rows["is_control"] != True).any():  # noqa: E712
            rows = rows.loc[rows["is_control"] != True]  # noqa: E712
        if not rows.empty:
            return {"edge_id": edge_id, "label": label, "source_path": path, "rows": rows.to_dict(orient="records")}
    return None


def load_live_signal_audit(csv_path: Path | None = None) -> pd.DataFrame:
    path = csv_path or LIVE_SIGNAL_AUDIT_CSV
    if not path.exists():
        raise DriftDataError(f"live_signal_audit.csv not found at {path}")
    df = pd.read_csv(path)
    for col in ("calibrated_probability", "raw_probability", "live_calibrated_probability"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


if __name__ == "__main__":
    edge_ids = load_current_live_edge_ids()
    print(f"Current live edge count: {len(edge_ids)}")
    scoped = scope_closed_trades(live_edge_ids=edge_ids)
    print(json.dumps(scoped.summary(), indent=2))
    print()
    print("Gate C / Gate A artifact availability per live edge:")
    for eid in sorted(edge_ids):
        gate_c = resolve_gate_c_summary_path(eid)
        gate_a = resolve_gate_a_grid_path(eid)
        print(f"  {eid}: gate_c={'YES' if gate_c else 'MISSING'}  gate_a={'YES' if gate_a else 'MISSING'}")
