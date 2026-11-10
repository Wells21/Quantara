from __future__ import annotations

"""
Quantara Phase D.4 consolidated real-history provider certification.

READ-ONLY WITH RESPECT TO PRODUCTION:
- does not install or activate routing
- does not mutate live settings
- does not write config/phase_d activation files
- writes only one certification report under portfolio_live/outputs (unless
  --output is explicitly supplied)

Authority chain:
1. Reconstruct the exact 6,000-row Phase C canonical authority exactly as the
   already-certified D.2/D.3 certifiers do.
2. Compare each incremental candidate provider to the original candidate source.
3. Compare candidate+primary provider output to runtime.generate_candidate_events,
   which is the currently active D.3 accepted-event authority.
4. Apply the exact D.3 selected-feature collision sanitization and compare the
   exact selected live model row produced from provider events vs authority events.
5. Bridge every mature accepted restart-tail row through the unchanged runtime.score path.
6. Prove provider restart equivalence and normal-update no-file-I/O.
"""

import argparse
import builtins
import contextlib
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd

CERT_SCHEMA_VERSION = "phase_d4_provider_real_history_certification_v2"
CERT_ROWS = 6000
STRICT_ATOL = 5e-10
STRICT_RTOL = 5e-10

SCORE_COLUMNS = [
    "Datetime",
    "raw_probability",
    "calibrated_probability",
    "afml_bet_size",
    "selected_threshold",
    "approved_signal",
    "signal_id",
]

EVENT_IDENTITY_COLUMNS = [
    "Datetime",
    "event_id",
    "edge_id",
    "side",
    "candidate_event_generator",
    "event_known_after_close",
    "uses_future_information",
]

PRIMARY_IDENTITY_COLUMNS = list(EVENT_IDENTITY_COLUMNS)


class CertificationError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise CertificationError(f"expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _git_head(repo_root: Path) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except Exception:
        return None


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for col in ("Datetime", "Bar_Start"):
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], utc=True, errors="coerce")
    if "Datetime" in out.columns:
        out = (
            out.dropna(subset=["Datetime"])
            .sort_values("Datetime", kind="mergesort")
            .reset_index(drop=True)
        )
    return out


def _as_event_frame(rows: list[Mapping[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return _normalize(pd.DataFrame([dict(row) for row in rows]))


def _iso_list(frame: pd.DataFrame) -> list[str]:
    if frame is None or frame.empty or "Datetime" not in frame.columns:
        return []
    return [
        pd.Timestamp(value).tz_convert("UTC").isoformat()
        if pd.Timestamp(value).tzinfo is not None
        else pd.Timestamp(value).tz_localize("UTC").isoformat()
        for value in pd.to_datetime(frame["Datetime"], utc=True, errors="coerce")
    ]


def _timestamp_parity(left: pd.DataFrame, right: pd.DataFrame) -> dict[str, Any]:
    a = _iso_list(left)
    b = _iso_list(right)
    ok = a == b
    first = None
    if not ok:
        n = max(len(a), len(b))
        for i in range(n):
            av = a[i] if i < len(a) else None
            bv = b[i] if i < len(b) else None
            if av != bv:
                first = {"index": i, "authority": av, "provider": bv}
                break
    return {
        "ok": ok,
        "authority_count": len(a),
        "provider_count": len(b),
        "first_mismatch": first,
    }


def _frame_subset_parity(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    c3cert: Any,
    columns: list[str],
    atol: float = STRICT_ATOL,
    rtol: float = STRICT_RTOL,
) -> dict[str, Any]:
    cols = [str(c) for c in columns if c in left.columns and c in right.columns]
    if not cols:
        return {"ok": False, "reason": "no_common_required_columns", "columns": []}
    try:
        result = c3cert._compare_frames(
            _normalize(left),
            _normalize(right),
            atol=float(atol),
            rtol=float(rtol),
            required_columns=cols,
        )
        result = dict(result)
        result["columns"] = cols
        return result
    except TypeError:
        a = _normalize(left).loc[:, cols].copy()
        b = _normalize(right).loc[:, cols].copy()
        result = c3cert._compare_frames(a, b, atol=float(atol), rtol=float(rtol))
        result = dict(result)
        result["columns"] = cols
        return result


def _common_context_parity(
    authority: pd.DataFrame,
    provider: pd.DataFrame,
    *,
    c3cert: Any,
) -> dict[str, Any]:
    if authority.empty and provider.empty:
        return {
            "ok": True,
            "common_columns": [],
            "authority_only_columns": [],
            "provider_only_columns": [],
            "parity": {"ok": True, "rows_checked": 0},
        }
    common = [c for c in authority.columns if c in provider.columns]
    parity = _frame_subset_parity(authority, provider, c3cert=c3cert, columns=common)
    return {
        "ok": bool(parity.get("ok")),
        "common_columns": common,
        "authority_only_columns": [c for c in authority.columns if c not in provider.columns],
        "provider_only_columns": [c for c in provider.columns if c not in authority.columns],
        "parity": parity,
    }


def _identity_parity(
    authority: pd.DataFrame,
    provider: pd.DataFrame,
    *,
    c3cert: Any,
    columns: list[str],
) -> dict[str, Any]:
    if authority.empty and provider.empty:
        return {"ok": True, "columns": [], "rows_checked": 0}
    required = list(columns)
    missing = [
        name for name in required
        if name not in authority.columns or name not in provider.columns
    ]
    if missing:
        return {
            "ok": False,
            "reason": "identity_columns_missing",
            "missing": missing,
            "required": required,
        }
    return _frame_subset_parity(
        authority, provider, c3cert=c3cert, columns=required
    )


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    arr = np.asarray(values, dtype="float64")
    return {
        "count": int(arr.size),
        "mean_ms": float(np.mean(arr)),
        "p50_ms": float(np.quantile(arr, 0.50)),
        "p95_ms": float(np.quantile(arr, 0.95)),
        "p99_ms": float(np.quantile(arr, 0.99)),
        "max_ms": float(np.max(arr)),
        "sum_ms": float(np.sum(arr)),
    }


def _source_raw_candidate(runtime: Any, d1_edge: Mapping[str, Any], features: pd.DataFrame) -> pd.DataFrame:
    candidate = d1_edge.get("candidate") or {}
    names = [str(x) for x in candidate.get("runtime_entry_functions") or []]
    if len(names) != 1:
        raise CertificationError(
            f"{d1_edge.get('edge_id')}: expected one candidate runtime entry, got {names}"
        )
    fn = getattr(runtime, names[0], None)
    if not callable(fn):
        raise CertificationError(
            f"{d1_edge.get('edge_id')}: runtime does not expose source candidate {names[0]}"
        )
    result = fn(features.copy())
    if not isinstance(result, pd.DataFrame):
        raise CertificationError(
            f"{d1_edge.get('edge_id')}: source candidate returned non-DataFrame"
        )
    return _normalize(result)


def _provider_raw_replay(
    *,
    d1_edge: Mapping[str, Any],
    projection: pd.DataFrame,
    root: Path,
    create_candidate_provider: Any,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Use the provider's certified offline replay contract.

    This intentionally lets each provider apply the exact same normalization it
    uses in its own source-parity tests (for example, dropping leading ATR warmup
    rows).  Normal-path timing is measured separately after bootstrap.
    """
    c_sig = str(((d1_edge.get("candidate") or {}).get("candidate_logic_signature")) or "")
    candidate = create_candidate_provider(c_sig, d1_edge=d1_edge, v2_root=str(root))
    started = time.perf_counter()
    replayed = candidate.replay(projection.copy())
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if not isinstance(replayed, pd.DataFrame):
        raise CertificationError(
            f"{d1_edge.get('edge_id')}: candidate provider replay returned non-DataFrame"
        )
    return _normalize(replayed), {
        "elapsed_ms": elapsed_ms,
        "state": candidate.state_summary() if callable(getattr(candidate, "state_summary", None)) else {},
    }


def _restart_cut(
    projection: pd.DataFrame,
    authority_accepted: pd.DataFrame,
    authority_raw: pd.DataFrame,
    *,
    minimum_bootstrap_rows: int = 1200,
) -> int:
    ts_to_index = {
        pd.Timestamp(ts): int(i)
        for i, ts in enumerate(pd.to_datetime(projection["Datetime"], utc=True))
    }
    source = authority_accepted if not authority_accepted.empty else authority_raw
    if source.empty:
        raise CertificationError("cannot prove restart: authority has no events")

    candidates: list[int] = []
    for ts in pd.to_datetime(source["Datetime"], utc=True):
        idx = ts_to_index.get(pd.Timestamp(ts))
        if idx is not None and int(idx) > int(minimum_bootstrap_rows):
            candidates.append(int(idx))
    candidates = sorted(set(candidates))
    if not candidates:
        raise CertificationError(
            f"cannot prove mature restart: no authority event after row {minimum_bootstrap_rows}"
        )

    target = candidates[max(0, int(len(candidates) * 0.70) - 1)]
    cut = max(int(minimum_bootstrap_rows), int(target) - 100)
    cut = min(cut, len(projection) - 2)
    if not any(index >= cut for index in candidates):
        cut = max(int(minimum_bootstrap_rows), candidates[-1] - 5)
    if not any(index >= cut for index in candidates):
        raise CertificationError("restart cut has no authority event in the mature tail")
    return int(cut)


def _provider_restart_replay(
    *,
    d1_edge: Mapping[str, Any],
    projection: pd.DataFrame,
    cut: int,
    root: Path,
    composed_candidate_event_generator: str,
    create_candidate_provider: Any,
    create_primary_side_provider: Any,
) -> tuple[pd.DataFrame, pd.DataFrame, list[float]]:
    """Simulate the actual restart -> normal one-row live path.

    Bootstrap is allowed to normalize historical warmup.  Only rows after the
    mature cut are sent to update_one().  The runtime-level candidate generator
    label is composed here because the unchanged source runtime performs that
    relabel after primary filtering, while primary providers intentionally do not.
    """
    c_sig = str(((d1_edge.get("candidate") or {}).get("candidate_logic_signature")) or "")
    p_sig = str(((d1_edge.get("primary_side") or {}).get("primary_logic_signature")) or "")
    candidate = create_candidate_provider(c_sig, d1_edge=d1_edge, v2_root=str(root))
    primary = create_primary_side_provider(p_sig, d1_edge=d1_edge, v2_root=str(root))
    candidate.bootstrap(projection.iloc[:cut].copy())
    primary.bootstrap(projection.iloc[:cut].copy())

    raw_rows: list[dict[str, Any]] = []
    accepted_rows: list[dict[str, Any]] = []
    update_ms: list[float] = []
    for _, row in projection.iloc[cut:].iterrows():
        started = time.perf_counter()
        raw = candidate.update_one(row)
        primary.update_one(row)
        if raw is not None:
            raw_dict = dict(raw)
            raw_rows.append(raw_dict)
            accepted = primary.filter_event(raw_dict)
            if accepted is not None:
                composed = dict(accepted)
                composed["candidate_event_generator"] = str(
                    composed_candidate_event_generator
                )
                accepted_rows.append(composed)
        update_ms.append((time.perf_counter() - started) * 1000.0)
    return _as_event_frame(raw_rows), _as_event_frame(accepted_rows), update_ms


def _tail(frame: pd.DataFrame, projection: pd.DataFrame, cut: int) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    allowed = set(pd.to_datetime(projection.iloc[cut:]["Datetime"], utc=True).tolist())
    ts = pd.to_datetime(frame["Datetime"], utc=True, errors="coerce")
    return _normalize(frame.loc[ts.isin(allowed)].copy())


@contextlib.contextmanager
def _deny_file_io():
    import unittest.mock as mock

    def denied(*args: Any, **kwargs: Any):
        raise CertificationError("D4_PROVIDER_NORMAL_UPDATE_FILE_IO_FORBIDDEN")

    stack = contextlib.ExitStack()
    try:
        stack.enter_context(mock.patch.object(builtins, "open", side_effect=denied))
        stack.enter_context(mock.patch.object(Path, "open", side_effect=denied))
        stack.enter_context(mock.patch.object(Path, "read_text", side_effect=denied))
        stack.enter_context(mock.patch.object(Path, "read_bytes", side_effect=denied))
        for obj, name in [
            (pd, "read_csv"),
            (pd, "read_parquet"),
            (pd, "read_pickle"),
            (np, "load"),
        ]:
            if hasattr(obj, name):
                stack.enter_context(mock.patch.object(obj, name, side_effect=denied))
        try:
            import joblib
            if hasattr(joblib, "load"):
                stack.enter_context(mock.patch.object(joblib, "load", side_effect=denied))
        except Exception:
            pass
        yield
    finally:
        stack.close()


def _no_io_probe(
    *,
    d1_edge: Mapping[str, Any],
    projection: pd.DataFrame,
    raw_authority: pd.DataFrame,
    root: Path,
    create_candidate_provider: Any,
    create_primary_side_provider: Any,
) -> dict[str, Any]:
    if len(projection) < 20:
        raise CertificationError("projection too short for no-I/O probe")

    event_indices: list[int] = []
    ts_to_index = {
        pd.Timestamp(ts): int(i)
        for i, ts in enumerate(pd.to_datetime(projection["Datetime"], utc=True))
    }
    if not raw_authority.empty:
        for ts in pd.to_datetime(raw_authority["Datetime"], utc=True):
            idx = ts_to_index.get(pd.Timestamp(ts))
            if idx is not None:
                event_indices.append(int(idx))

    if event_indices:
        target = event_indices[-1]
        start = max(1, target - 3)
        stop = min(len(projection), target + 4)
    else:
        start = max(1, len(projection) - 8)
        stop = len(projection)

    c_sig = str(((d1_edge.get("candidate") or {}).get("candidate_logic_signature")) or "")
    p_sig = str(((d1_edge.get("primary_side") or {}).get("primary_logic_signature")) or "")
    candidate = create_candidate_provider(c_sig, d1_edge=d1_edge, v2_root=str(root))
    primary = create_primary_side_provider(p_sig, d1_edge=d1_edge, v2_root=str(root))
    candidate.bootstrap(projection.iloc[:start].copy())
    primary.bootstrap(projection.iloc[:start].copy())

    raw_seen = 0
    accepted_seen = 0
    with _deny_file_io():
        for _, row in projection.iloc[start:stop].iterrows():
            raw = candidate.update_one(row)
            primary.update_one(row)
            if raw is None:
                continue
            raw_seen += 1
            accepted = primary.filter_event(dict(raw))
            if accepted is not None:
                accepted_seen += 1

    return {
        "status": "PASS",
        "start_index": int(start),
        "stop_index_exclusive": int(stop),
        "bars_checked": int(stop - start),
        "raw_events_seen": int(raw_seen),
        "accepted_events_seen": int(accepted_seen),
    }


def _score_bridge(
    *,
    runtime: Any,
    authority_rows: pd.DataFrame,
    provider_rows: pd.DataFrame,
    c3cert: Any,
) -> dict[str, Any]:
    """Score every mature accepted row through the unchanged runtime scorer."""
    if authority_rows.empty or provider_rows.empty:
        raise CertificationError("cannot score bridge: live model rows are empty")
    if len(authority_rows) != len(provider_rows):
        raise CertificationError(
            f"cannot score bridge: row count differs {len(authority_rows)} vs {len(provider_rows)}"
        )

    a_scored, _ = runtime.score(model_rows=authority_rows.copy(), latest_only=False)
    p_scored, _ = runtime.score(model_rows=provider_rows.copy(), latest_only=False)
    a_scored = _normalize(a_scored)
    p_scored = _normalize(p_scored)

    required = list(SCORE_COLUMNS)
    missing = [
        c for c in required
        if c not in a_scored.columns or c not in p_scored.columns
    ]
    if missing:
        return {
            "status": "FAIL",
            "missing_required_score_columns": missing,
            "rows_scored": int(len(a_scored)),
        }

    parity = _frame_subset_parity(
        a_scored, p_scored, c3cert=c3cert, columns=required
    )
    return {
        "status": "PASS" if parity.get("ok") else "FAIL",
        "rows_scored": int(len(a_scored)),
        "required_score_columns": required,
        "parity": parity,
    }


def _model_row_bridge(
    *,
    runtime: Any,
    full_features: pd.DataFrame,
    authority_events: pd.DataFrame,
    provider_events: pd.DataFrame,
    selected_features: list[str],
    strip_selected_feature_collisions: Any,
    c3cert: Any,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    auth_sanitized, auth_ownership = strip_selected_feature_collisions(
        events=authority_events.copy(), selected_features=selected_features
    )
    fast_sanitized, fast_ownership = strip_selected_feature_collisions(
        events=provider_events.copy(), selected_features=selected_features
    )

    builder = getattr(runtime, "_build_scoring_rows", None)
    if not callable(builder):
        raise CertificationError(
            f"{getattr(runtime, 'EDGE_ID', '<edge>')}: _build_scoring_rows unavailable"
        )

    auth_rows, auth_manifest = builder(
        features=full_features.copy(), events=auth_sanitized.copy()
    )
    fast_rows, fast_manifest = builder(
        features=full_features.copy(), events=fast_sanitized.copy()
    )
    auth_rows = _normalize(auth_rows)
    fast_rows = _normalize(fast_rows)

    required = ["Datetime", *selected_features]
    missing = [
        c for c in required if c not in auth_rows.columns or c not in fast_rows.columns
    ]
    if missing:
        return (
            {"status": "FAIL", "missing_selected_model_columns": missing},
            auth_rows,
            fast_rows,
        )

    parity = _frame_subset_parity(
        auth_rows, fast_rows, c3cert=c3cert, columns=required
    )
    report = {
        "status": "PASS" if parity.get("ok") else "FAIL",
        "authority_rows": int(len(auth_rows)),
        "provider_rows": int(len(fast_rows)),
        "selected_feature_count": len(selected_features),
        "parity": parity,
        "authority_collision_policy": auth_ownership,
        "provider_collision_policy": fast_ownership,
        "authority_row_manifest": auth_manifest,
        "provider_row_manifest": fast_manifest,
    }
    return report, auth_rows, fast_rows



def _harness_negative_controls(c3cert: Any) -> dict[str, Any]:
    """Prove the corrected release gates still reject material divergences."""
    dt = pd.date_range("2026-01-01T00:00:00Z", periods=2, freq="min")

    timestamp_authority = pd.DataFrame({"Datetime": dt})
    timestamp_mutated = timestamp_authority.copy()
    timestamp_mutated.loc[1, "Datetime"] = timestamp_mutated.loc[1, "Datetime"] + pd.Timedelta(minutes=1)
    timestamp_rejected = not _timestamp_parity(
        timestamp_authority, timestamp_mutated
    )["ok"]

    model_authority = pd.DataFrame({
        "Datetime": dt,
        "Selected_Feature": [1.0, 2.0],
    })
    model_mutated = model_authority.copy()
    model_mutated.loc[1, "Selected_Feature"] += 1e-4
    model_rejected = not _frame_subset_parity(
        model_authority,
        model_mutated,
        c3cert=c3cert,
        columns=["Datetime", "Selected_Feature"],
    ).get("ok", False)

    score_authority = pd.DataFrame({
        "Datetime": dt,
        "raw_probability": [0.40, 0.60],
        "calibrated_probability": [0.42, 0.62],
        "afml_bet_size": [0.10, 0.20],
        "selected_threshold": [0.55, 0.55],
        "approved_signal": [False, True],
        "signal_id": ["a", "b"],
    })
    score_mutated = score_authority.copy()
    score_mutated.loc[1, "raw_probability"] += 1e-4
    score_rejected = not _frame_subset_parity(
        score_authority,
        score_mutated,
        c3cert=c3cert,
        columns=SCORE_COLUMNS,
    ).get("ok", False)

    status = "PASS" if all(
        [timestamp_rejected, model_rejected, score_rejected]
    ) else "FAIL"
    return {
        "status": status,
        "timestamp_divergence_rejected": timestamp_rejected,
        "selected_model_value_divergence_rejected": model_rejected,
        "score_value_divergence_rejected": score_rejected,
        "mutation_size": 1e-4,
        "tolerance_unchanged": {"atol": STRICT_ATOL, "rtol": STRICT_RTOL},
    }

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root",
        type=Path,
        required=True,
        help="Repository root containing trading_models_v2",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Optional report path. Default is portfolio_live/outputs/"
            "phase_d4_provider_real_history_certification_v2.json"
        ),
    )
    parser.add_argument("--atol", type=float, default=STRICT_ATOL)
    parser.add_argument("--rtol", type=float, default=STRICT_RTOL)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    repo_root = Path(args.repo_root).resolve()
    root = repo_root / "trading_models_v2"
    if not root.is_dir():
        raise CertificationError(f"V2 root missing: {root}")
    if args.atol > STRICT_ATOL or args.rtol > STRICT_RTOL:
        raise CertificationError(
            "certification tolerances may not be weaker than 5e-10"
        )

    for path in (root, root / "portfolio_live"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    import phase_c_03_certify_consumer_contract as c3cert
    import phase_d_03_production_router as d3

    from shared.edges.candidate_continuations import (
        create_candidate_provider,
        discover_candidate_providers,
    )
    from shared.edges.candidate_continuations.base import (
        extract_candidate_source_identity,
    )
    from shared.edges.primary_side_continuations import (
        create_primary_side_provider,
        discover_primary_side_providers,
    )
    from shared.edges.primary_side_continuations.base import (
        extract_primary_source_identity,
    )
    from shared.edges.phase_c_consumer_adapter import (
        strip_selected_feature_collisions,
    )
    from shared.edges.phase_c_incremental_feature_engine import (
        bootstrap_compact_state,
    )
    from shared.edges.phase_d_candidate_first import (
        edge_map,
        load_phase_d1_contract,
        manifest_edge_map,
        project_candidate_features,
        project_full_c3_features,
        source_guard,
    )
    from shared.edges.composed_decision_continuations.base import (
        extract_runtime_composition_identity,
    )
    from shared.edges.composed_decision_continuations.router import (
        FAST_CONTINUATION,
        capability_summary,
        classify_selected_edges,
    )
    from shared.edges.runtime_loader import load_edge_runtime_from_manifest_entry

    harness_negative_controls = _harness_negative_controls(c3cert)
    if harness_negative_controls.get("status") != "PASS":
        raise CertificationError(
            "corrected certification harness failed its negative controls"
        )

    output = (
        Path(args.output).resolve()
        if args.output is not None
        else (
            root
            / "portfolio_live"
            / "outputs"
            / "phase_d4_provider_real_history_certification_v2.json"
        ).resolve()
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    paths = {
        "d1": root / "portfolio_live/config/phase_d/candidate_contract.json",
        "d2": root / "portfolio_live/config/phase_d/candidate_first_certification.json",
        "d3_cert": root / "portfolio_live/config/phase_d/candidate_first_routing_certification.json",
        "d3_activation": root / "portfolio_live/config/phase_d/production_routing_activation.json",
        "c1": root / "portfolio_live/config/phase_c/feature_contract.json",
        "c2": root / "portfolio_live/config/phase_c/incremental_feature_certification.json",
        "c3": root / "portfolio_live/config/phase_c/consumer_contract.json",
        "manifest": root / "portfolio_live/live_manifest.json",
    }
    for label, path in paths.items():
        if not path.is_file():
            raise CertificationError(f"required {label} file missing: {path}")

    d1 = load_phase_d1_contract(root)
    d2 = _read_json(paths["d2"])
    d3_cert = _read_json(paths["d3_cert"])
    d3_activation = _read_json(paths["d3_activation"])
    c1 = _read_json(paths["c1"])
    c2 = _read_json(paths["c2"])
    c3 = _read_json(paths["c3"])
    manifest = _read_json(paths["manifest"])

    d1_edges = edge_map(d1)
    c3_edges = edge_map(c3)
    manifest_edges = manifest_edge_map(manifest)
    selected_count = len(manifest_edges)

    print("=" * 118)
    print("QUANTARA PHASE D.4 — CONSOLIDATED REAL-HISTORY PROVIDER CERTIFICATION")
    print("=" * 118)
    print(f"Repo root:          {repo_root}")
    print(f"V2 root:            {root}")
    print(f"Selected edges:     {selected_count}")
    print(f"Authority rows:     {CERT_ROWS}")
    print(f"Tolerance:          atol={args.atol} rtol={args.rtol}")
    print(f"Output:             {output}")
    print("Production mutation: NONE")
    print("Harness contract:   provider replay normalization + mature restart tail")
    print("Context diagnostics: non-gating; selected model rows remain exact-gating")
    print()

    fatal_errors: list[str] = []

    if selected_count <= 0:
        fatal_errors.append("selected edge universe is empty")
    if set(d1_edges) != set(c3_edges) or set(d1_edges) != set(manifest_edges):
        fatal_errors.append("D.1/C.3/live-manifest selected-edge universe differs")

    source = source_guard(v2_root=root, d1_contract=d1, c3_contract=c3)
    if source.get("status") != "PASS":
        fatal_errors.extend(
            "source_guard:" + str(x) for x in source.get("failures") or []
        )

    static_d3 = d3.static_contract_report(root)
    if static_d3.get("status") != "PASS":
        fatal_errors.extend(
            "d3_static:" + str(x) for x in static_d3.get("failures") or []
        )
    activation_validation = d3.validate_activation(d3_activation, v2_root=root)
    if activation_validation.get("status") != "PASS":
        fatal_errors.extend(
            "d3_activation:" + str(x)
            for x in activation_validation.get("failures") or []
        )

    d3_chain_ok = (
        d3_cert.get("status") == "PASS"
        and d3_cert.get("production_activation_allowed") is True
        and int(d3_cert.get("selected_edge_count") or -1) == selected_count
        and int(d3_cert.get("deferred_projection_edges_passed") or -1)
        == selected_count
        and (d3_cert.get("c5_model_decision_parity") or {}).get("status")
        == "PASS"
        and (d3_cert.get("d2_candidate_authority") or {}).get("status")
        == "PASS"
    )
    if not d3_chain_ok:
        fatal_errors.append(
            "existing D.3 downstream certification chain is not current all-edge PASS"
        )

    candidate_registry = discover_candidate_providers()
    primary_registry = discover_primary_side_providers()
    capabilities = classify_selected_edges(
        d1_edges=d1_edges,
        manifest_edges=manifest_edges,
        candidate_registry=candidate_registry,
        primary_registry=primary_registry,
    )
    caps = capability_summary(capabilities)
    if int(caps.get("fast_continuation") or 0) != selected_count:
        fatal_errors.append(f"provider capability is not all-edge FAST: {caps}")
    non_fast = sorted(
        edge_id
        for edge_id, cap in capabilities.items()
        if cap.mode != FAST_CONTINUATION
    )
    if non_fast:
        fatal_errors.append("non-fast edges: " + ",".join(non_fast))

    if fatal_errors:
        report = {
            "schema_version": CERT_SCHEMA_VERSION,
            "status": "FAIL",
            "production_mutated": False,
            "provider_overlay_wiring_allowed": False,
            "fatal_errors": fatal_errors,
            "source_guard": source,
            "d3_static_contract": static_d3,
            "d3_activation_validation": activation_validation,
            "capability_summary": caps,
            "git_head_provenance_only": _git_head(repo_root),
        }
        output.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print("FAIL: preflight did not pass.")
        for item in fatal_errors:
            print(" -", item)
        print("Report:", output)
        return 1

    assets = sorted(
        {
            str(row.get("asset") or "").upper()
            for row in manifest_edges.values()
            if str(row.get("asset") or "")
        }
    )

    results: dict[str, Any] = {}
    asset_builds: list[dict[str, Any]] = []
    all_fast_update_ms: list[float] = []

    print("Preflight: PASS")
    print("Harness negative controls: PASS")
    print(f"Assets:    {len(assets)}")
    print(f"FAST:      {caps.get('fast_continuation')}/{selected_count}")
    print()

    with c3cert._isolated_dependency_authority(root, assets) as sandbox:
        for asset in assets:
            asset_edges = [
                edge_id
                for edge_id in sorted(manifest_edges)
                if str(manifest_edges[edge_id].get("asset") or "").upper()
                == asset
            ]
            c2_asset = (c2.get("assets") or {}).get(asset) or {}
            representative = str(
                c2_asset.get("canonical_core_representative_edge_id") or ""
            )
            if representative not in manifest_edges:
                error = (
                    f"{asset}: C.2 representative unavailable: {representative!r}"
                )
                fatal_errors.append(error)
                print(error)
                continue

            try:
                rep_runtime = load_edge_runtime_from_manifest_entry(
                    manifest_edges[representative], v2_root=root
                )
                bars_all = c3cert._load_bars(rep_runtime, v2_root=root)
                if len(bars_all) < CERT_ROWS:
                    raise CertificationError(
                        f"need >= {CERT_ROWS} verified bars, have {len(bars_all)}"
                    )
                bars = bars_all.tail(CERT_ROWS).reset_index(drop=True)

                started = time.perf_counter()
                authority_features = rep_runtime.build_features(
                    bars=bars.copy(), drop_warmup_rows=False
                )
                state = bootstrap_compact_state(
                    asset=asset,
                    bars=bars,
                    authoritative_features=authority_features,
                    phase_c1_contract=c1,
                    feature_context_rows=CERT_ROWS,
                )
                ring = (
                    _normalize(state["feature_tail"])
                    .tail(CERT_ROWS)
                    .reset_index(drop=True)
                )
                build_ms = (time.perf_counter() - started) * 1000.0
                if len(ring) != CERT_ROWS:
                    raise CertificationError(
                        f"canonical ring rows={len(ring)}, expected {CERT_ROWS}"
                    )
                asset_builds.append(
                    {
                        "asset": asset,
                        "representative_edge_id": representative,
                        "rows": int(len(ring)),
                        "build_ms": build_ms,
                    }
                )
                print(
                    f"\n--- {asset} --- canonical ring rep={representative} "
                    f"rows={len(ring)} build_ms={build_ms:.1f}"
                )
            except Exception as exc:
                error = (
                    f"{asset}: canonical ring build failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                fatal_errors.append(error)
                print(error)
                continue

            for edge_id in asset_edges:
                edge_result: dict[str, Any] = {
                    "status": "FAIL",
                    "edge_id": edge_id,
                    "asset": asset,
                }
                try:
                    edge = manifest_edges[edge_id]
                    d1_edge = d1_edges[edge_id]
                    c3_edge = c3_edges[edge_id]
                    runtime = load_edge_runtime_from_manifest_entry(
                        edge, v2_root=root
                    )

                    projected_edge = dict(edge)
                    projected_edge["side_value"] = getattr(
                        runtime, "SIDE_VALUE", edge.get("side_value")
                    )
                    narrow = project_candidate_features(
                        ring=ring, edge=projected_edge, d1_edge=d1_edge
                    )
                    full = project_full_c3_features(
                        ring=ring,
                        edge=projected_edge,
                        d1_edge=d1_edge,
                        c3_edge=c3_edge,
                    )

                    candidate_identity = extract_candidate_source_identity(
                        v2_root=root, d1_edge=d1_edge
                    )
                    primary_identity = extract_primary_source_identity(
                        v2_root=root, d1_edge=d1_edge
                    )

                    c_sig = str(
                        ((d1_edge.get("candidate") or {}).get(
                            "candidate_logic_signature"
                        ))
                        or ""
                    )
                    p_sig = str(
                        ((d1_edge.get("primary_side") or {}).get(
                            "primary_logic_signature"
                        ))
                        or ""
                    )
                    c_descriptor = candidate_registry[c_sig]
                    p_descriptor = primary_registry[p_sig]

                    # 1) Full 6000-row raw-candidate parity uses each provider's
                    # own certified replay/normalization contract.  This prevents
                    # leading feature warmup NaNs from being misclassified as
                    # normal-path provider failures.
                    raw_authority = _source_raw_candidate(runtime, d1_edge, narrow)
                    raw_fast, raw_replay_meta = _provider_raw_replay(
                        d1_edge=d1_edge,
                        projection=narrow,
                        root=root,
                        create_candidate_provider=create_candidate_provider,
                    )
                    raw_ts = _timestamp_parity(raw_authority, raw_fast)
                    raw_identity = _identity_parity(
                        raw_authority, raw_fast, c3cert=c3cert,
                        columns=EVENT_IDENTITY_COLUMNS,
                    )
                    raw_context = _common_context_parity(
                        raw_authority, raw_fast, c3cert=c3cert
                    )
                    if not raw_ts["ok"]:
                        raise CertificationError(
                            f"raw candidate timestamp sequence mismatch: {raw_ts}"
                        )
                    if not raw_identity.get("ok"):
                        raise CertificationError(
                            f"raw candidate identity mismatch: {raw_identity}"
                        )

                    # 2) The unchanged runtime remains the accepted-event authority.
                    accepted_authority = runtime.generate_candidate_events(
                        features=narrow.copy()
                    )
                    if not isinstance(accepted_authority, pd.DataFrame):
                        raise CertificationError(
                            "runtime.generate_candidate_events returned non-DataFrame"
                        )
                    accepted_authority = _normalize(accepted_authority)
                    if accepted_authority.empty:
                        raise CertificationError(
                            "real 6000-row authority produced zero accepted events; "
                            "cannot certify model/decision bridge"
                        )

                    # 3) Restart from mature state, then exercise only update_one()
                    # over the tail.  This is the actual production continuation
                    # contract.  Runtime composition relabeling is applied exactly
                    # where the source runtime applies it.
                    composition = extract_runtime_composition_identity(
                        v2_root=root, d1_edge=d1_edge
                    )
                    cut = _restart_cut(
                        narrow, accepted_authority, raw_authority,
                        minimum_bootstrap_rows=1200,
                    )
                    restart_raw, restart_accepted, update_ms = _provider_restart_replay(
                        d1_edge=d1_edge,
                        projection=narrow,
                        cut=cut,
                        root=root,
                        composed_candidate_event_generator=(
                            composition.composed_candidate_event_generator
                        ),
                        create_candidate_provider=create_candidate_provider,
                        create_primary_side_provider=create_primary_side_provider,
                    )
                    all_fast_update_ms.extend(update_ms)

                    raw_tail = _tail(raw_authority, narrow, cut)
                    accepted_tail = _tail(accepted_authority, narrow, cut)
                    if accepted_tail.empty:
                        raise CertificationError(
                            "mature restart tail produced zero authority accepted events"
                        )

                    restart_raw_ts = _timestamp_parity(raw_tail, restart_raw)
                    restart_accepted_ts = _timestamp_parity(
                        accepted_tail, restart_accepted
                    )
                    restart_raw_identity = _identity_parity(
                        raw_tail, restart_raw, c3cert=c3cert,
                        columns=EVENT_IDENTITY_COLUMNS,
                    )
                    restart_accepted_identity = _identity_parity(
                        accepted_tail, restart_accepted, c3cert=c3cert,
                        columns=PRIMARY_IDENTITY_COLUMNS,
                    )
                    restart_raw_context = _common_context_parity(
                        raw_tail, restart_raw, c3cert=c3cert
                    )
                    restart_accepted_context = _common_context_parity(
                        accepted_tail, restart_accepted, c3cert=c3cert
                    )

                    if not restart_raw_ts["ok"]:
                        raise CertificationError(
                            f"restart raw timestamp parity failed: {restart_raw_ts}"
                        )
                    if not restart_accepted_ts["ok"]:
                        raise CertificationError(
                            "restart accepted timestamp parity failed: "
                            f"{restart_accepted_ts}"
                        )
                    if not restart_raw_identity.get("ok"):
                        raise CertificationError(
                            f"restart raw identity parity failed: {restart_raw_identity}"
                        )
                    if not restart_accepted_identity.get("ok"):
                        raise CertificationError(
                            "restart accepted identity parity failed: "
                            f"{restart_accepted_identity}"
                        )

                    # Full descriptive event-context parity is retained as a
                    # diagnostic, not a release gate.  The frozen Phase D release
                    # contract gates candidate timestamps, accepted timestamps,
                    # exact selected model rows, score/decision outputs and restart.
                    # D.3 already guarantees selected model features are owned by
                    # the canonical feature table, not duplicate event context.
                    selected_features = [
                        str(x) for x in c3_edge.get("selected_features") or []
                    ]
                    if not selected_features:
                        raise CertificationError("C.3 selected feature list is empty")

                    model_bridge, auth_rows, fast_rows = _model_row_bridge(
                        runtime=runtime,
                        full_features=full,
                        authority_events=accepted_tail,
                        provider_events=restart_accepted,
                        selected_features=selected_features,
                        strip_selected_feature_collisions=(
                            strip_selected_feature_collisions
                        ),
                        c3cert=c3cert,
                    )
                    if model_bridge.get("status") != "PASS":
                        raise CertificationError(
                            f"selected model-row bridge mismatch: {model_bridge}"
                        )

                    score_bridge = _score_bridge(
                        runtime=runtime,
                        authority_rows=auth_rows,
                        provider_rows=fast_rows,
                        c3cert=c3cert,
                    )
                    if score_bridge.get("status") != "PASS":
                        raise CertificationError(
                            f"score/decision bridge mismatch: {score_bridge}"
                        )

                    no_io = _no_io_probe(
                        d1_edge=d1_edge,
                        projection=narrow,
                        raw_authority=raw_authority,
                        root=root,
                        create_candidate_provider=create_candidate_provider,
                        create_primary_side_provider=create_primary_side_provider,
                    )

                    edge_result = {
                        "status": "PASS",
                        "edge_id": edge_id,
                        "asset": asset,
                        "candidate_logic_signature": c_sig,
                        "primary_logic_signature": p_sig,
                        "candidate_provider": {
                            "provider_id": c_descriptor.provider_id,
                            "provider_version": c_descriptor.provider_version,
                            "module_name": c_descriptor.module_name,
                        },
                        "primary_provider": {
                            "provider_id": p_descriptor.provider_id,
                            "provider_version": p_descriptor.provider_version,
                            "module_name": p_descriptor.module_name,
                        },
                        "candidate_source_identity": {
                            "edge_id": candidate_identity.edge_id,
                            "side_value": candidate_identity.side_value,
                            "candidate_event_generator": (
                                candidate_identity.candidate_event_generator
                            ),
                            "candidate_source_path": (
                                candidate_identity.candidate_source_path
                            ),
                        },
                        "primary_source_identity": {
                            "edge_id": primary_identity.edge_id,
                            "side_value": primary_identity.side_value,
                            "primary_source_path": (
                                primary_identity.primary_source_path
                            ),
                        },
                        "raw_candidate_full_history": {
                            "authority_rows": int(len(raw_authority)),
                            "provider_rows": int(len(raw_fast)),
                            "timestamp_parity": raw_ts,
                            "identity_parity": raw_identity,
                            "common_context_parity_diagnostic_only": raw_context,
                            "provider_replay_meta": raw_replay_meta,
                        },
                        "primary_accepted_full_authority": {
                            "authority_rows": int(len(accepted_authority)),
                            "note": (
                                "provider accepted events are release-gated on the "
                                "mature restart tail below; full descriptive context "
                                "is not a frozen release dimension"
                            ),
                        },
                        "selected_model_row_bridge": model_bridge,
                        "score_decision_bridge": score_bridge,
                        "restart": {
                            "status": "PASS",
                            "cut_index": int(cut),
                            "bootstrap_rows": int(cut),
                            "tail_rows": int(len(narrow) - cut),
                            "raw_authority_events": int(len(raw_tail)),
                            "raw_provider_events": int(len(restart_raw)),
                            "accepted_authority_events": int(len(accepted_tail)),
                            "accepted_provider_events": int(len(restart_accepted)),
                            "raw_timestamp_parity": restart_raw_ts,
                            "accepted_timestamp_parity": restart_accepted_ts,
                            "raw_identity_parity": restart_raw_identity,
                            "accepted_identity_parity": restart_accepted_identity,
                            "raw_common_context_parity_diagnostic_only": restart_raw_context,
                            "accepted_common_context_parity_diagnostic_only": (
                                restart_accepted_context
                            ),
                            "selected_model_row_status": model_bridge.get("status"),
                            "score_decision_status": score_bridge.get("status"),
                        },
                        "normal_update_no_file_io": no_io,
                        "fast_update_timing": _summary(update_ms),
                    }
                    results[edge_id] = edge_result
                    print(
                        f"{edge_id:<52} PASS "
                        f"raw_full={len(raw_fast):>4} "
                        f"accepted_tail={len(restart_accepted):>4} "
                        f"model_rows_tail={len(fast_rows):>4} "
                        f"fast_p95_ms="
                        f"{edge_result['fast_update_timing'].get('p95_ms', 0.0):.4f}"
                    )
                except Exception as exc:
                    edge_result["error"] = f"{type(exc).__name__}: {exc}"
                    results[edge_id] = edge_result
                    fatal_errors.append(
                        f"{edge_id}: {type(exc).__name__}: {exc}"
                    )
                    print(
                        f"{edge_id:<52} FAIL "
                        f"{type(exc).__name__}: {exc}"
                    )

        sandbox_meta = {
            "enabled": True,
            "seeded_file_count": int(sandbox.get("seeded_file_count") or 0),
            "production_continuation_mutated": False,
        }

    passed = sorted(
        edge_id
        for edge_id, row in results.items()
        if row.get("status") == "PASS"
    )
    failed = sorted(
        edge_id
        for edge_id in manifest_edges
        if results.get(edge_id, {}).get("status") != "PASS"
    )

    status = (
        "PASS"
        if not fatal_errors
        and not failed
        and selected_count > 0
        and len(passed) == selected_count
        else "FAIL"
    )

    report = {
        "schema_version": CERT_SCHEMA_VERSION,
        "status": status,
        "created_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "production_mutated": False,
        "production_activation_changed": False,
        "provider_overlay_wiring_allowed": status == "PASS",
        "phase_d_closed": False,
        "next_gate": (
            "D4_PROVIDER_OVERLAY_WIRING"
            if status == "PASS"
            else "REPAIR_PROVIDER_CERTIFICATION_FAILURES"
        ),
        "authority_identity_mode": (
            "structural_current_state; git SHA recorded as provenance only"
        ),
        "git_head_provenance_only": _git_head(repo_root),
        "authority_rows": CERT_ROWS,
        "tolerance": {"atol": float(args.atol), "rtol": float(args.rtol)},
        "selected_edge_count": selected_count,
        "edges_checked": len(results),
        "edges_passed": len(passed),
        "edges_failed": failed,
        "selected_assets": assets,
        "capability_summary": caps,
        "candidate_provider_signature_count": len(candidate_registry),
        "primary_provider_signature_count": len(primary_registry),
        "source_guard": source,
        "existing_d3_bridge": {
            "status": "PASS" if d3_chain_ok else "FAIL",
            "d3_static_contract_status": static_d3.get("status"),
            "d3_activation_validation_status": (
                activation_validation.get("status")
            ),
            "d3_certification_status": d3_cert.get("status"),
            "d3_certification_file_sha256": _sha256(paths["d3_cert"]),
            "d3_activation_file_sha256": _sha256(paths["d3_activation"]),
            "c5_model_decision_parity_status": (
                (d3_cert.get("c5_model_decision_parity") or {}).get("status")
            ),
            "d2_candidate_authority_status": (
                (d3_cert.get("d2_candidate_authority") or {}).get("status")
            ),
            "downstream_policy": (
                "unchanged active D.3 collision sanitization + deferred exact C.3 "
                "projection + existing runtime.score/shared EdgeScorer"
            ),
        },
        "harness_negative_controls": harness_negative_controls,
        "harness_semantics": {
            "raw_candidate_full_history": "provider.replay uses provider-certified normalization",
            "normal_update_start": "mature restart tail after >=1200 bootstrap rows",
            "runtime_candidate_generator_relabel": "applied from RuntimeCompositionIdentity before accepted-event comparison",
            "gating_dimensions": [
                "candidate_timestamp_sequence",
                "event_identity",
                "primary_accepted_timestamp_sequence",
                "selected_model_row",
                "raw_probability",
                "calibrated_probability",
                "afml_bet_size",
                "selected_threshold",
                "approved_signal",
                "signal_id",
                "restart",
                "normal_update_no_file_io",
            ],
            "descriptive_event_context": "diagnostic_only_non_model_owning_after_d3_collision_sanitization",
        },
        "dependency_authority_sandbox": sandbox_meta,
        "asset_canonical_ring_builds": asset_builds,
        "fast_provider_update_timing_all_edges_all_bars": _summary(
            all_fast_update_ms
        ),
        "edges": results,
        "fatal_errors": fatal_errors,
        "certification_statement": (
            "PASS proves every selected edge's incremental candidate provider "
            "matches the original candidate-source timestamp sequence and event "
            "identity over the exact 6000-row real canonical authority. It also "
            "proves a mature bootstrap/restart followed by normal one-row updates "
            "matches the current runtime's accepted-event timestamp/identity "
            "sequence; after the exact active D.3 selected-feature collision "
            "policy, every selected live model row is numerically identical; all "
            "mature accepted rows match raw_probability, calibrated_probability, "
            "AFML bet size, threshold, approved_signal and signal_id; and provider "
            "normal updates perform no file I/O. Descriptive event-context fields "
            "that are not frozen release dimensions remain recorded as diagnostics "
            "and cannot override canonical selected model inputs."
        ),
    }

    output.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    print()
    print("=" * 118)
    print("D.4 CONSOLIDATED REAL-HISTORY CERTIFICATION SUMMARY")
    print("=" * 118)
    print("Status                    :", status)
    print("Edges checked             :", len(results))
    print("Edges passed              :", len(passed))
    print("Edges failed              :", len(failed))
    print(
        "FAST capability           :",
        f"{caps.get('fast_continuation')} / {caps.get('selected_edges')}",
    )
    print("Unsupported trading       :", caps.get("unsupported_trading_edges"))
    timing = report["fast_provider_update_timing_all_edges_all_bars"]
    print("Fast provider update p50  :", timing.get("p50_ms"), "ms")
    print("Fast provider update p95  :", timing.get("p95_ms"), "ms")
    print("Fast provider update p99  :", timing.get("p99_ms"), "ms")
    print("Production changed        : NO")
    print("Provider overlay allowed  :", status == "PASS")
    print("Phase D closed            : NO")
    print("Report                    :", output)
    if failed:
        print("Failed edges:")
        for edge_id in failed:
            print(" -", edge_id)
    if fatal_errors:
        print("Fatal errors:")
        for item in fatal_errors[:30]:
            print(" -", item)

    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
