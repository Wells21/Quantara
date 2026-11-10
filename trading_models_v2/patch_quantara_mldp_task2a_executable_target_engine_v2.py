#!/usr/bin/env python3
r"""Quantara MLDP transformation patcher - Task 2A.

Installs the shared pre-model exact Bid/Ask executable-outcome audit station.

Run from trading_models_v2:
    python .\patch_quantara_mldp_task2a_executable_target_engine_v1.py --dry-run
    python .\patch_quantara_mldp_task2a_executable_target_engine_v1.py

This patcher performs NO git add/commit/push/merge/ref operation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime, timezone

PATCH_ID = "quantara_mldp_task2a_executable_target_engine_v1"
PATCHER_REVISION = "v2_git_blob_preflight_fix"
EXPECTED_BRANCH = "v3"

# Exact Task-1/current-v3 prerequisites audited before this patch was built.
# IMPORTANT: the 40-hex values below are Git blob SHA-1 identities from the
# GitHub Contents API, not ordinary file SHA-256 digests. The preflight must
# therefore compare them with `git hash-object`, while SHA-256 remains the
# provenance digest used for generated Quantara artifacts/manifests.
EXPECTED_EXECUTION_SHA = "a911b1a94fa6abb5a9e0a528b145c7f99d227be2"
EXPECTED_ONTOLOGY_SHA = "7095bbb81a81809539b0d80c822ed4d1bce26b4a"
EXPECTED_REGISTRY_SHA = "a62bc9a292462197cbb805c6c2d1491ca2a58a92"
EXPECTED_STATE_SHA = "b27d1cc8925bca25b5e5490e8de2da0b2ad354fd"

NEW_FILES = {'shared/execution/executable_target_dataset.py': '"""Quantara MLDP Task 2A: pre-model exact Bid/Ask executable-outcome engine.\n\nThis is one shared research station for every compatible Quantara edge.\n\nAFML Chapter 3 and MLAM Chapter 5 motivate path-dependent financial labels and\nmeta-labeling when a primary rule/model supplies side. Quantara\'s exact\nhistorical Bid/Ask replay is a production extension, not a canonical AFML/MLAM\nroutine. Task 2A reuses Quantara\'s existing resolver before secondary-model\ntraining, without duplicating its execution mathematics.\n\nTask 2A is AUDIT-ONLY. It does not authorize Step 01-09 to train on the new\ntarget. Unresolved/no-timely-entry observations are never silently coerced to\nlosses or label 0: AFML/MLAM do not define Quantara\'s quote-feed/data-gap/no-fill\nsemantics, so that population policy must be audited and frozen in Task 2B.\n"""\nfrom __future__ import annotations\n\nfrom dataclasses import dataclass\nfrom pathlib import Path\nfrom typing import Any\nimport hashlib\nimport json\nimport time\n\nimport numpy as np\nimport pandas as pd\n\nfrom shared.edges.edge_layout import get_edge_layout_from_edge_root\nfrom shared.edges.edge_yaml_contract import validate_edge_yaml_file\nfrom shared.edges.trade_geometry import load_trade_geometry\nfrom shared.execution import oof_exact_execution as exact\nfrom shared.labels.outcome_ontology import (\n    ONTOLOGY_VERSION,\n    canonicalize_legacy_outcomes,\n    label_from_r,\n    validate_layer,\n)\n\nENGINE_VERSION = "quantara_mldp_task2a_executable_target_engine_v1"\nTARGET_LAYER = "quote_executable"\nEXPECTED_EXECUTION_CONTRACT_VERSION = "v3_oof_exact_bidask_economic_label_v2"\nTRAINING_AUTHORITY = "AUDIT_ONLY_TASK2A_NOT_YET_AUTHORIZED"\n\nTIME_COL = "Datetime"\nEVENT_VOL_COL = "Event_Vol"\nSIDE_COL = "side"\nSOURCE_ROW_COL = "source_event_row_number"\n\n\nclass ExecutableTargetEngineError(RuntimeError):\n    """Fail-closed Task-2A executable-target engine error."""\n\n\n@dataclass(frozen=True)\nclass TargetArtifactPaths:\n    resolution_csv: Path\n    manifest_json: Path\n\n\ndef sha256_file(path: Path) -> str:\n    h = hashlib.sha256()\n    with Path(path).open("rb") as handle:\n        for block in iter(lambda: handle.read(1024 * 1024), b""):\n            h.update(block)\n    return h.hexdigest()\n\n\ndef _rel(path: Path, root: Path) -> str:\n    try:\n        return str(path.resolve().relative_to(root.resolve())).replace("\\\\", "/")\n    except ValueError:\n        return str(path.resolve()).replace("\\\\", "/")\n\n\ndef _model_rows_path(layout: Any) -> Path:\n    return layout.research_dataset_dir / f"{layout.edge_stem}_model_rows.csv"\n\n\ndef _model_rows_manifest_path(layout: Any) -> Path:\n    return layout.research_dataset_dir / "model_row_manifest.json"\n\n\ndef target_artifact_paths(layout: Any) -> TargetArtifactPaths:\n    return TargetArtifactPaths(\n        resolution_csv=layout.research_dataset_dir\n        / f"{layout.edge_stem}_mldp_execution_resolution.csv",\n        manifest_json=layout.research_dataset_dir\n        / f"{layout.edge_stem}_mldp_executable_target_manifest.json",\n    )\n\n\ndef discover_edge_roots(\n    v2_root: str | Path,\n    *,\n    asset: str | None = None,\n    require_model_rows: bool = False,\n) -> list[Path]:\n    """Discover side-specialized packages; no asset/edge list is hard-coded."""\n    root = Path(v2_root).resolve()\n    assets_root = root / "assets"\n    if not assets_root.exists():\n        raise ExecutableTargetEngineError(f"Missing assets root: {assets_root}")\n\n    wanted = None if asset is None else str(asset).strip().upper()\n    out: list[Path] = []\n    for asset_dir in sorted(\n        (p for p in assets_root.iterdir() if p.is_dir()), key=lambda p: p.name\n    ):\n        if wanted is not None and asset_dir.name.upper() != wanted:\n            continue\n        edges_root = asset_dir / "edges"\n        if not edges_root.exists():\n            continue\n        for edge_root in sorted(\n            (p for p in edges_root.iterdir() if p.is_dir()), key=lambda p: p.name\n        ):\n            required = (\n                edge_root / "edge.yaml",\n                edge_root / "runtime.py",\n                edge_root / "model_rows.py",\n            )\n            if not all(p.exists() for p in required):\n                continue\n            if require_model_rows:\n                stem = f"{asset_dir.name.lower()}_{edge_root.name}"\n                model_rows = (\n                    edge_root\n                    / "artifacts"\n                    / "research_dataset"\n                    / f"{stem}_model_rows.csv"\n                )\n                if not model_rows.exists():\n                    continue\n            out.append(edge_root.resolve())\n    return out\n\n\ndef compatibility_report(edge_root: str | Path) -> dict[str, Any]:\n    layout = get_edge_layout_from_edge_root(edge_root)\n    paths = target_artifact_paths(layout)\n    checks = {\n        "edge_yaml": layout.edge_yaml.exists(),\n        "runtime_py": layout.runtime_py.exists(),\n        "model_rows_py": (layout.edge_root / "model_rows.py").exists(),\n        "gross_model_rows_csv": _model_rows_path(layout).exists(),\n        "gross_model_rows_manifest": _model_rows_manifest_path(layout).exists(),\n    }\n    return {\n        "edge_root": str(layout.edge_root),\n        "edge_id": layout.edge_id,\n        "asset": layout.asset,\n        "side": layout.side_name,\n        "compatible_for_resolution": bool(all(checks.values())),\n        "checks": checks,\n        "source_model_rows": _rel(_model_rows_path(layout), layout.v2_root),\n        "task2a_resolution_output": _rel(paths.resolution_csv, layout.v2_root),\n        "task2a_manifest_output": _rel(paths.manifest_json, layout.v2_root),\n    }\n\n\ndef _event_id(edge_id: str, t0: Any, side: int, geometry_hash: str) -> str:\n    ts = pd.Timestamp(t0)\n    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")\n    payload = f"{edge_id}|{int(ts.value)}|{int(side)}|{geometry_hash}".encode("utf-8")\n    return hashlib.sha256(payload).hexdigest()\n\n\ndef load_gross_model_rows(\n    edge_root: str | Path, geometry_hash: str\n) -> tuple[Any, pd.DataFrame, Path]:\n    """Load every gross model row; do not filter using future outcomes."""\n    layout = get_edge_layout_from_edge_root(edge_root)\n    path = _model_rows_path(layout)\n    if not path.exists():\n        raise ExecutableTargetEngineError(\n            f"{layout.edge_id}: missing gross model rows: {path}. "\n            "Task 2A does not regenerate upstream model rows."\n        )\n\n    frame = pd.read_csv(path)\n    if frame.empty:\n        raise ExecutableTargetEngineError(\n            f"{layout.edge_id}: gross model rows are empty: {path}"\n        )\n    if TIME_COL not in frame.columns:\n        frame = frame.rename(columns={frame.columns[0]: TIME_COL})\n\n    try:\n        frame = canonicalize_legacy_outcomes(frame, copy=True, strict=True)\n    except Exception as exc:\n        raise ExecutableTargetEngineError(\n            f"{layout.edge_id}: gross outcome canonicalization failed: {exc}"\n        ) from exc\n\n    if SIDE_COL not in frame.columns:\n        frame[SIDE_COL] = int(layout.side_value)\n\n    required = [\n        TIME_COL,\n        EVENT_VOL_COL,\n        SIDE_COL,\n        "gross_r",\n        "gross_meta_label",\n        "gross_t1",\n    ]\n    missing = [c for c in required if c not in frame.columns]\n    if missing:\n        raise ExecutableTargetEngineError(\n            f"{layout.edge_id}: source model rows missing Task-2A fields: {missing}"\n        )\n\n    frame[TIME_COL] = pd.to_datetime(frame[TIME_COL], utc=True, errors="coerce")\n    frame["gross_t1"] = pd.to_datetime(frame["gross_t1"], utc=True, errors="coerce")\n    for col in (EVENT_VOL_COL, SIDE_COL, "gross_r", "gross_meta_label"):\n        frame[col] = pd.to_numeric(frame[col], errors="coerce")\n\n    invalid = {c: int(frame[c].isna().sum()) for c in required if frame[c].isna().any()}\n    if invalid:\n        raise ExecutableTargetEngineError(\n            f"{layout.edge_id}: invalid required source values: {invalid}"\n        )\n    if bool(frame[TIME_COL].duplicated().any()):\n        raise ExecutableTargetEngineError(\n            f"{layout.edge_id}: event timestamps must be unique for one side-specialized edge."\n        )\n    if bool((frame[SIDE_COL].astype(int) != int(layout.side_value)).any()):\n        raise ExecutableTargetEngineError(\n            f"{layout.edge_id}: source rows violate the package side."\n        )\n    ev = frame[EVENT_VOL_COL].to_numpy(dtype="float64")\n    if not (np.isfinite(ev) & (ev > 0.0)).all():\n        raise ExecutableTargetEngineError(\n            f"{layout.edge_id}: Event_Vol must be finite and > 0."\n        )\n\n    try:\n        validate_layer(frame, "gross", t0_column=TIME_COL)\n    except Exception as exc:\n        raise ExecutableTargetEngineError(\n            f"{layout.edge_id}: invalid gross outcome layer: {exc}"\n        ) from exc\n\n    frame = frame.sort_values(TIME_COL).reset_index(drop=True)\n    frame[SOURCE_ROW_COL] = np.arange(len(frame), dtype="int64")\n    frame["event_id"] = [\n        _event_id(layout.edge_id, t0, int(side), geometry_hash)\n        for t0, side in zip(frame[TIME_COL], frame[SIDE_COL])\n    ]\n    frame["trgt"] = frame[EVENT_VOL_COL].astype("float64")\n    frame["geometry_hash"] = str(geometry_hash)\n    return layout, frame, path\n\n\ndef assemble_executable_outcome_frame(\n    gross_frame: pd.DataFrame,\n    execution_frame: pd.DataFrame,\n) -> pd.DataFrame:\n    """Attach execution results without inventing outcomes for unresolved rows."""\n    gross = canonicalize_legacy_outcomes(gross_frame, copy=True, strict=True)\n    if SOURCE_ROW_COL not in gross.columns:\n        gross[SOURCE_ROW_COL] = np.arange(len(gross), dtype="int64")\n    if "source_oof_row_number" not in execution_frame.columns:\n        raise ExecutableTargetEngineError(\n            "Execution frame missing source_oof_row_number identity."\n        )\n\n    execution = execution_frame.copy()\n    execution["source_oof_row_number"] = pd.to_numeric(\n        execution["source_oof_row_number"], errors="coerce"\n    )\n    if execution["source_oof_row_number"].isna().any():\n        raise ExecutableTargetEngineError("Execution source identity contains NaN.")\n    execution["source_oof_row_number"] = execution["source_oof_row_number"].astype(\n        "int64"\n    )\n    if execution["source_oof_row_number"].duplicated().any():\n        raise ExecutableTargetEngineError("Execution source identity is duplicated.")\n\n    expected = set(gross[SOURCE_ROW_COL].astype(int).tolist())\n    actual = set(execution["source_oof_row_number"].astype(int).tolist())\n    if expected != actual:\n        raise ExecutableTargetEngineError(\n            f"Gross/execution row identity mismatch: "\n            f"missing={sorted(expected-actual)[:10]} extra={sorted(actual-expected)[:10]}"\n        )\n\n    execution = execution.set_index("source_oof_row_number").reindex(\n        gross[SOURCE_ROW_COL].astype(int)\n    )\n    execution.index = gross.index\n\n    out = gross.copy()\n    protected = {\n        TIME_COL,\n        SIDE_COL,\n        EVENT_VOL_COL,\n        "realized_r",\n        "meta_label",\n        "t1",\n        "gross_r",\n        "gross_meta_label",\n        "gross_t1",\n        SOURCE_ROW_COL,\n        "event_id",\n        "trgt",\n        "geometry_hash",\n    }\n    for col in execution.columns:\n        if col not in protected:\n            out[col] = execution[col].to_numpy()\n\n    if "execution_status" not in out.columns:\n        raise ExecutableTargetEngineError("Execution results lack execution_status.")\n    resolved = out["execution_status"].astype(str).eq("resolved")\n\n    executed_r = pd.to_numeric(\n        out["executed_r"]\n        if "executed_r" in out.columns\n        else pd.Series(np.nan, index=out.index),\n        errors="coerce",\n    )\n    exit_time = pd.to_datetime(\n        out["exit_time"]\n        if "exit_time" in out.columns\n        else pd.Series(pd.NaT, index=out.index),\n        utc=True,\n        errors="coerce",\n    )\n\n    out["quote_executable_r"] = np.where(resolved, executed_r, np.nan)\n    out["quote_executable_t1"] = exit_time.where(resolved, pd.NaT)\n    labels = pd.Series(\n        pd.array([pd.NA] * len(out), dtype="Int64"), index=out.index\n    )\n\n    if bool(resolved.any()):\n        resolved_r = pd.to_numeric(\n            out.loc[resolved, "quote_executable_r"], errors="coerce"\n        )\n        if resolved_r.isna().any() or not np.isfinite(\n            resolved_r.to_numpy(dtype="float64")\n        ).all():\n            raise ExecutableTargetEngineError(\n                "Resolved rows contain invalid quote_executable_r."\n            )\n        if out.loc[resolved, "quote_executable_t1"].isna().any():\n            raise ExecutableTargetEngineError(\n                "Resolved rows contain invalid quote_executable_t1."\n            )\n        labels.loc[resolved] = label_from_r(resolved_r.to_numpy(dtype="float64"))\n\n    out["quote_executable_meta_label"] = labels\n    out["mldp_target_eligible"] = resolved.astype(bool)\n\n    gross_label = pd.to_numeric(\n        out["gross_meta_label"], errors="coerce"\n    ).astype("Int64")\n    out["gross_to_quote_label_flip"] = (\n        resolved\n        & out["quote_executable_meta_label"].notna()\n        & (gross_label != out["quote_executable_meta_label"])\n    )\n\n    if bool(resolved.any()):\n        try:\n            validate_layer(\n                out.loc[resolved].copy(),\n                "quote_executable",\n                t0_column=TIME_COL,\n            )\n        except Exception as exc:\n            raise ExecutableTargetEngineError(\n                f"Resolved quote-executable layer is invalid: {exc}"\n            ) from exc\n\n    out["mldp_training_authority"] = TRAINING_AUTHORITY\n    return out\n\n\ndef _catalog_provenance(\n    catalog: list[dict[str, Any]],\n    *,\n    v2_root: Path,\n    hash_tick_contents: bool,\n) -> tuple[list[dict[str, Any]], str]:\n    records: list[dict[str, Any]] = []\n    for rec in catalog:\n        path = Path(rec["path"]).resolve()\n        stat = path.stat()\n        item: dict[str, Any] = {\n            "path": _rel(path, v2_root),\n            "size_bytes": int(stat.st_size),\n            "mtime_ns": int(stat.st_mtime_ns),\n            "end_date": str(rec.get("end_date", "")),\n        }\n        if hash_tick_contents:\n            item["sha256"] = sha256_file(path)\n        records.append(item)\n    blob = json.dumps(\n        records, sort_keys=True, separators=(",", ":")\n    ).encode("utf-8")\n    return records, hashlib.sha256(blob).hexdigest()\n\n\ndef _numeric_summary(series: pd.Series) -> dict[str, float | int | None]:\n    x = pd.to_numeric(series, errors="coerce").dropna().to_numpy(dtype="float64")\n    if not len(x):\n        return {\n            "count": 0,\n            "mean": None,\n            "median": None,\n            "positive_rate": None,\n        }\n    return {\n        "count": int(len(x)),\n        "mean": float(np.mean(x)),\n        "median": float(np.median(x)),\n        "positive_rate": float(np.mean(x > 0.0)),\n    }\n\n\ndef resolve_edge_executable_targets(\n    edge_root: str | Path,\n    *,\n    write: bool = True,\n    strict_coverage: bool = False,\n    hash_tick_contents: bool = False,\n) -> dict[str, Any]:\n    """Resolve pre-model exact Bid/Ask outcomes for one arbitrary edge."""\n    if exact.EXECUTION_CONTRACT_VERSION != EXPECTED_EXECUTION_CONTRACT_VERSION:\n        raise ExecutableTargetEngineError(\n            "Exact execution contract changed; Task 2A must be re-audited. "\n            f"Found {exact.EXECUTION_CONTRACT_VERSION!r}."\n        )\n\n    layout = get_edge_layout_from_edge_root(edge_root)\n    validate_edge_yaml_file(layout.edge_root)\n    geometry = load_trade_geometry(\n        layout.edge_yaml,\n        expected_edge_id=layout.edge_id,\n        expected_side_name=layout.side_name,\n    )\n    layout, gross, model_rows_path = load_gross_model_rows(\n        layout.edge_root, str(geometry["geometry_hash"])\n    )\n\n    runtime, model_rows_module = exact._import_edge_modules(layout.edge_root)\n    max_signal_age_seconds = exact._effective_live_max_signal_age_seconds(\n        layout.v2_root\n    )\n    bars_dt, bars_close = exact._load_bars(runtime)\n    catalog = exact._build_tick_catalog(model_rows_module)\n\n    print("=" * 100, flush=True)\n    print(\n        f"MLDP TASK 2A - PRE-MODEL EXACT EXECUTION AUDIT - {layout.edge_id}",\n        flush=True,\n    )\n    print("=" * 100, flush=True)\n    print(f"Gross events : {len(gross):,}", flush=True)\n    print(\n        f"Geometry     : Hv={geometry[\'vertical_horizon_bars\']} "\n        f"PT={geometry[\'pt_multiplier\']:g} SL={geometry[\'sl_multiplier\']:g}",\n        flush=True,\n    )\n    print(f"Signal age   : {max_signal_age_seconds}s", flush=True)\n    print(\n        f"Target layer : {TARGET_LAYER} (AUDIT ONLY; training not authorized)",\n        flush=True,\n    )\n\n    resolved_rows: list[dict[str, Any]] = []\n    started = time.time()\n    for i, (_, source) in enumerate(gross.iterrows(), start=1):\n        resolver_row = source.copy()\n        resolver_row["source_oof_row_number"] = int(source[SOURCE_ROW_COL])\n        resolved_rows.append(\n            exact._resolve_one(\n                resolver_row,\n                bars_dt=bars_dt,\n                bars_close=bars_close,\n                catalog=catalog,\n                geometry=geometry,\n                max_signal_age_seconds=max_signal_age_seconds,\n            )\n        )\n        if i % 250 == 0 or i == len(gross):\n            print(\n                f"  {i:,}/{len(gross):,} | elapsed={time.time()-started:.1f}s",\n                flush=True,\n            )\n\n    execution = pd.DataFrame(resolved_rows)\n    combined = assemble_executable_outcome_frame(gross, execution)\n    resolved = combined["mldp_target_eligible"].astype(bool)\n    total = int(len(combined))\n    resolved_count = int(resolved.sum())\n    unresolved_count = total - resolved_count\n    status_counts = (\n        combined["execution_status"]\n        .astype(str)\n        .value_counts(dropna=False)\n        .to_dict()\n    )\n\n    paths = target_artifact_paths(layout)\n    tick_records, tick_catalog_digest = _catalog_provenance(\n        catalog,\n        v2_root=layout.v2_root,\n        hash_tick_contents=hash_tick_contents,\n    )\n\n    quote_r = (\n        combined.loc[resolved, "quote_executable_r"]\n        if resolved_count\n        else pd.Series(dtype=float)\n    )\n    flip_count = (\n        int(combined.loc[resolved, "gross_to_quote_label_flip"].sum())\n        if resolved_count\n        else 0\n    )\n\n    resolver_path = Path(exact.__file__).resolve()\n    import shared.labels.outcome_ontology as ontology_module\n    ontology_path = Path(ontology_module.__file__).resolve()\n    live_settings_path = layout.v2_root / "portfolio_live" / "live_settings.py"\n\n    manifest: dict[str, Any] = {\n        "schema_version": ENGINE_VERSION,\n        "created_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),\n        "task": "TASK2A_PRE_MODEL_EXECUTABLE_TARGET_AUDIT",\n        "training_authority": TRAINING_AUTHORITY,\n        "task2b_may_consume_for_training": False,\n        "edge_id": layout.edge_id,\n        "asset": layout.asset,\n        "family": layout.family,\n        "side_name": layout.side_name,\n        "side_value": int(layout.side_value),\n        "target_layer_candidate": TARGET_LAYER,\n        "outcome_ontology_version": ONTOLOGY_VERSION,\n        "execution_contract_version": exact.EXECUTION_CONTRACT_VERSION,\n        "geometry_hash": str(geometry["geometry_hash"]),\n        "trade_geometry": geometry,\n        "live_signal_age_max_seconds": int(max_signal_age_seconds),\n        "event_count": total,\n        "resolved_count": resolved_count,\n        "unresolved_count": int(unresolved_count),\n        "coverage_rate": float(resolved_count / total) if total else 0.0,\n        "execution_status_counts": {\n            str(k): int(v) for k, v in status_counts.items()\n        },\n        "gross_r_summary": _numeric_summary(combined["gross_r"]),\n        "quote_executable_r_summary_resolved_only": _numeric_summary(quote_r),\n        "gross_to_quote_label_flip_count_resolved_only": flip_count,\n        "gross_to_quote_label_flip_rate_resolved_only": (\n            float(flip_count / resolved_count) if resolved_count else None\n        ),\n        "unresolved_target_policy": (\n            "NO_LABEL_ASSIGNED. Task 2A does not coerce stale/no-entry/"\n            "data-gap/right-censored rows to 0 or to a loss. AFML/MLAM do "\n            "not define these Quantara execution/data semantics; Task 2B "\n            "must freeze the universal population rule first."\n        ),\n        "source_model_rows": _rel(model_rows_path, layout.v2_root),\n        "source_model_rows_sha256": sha256_file(model_rows_path),\n        "source_model_rows_manifest": _rel(\n            _model_rows_manifest_path(layout), layout.v2_root\n        ),\n        "source_model_rows_manifest_sha256": (\n            sha256_file(_model_rows_manifest_path(layout))\n            if _model_rows_manifest_path(layout).exists()\n            else None\n        ),\n        "edge_yaml_sha256": sha256_file(layout.edge_yaml),\n        "exact_execution_resolver": _rel(resolver_path, layout.v2_root),\n        "exact_execution_resolver_sha256": sha256_file(resolver_path),\n        "outcome_ontology_sha256": sha256_file(ontology_path),\n        "live_settings_sha256": sha256_file(live_settings_path),\n        "tick_provenance_level": (\n            "sha256_content"\n            if hash_tick_contents\n            else "metadata_signature_only"\n        ),\n        "tick_catalog_metadata_digest": tick_catalog_digest,\n        "tick_catalog": tick_records,\n        "execution_semantics": {\n            "entry": (\n                "first valid quote strictly after event; LONG Ask / SHORT Bid"\n            ),\n            "horizontal_distance": (\n                "signal_mid * Event_Vol * multiplier frozen at event, then "\n                "PT/SL levels re-anchored around executable entry"\n            ),\n            "exit": (\n                "LONG Bid / SHORT Ask; first PT/SL touch before vertical "\n                "deadline; vertical uses first valid quote strictly after deadline"\n            ),\n            "spread_embedded": True,\n        },\n        "outputs": {\n            "resolution_csv": _rel(paths.resolution_csv, layout.v2_root),\n            "manifest_json": _rel(paths.manifest_json, layout.v2_root),\n        },\n    }\n\n    if write:\n        paths.resolution_csv.parent.mkdir(parents=True, exist_ok=True)\n        tmp_csv = paths.resolution_csv.with_suffix(\n            paths.resolution_csv.suffix + ".tmp"\n        )\n        tmp_json = paths.manifest_json.with_suffix(\n            paths.manifest_json.suffix + ".tmp"\n        )\n        combined.to_csv(tmp_csv, index=False)\n        manifest["resolution_csv_sha256"] = sha256_file(tmp_csv)\n        tmp_json.write_text(\n            json.dumps(\n                manifest, indent=2, sort_keys=True, default=str\n            )\n            + "\\n",\n            encoding="utf-8",\n        )\n        tmp_csv.replace(paths.resolution_csv)\n        tmp_json.replace(paths.manifest_json)\n\n    print("", flush=True)\n    print(\n        f"Resolution coverage: {resolved_count:,}/{total:,} "\n        f"({manifest[\'coverage_rate\']:.6f})",\n        flush=True,\n    )\n    print(f"Statuses: {manifest[\'execution_status_counts\']}", flush=True)\n    qsum = manifest["quote_executable_r_summary_resolved_only"]\n    if qsum["count"]:\n        print(\n            f"Resolved quote-executable R: mean={qsum[\'mean\']:+.6f} "\n            f"median={qsum[\'median\']:+.6f} "\n            f"positive_rate={qsum[\'positive_rate\']:.4f}",\n            flush=True,\n        )\n    print(f"Training authority: {TRAINING_AUTHORITY}", flush=True)\n    if write:\n        print(f"Saved: {paths.resolution_csv}", flush=True)\n        print(f"Saved: {paths.manifest_json}", flush=True)\n\n    if strict_coverage and unresolved_count:\n        raise ExecutableTargetEngineError(\n            f"{layout.edge_id}: strict coverage requested but "\n            f"{unresolved_count} events are unresolved."\n        )\n    return manifest\n', 'build_mldp_executable_targets.py': '#!/usr/bin/env python3\n"""CLI for Quantara MLDP Task 2A executable-outcome audits."""\nfrom __future__ import annotations\n\nimport argparse\nfrom pathlib import Path\nimport sys\n\n_THIS = Path(__file__).resolve()\nV2_ROOT = _THIS.parent\nif str(V2_ROOT) not in sys.path:\n    sys.path.insert(0, str(V2_ROOT))\n\nfrom shared.execution.executable_target_dataset import (\n    ExecutableTargetEngineError,\n    compatibility_report,\n    discover_edge_roots,\n    resolve_edge_executable_targets,\n)\n\n\ndef _parse_args() -> argparse.Namespace:\n    p = argparse.ArgumentParser(\n        description=(\n            "Quantara MLDP Task 2A: exact Bid/Ask outcomes before model "\n            "training. Task 2A is audit-only and does not migrate Step 01-09."\n        )\n    )\n    g = p.add_mutually_exclusive_group(required=True)\n    g.add_argument(\n        "--edge-root",\n        help="Path to assets/<ASSET>/edges/<family>_<side>.",\n    )\n    g.add_argument(\n        "--asset",\n        help="Audit/resolve every discovered edge for one asset.",\n    )\n    g.add_argument(\n        "--all",\n        action="store_true",\n        help="Audit/resolve every discovered edge.",\n    )\n    p.add_argument(\n        "--dry-run",\n        action="store_true",\n        help=(\n            "Only discover packages and verify source/artifact presence; "\n            "no tick replay."\n        ),\n    )\n    p.add_argument(\n        "--strict-coverage",\n        action="store_true",\n        help="After diagnostics, fail if any source event is unresolved.",\n    )\n    p.add_argument(\n        "--hash-ticks",\n        action="store_true",\n        help="SHA-256 every tick file (can be slow on large histories).",\n    )\n    p.add_argument(\n        "--no-write",\n        action="store_true",\n        help="Resolve in memory only.",\n    )\n    return p.parse_args()\n\n\ndef _selected_roots(args: argparse.Namespace) -> list[Path]:\n    if args.edge_root:\n        return [Path(args.edge_root).resolve()]\n    return discover_edge_roots(\n        V2_ROOT, asset=args.asset if args.asset else None\n    )\n\n\ndef main() -> None:\n    args = _parse_args()\n    roots = _selected_roots(args)\n    if not roots:\n        raise ExecutableTargetEngineError(\n            "No compatible edge package directories were discovered."\n        )\n\n    pairs = [(root, compatibility_report(root)) for root in roots]\n    assets = sorted({report["asset"] for _, report in pairs})\n    ready = [\n        (root, report)\n        for root, report in pairs\n        if report["compatible_for_resolution"]\n    ]\n    blocked = [\n        (root, report)\n        for root, report in pairs\n        if not report["compatible_for_resolution"]\n    ]\n\n    print("=" * 100)\n    print("QUANTARA MLDP TASK 2A - SHARED EXECUTABLE-TARGET STATION")\n    print("=" * 100)\n    print(f"Discovered edge packages : {len(pairs)}")\n    print(f"Assets                   : {len(assets)} ({\', \'.join(assets)})")\n    print(f"Ready model-row packages : {len(ready)}")\n    print(f"Blocked/no artifacts      : {len(blocked)}")\n    print(\n        "Training authority        : AUDIT ONLY - Step 01-09 migration is NOT active"\n    )\n\n    if blocked:\n        print("\\nBlocked packages:")\n        for _, report in blocked:\n            failed = [\n                k for k, ok in report["checks"].items() if not ok\n            ]\n            print(f"  - {report[\'edge_id\']}: {\', \'.join(failed)}")\n\n    if args.dry_run:\n        print("\\nDRY RUN COMPLETE - no tick replay and no files written.")\n        return\n\n    if not ready:\n        raise ExecutableTargetEngineError(\n            "No selected edge has the existing gross model-row artifacts "\n            "required for Task 2A."\n        )\n\n    failures: list[str] = []\n    for n, (edge_root, report) in enumerate(ready, start=1):\n        print(f"\\n[{n}/{len(ready)}] {report[\'edge_id\']}")\n        try:\n            resolve_edge_executable_targets(\n                edge_root,\n                write=not args.no_write,\n                strict_coverage=args.strict_coverage,\n                hash_tick_contents=args.hash_ticks,\n            )\n        except Exception as exc:\n            failures.append(f"{report[\'edge_id\']}: {exc}")\n            print(f"FAILED: {exc}")\n\n    if failures:\n        raise ExecutableTargetEngineError(\n            "One or more edge audits failed:\\n- " + "\\n- ".join(failures)\n        )\n\n\nif __name__ == "__main__":\n    try:\n        main()\n    except ExecutableTargetEngineError as exc:\n        raise SystemExit(f"ERROR: {exc}") from exc\n', 'tests/test_mldp_executable_target_engine.py': 'from __future__ import annotations\n\nimport unittest\nimport numpy as np\nimport pandas as pd\n\nfrom shared.execution.executable_target_dataset import (\n    TRAINING_AUTHORITY,\n    _event_id,\n    assemble_executable_outcome_frame,\n)\n\n\nclass TestMLDPExecutableTargetEngine(unittest.TestCase):\n    def _gross(self) -> pd.DataFrame:\n        t0 = pd.to_datetime(\n            ["2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z"]\n        )\n        t1 = pd.to_datetime(\n            ["2026-01-01T00:05:00Z", "2026-01-01T00:15:00Z"]\n        )\n        return pd.DataFrame(\n            {\n                "Datetime": t0,\n                "Event_Vol": [0.01, 0.02],\n                "side": [1, 1],\n                "realized_r": [1.0, -1.0],\n                "meta_label": [1, 0],\n                "t1": t1,\n                "source_event_row_number": [0, 1],\n                "event_id": ["a", "b"],\n                "trgt": [0.01, 0.02],\n                "geometry_hash": ["g", "g"],\n            }\n        )\n\n    def test_resolved_row_gets_same_quote_r_label_t1(self):\n        gross = self._gross()\n        execution = pd.DataFrame(\n            {\n                "source_oof_row_number": [0, 1],\n                "execution_status": ["resolved", "resolved"],\n                "executed_r": [-0.25, 0.50],\n                "exit_time": pd.to_datetime(\n                    [\n                        "2026-01-01T00:04:00Z",\n                        "2026-01-01T00:14:00Z",\n                    ]\n                ),\n                "entry_time": pd.to_datetime(\n                    [\n                        "2026-01-01T00:00:01Z",\n                        "2026-01-01T00:10:01Z",\n                    ]\n                ),\n            }\n        )\n        out = assemble_executable_outcome_frame(gross, execution)\n        self.assertEqual(\n            out["quote_executable_meta_label"].astype(int).tolist(),\n            [0, 1],\n        )\n        self.assertTrue(\n            np.allclose(\n                out["quote_executable_r"].to_numpy(float),\n                [-0.25, 0.50],\n            )\n        )\n        self.assertEqual(int(out["gross_to_quote_label_flip"].sum()), 2)\n        self.assertTrue(out["mldp_target_eligible"].all())\n        self.assertTrue(\n            (out["mldp_training_authority"] == TRAINING_AUTHORITY).all()\n        )\n\n    def test_unresolved_is_not_silently_coerced_to_zero(self):\n        gross = self._gross()\n        execution = pd.DataFrame(\n            {\n                "source_oof_row_number": [0, 1],\n                "execution_status": ["resolved", "stale_entry_quote"],\n                "executed_r": [0.20, np.nan],\n                "exit_time": [\n                    pd.Timestamp("2026-01-01T00:04:00Z"),\n                    pd.NaT,\n                ],\n            }\n        )\n        out = assemble_executable_outcome_frame(gross, execution)\n        self.assertEqual(\n            int(out.loc[0, "quote_executable_meta_label"]), 1\n        )\n        self.assertTrue(\n            pd.isna(out.loc[1, "quote_executable_meta_label"])\n        )\n        self.assertTrue(pd.isna(out.loc[1, "quote_executable_r"]))\n        self.assertTrue(pd.isna(out.loc[1, "quote_executable_t1"]))\n        self.assertFalse(bool(out.loc[1, "mldp_target_eligible"]))\n\n    def test_event_id_binds_geometry(self):\n        a = _event_id(\n            "BTCUSD/x_long", "2026-01-01T00:00:00Z", 1, "geom1"\n        )\n        b = _event_id(\n            "BTCUSD/x_long", "2026-01-01T00:00:00Z", 1, "geom1"\n        )\n        c = _event_id(\n            "BTCUSD/x_long", "2026-01-01T00:00:00Z", 1, "geom2"\n        )\n        self.assertEqual(a, b)\n        self.assertNotEqual(a, c)\n\n\nif __name__ == "__main__":\n    unittest.main()\n', 'tests/test_mldp_executable_target_discovery.py': 'from __future__ import annotations\n\nimport tempfile\nfrom pathlib import Path\nimport unittest\n\nfrom shared.execution.executable_target_dataset import discover_edge_roots\n\n\nclass TestMLDPExecutableTargetDiscovery(unittest.TestCase):\n    def _edge(\n        self,\n        root: Path,\n        asset: str,\n        edge: str,\n        *,\n        model_rows_artifact: bool,\n    ) -> Path:\n        e = root / "assets" / asset / "edges" / edge\n        e.mkdir(parents=True)\n        for name in ("edge.yaml", "runtime.py", "model_rows.py"):\n            (e / name).write_text("# fixture\\n", encoding="utf-8")\n        if model_rows_artifact:\n            stem = f"{asset.lower()}_{edge}"\n            d = e / "artifacts" / "research_dataset"\n            d.mkdir(parents=True)\n            (d / f"{stem}_model_rows.csv").write_text(\n                "Datetime\\n", encoding="utf-8"\n            )\n        return e\n\n    def test_discovery_is_multi_asset_and_not_edge_hardcoded(self):\n        with tempfile.TemporaryDirectory() as td:\n            root = Path(td)\n            e1 = self._edge(\n                root, "AAA", "alpha_long", model_rows_artifact=False\n            )\n            e2 = self._edge(\n                root, "BBB", "beta_short", model_rows_artifact=False\n            )\n            found = discover_edge_roots(root)\n            self.assertEqual(found, [e1.resolve(), e2.resolve()])\n            self.assertEqual(\n                discover_edge_roots(root, asset="BBB"), [e2.resolve()]\n            )\n\n    def test_require_model_rows_is_artifact_driven(self):\n        with tempfile.TemporaryDirectory() as td:\n            root = Path(td)\n            self._edge(\n                root, "AAA", "alpha_long", model_rows_artifact=False\n            )\n            e2 = self._edge(\n                root, "BBB", "beta_short", model_rows_artifact=True\n            )\n            found = discover_edge_roots(root, require_model_rows=True)\n            self.assertEqual(found, [e2.resolve()])\n\n\nif __name__ == "__main__":\n    unittest.main()\n', 'docs/mldp/TASK2A_EXECUTABLE_TARGET_ENGINE.md': "# Quantara MLDP Transformation - Task 2A\n\n## Pre-model executable-outcome engine\n\nTask 2A installs one shared, asset-agnostic and edge-agnostic audit station.\nIt does **not** yet change Step 01-09 training semantics.\n\n### Methodological basis\n\nAFML Chapter 3 makes triple-barrier labels path-dependent and defines `t1` as\nthe first barrier touch. When a primary model/rule supplies side, meta-labeling\nlets a secondary model decide act versus pass. AFML Chapter 4 then measures\noverlap and uniqueness using each label's `[t0,t1]` lifespan.\n\nMLAM Chapter 5 likewise argues that financial labels should reflect the\nsuccess/failure of a position, preserve the relevant path information, and use\nmeta-labeling when side is predetermined.\n\nQuantara's exact historical Bid/Ask resolver is not a canonical AFML/MLAM\nalgorithm. It is a `QUANTARA_EXTENSION`. Task 2A reuses that resolver rather\nthan creating a second execution model.\n\n### Why Task 2A is audit-only\n\nAFML and MLAM do not specify what a Quantara target should be when a\nhistorical event has no timely quote because of true market inactivity, missing\nhistorical coverage, a feed defect, or right-censoring. Treating all such cases as\nlosses would invent semantics. Dropping them blindly could condition the sample\non post-event information.\n\nTask 2A therefore:\n- resolves every source event it can;\n- assigns `quote_executable_r`, `quote_executable_meta_label`, and\n  `quote_executable_t1` only to genuinely resolved rows;\n- leaves unresolved target fields missing and records the reason;\n- marks outputs `AUDIT_ONLY_TASK2A_NOT_YET_AUTHORIZED`;\n- does not authorize Step 01-09 to consume the new target.\n\nThe pilot audit will enumerate unresolved reasons before Task 2B freezes one\nuniversal population rule.\n\n### Dynamic scope\n\nThe CLI discovers:\n`assets/<ASSET>/edges/<family>_<side>/`\n\nNo asset, edge family, current edge count, or profitability assumption is\nhard-coded. `BTCUSD/absorption_long` is only the first validation specimen.\n\n### Commands\n\nFrom `trading_models_v2`:\n\n```powershell\npython .\\build_mldp_executable_targets.py --all --dry-run\npython .\\build_mldp_executable_targets.py --edge-root .\\assets\\BTCUSD\\edges\\absorption_long\n```\n\nDo not run Step 01-09 from Task-2A outputs.\n"}


class PatchError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run_git(args: list[str], cwd: Path) -> str:
    cp = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    if cp.returncode != 0:
        raise PatchError(
            "git " + " ".join(args) + " failed:\n" + cp.stdout + cp.stderr
        )
    return cp.stdout.strip()


def locate_v2_root(start: Path) -> Path:
    here = start.resolve()
    for p in [here, *here.parents]:
        if (
            p.name == "trading_models_v2"
            and (p / "shared").exists()
            and (p / "assets").exists()
        ):
            return p
        q = p / "trading_models_v2"
        if (
            q.exists()
            and (q / "shared").exists()
            and (q / "assets").exists()
        ):
            return q.resolve()
    raise PatchError("Could not locate trading_models_v2 root.")


def git_root(v2_root: Path) -> Path:
    return Path(run_git(["rev-parse", "--show-toplevel"], v2_root)).resolve()


def assert_clean_tracked(repo_root: Path) -> None:
    status = run_git(
        ["status", "--porcelain", "--untracked-files=no"], repo_root
    )
    if status:
        raise PatchError(
            "Tracked working tree is not clean. Commit/stash/restore existing "
            "tracked changes before Task 2A.\n" + status
        )


def git_blob_sha(path: Path, repo_root: Path) -> str:
    """Return the Git blob SHA-1 for the exact working-tree bytes at *path*.

    GitHub's Contents API `sha` field is a Git blob object identity. It is not
    the same digest as hashlib.sha256(file_bytes). `git hash-object` computes
    the matching Git blob identity without modifying the repository.
    """
    return run_git(["hash-object", str(path.resolve())], repo_root)


def assert_git_blob_sha(
    path: Path, expected_git_blob_sha1: str, repo_root: Path
) -> None:
    if not path.exists():
        raise PatchError(f"Missing Task-2A prerequisite: {path}")
    if len(expected_git_blob_sha1) != 40:
        raise PatchError(
            "Internal patch design error: expected Git blob identity must be "
            f"40 hex characters, got {expected_git_blob_sha1!r}."
        )
    actual_git_blob_sha1 = git_blob_sha(path, repo_root)
    if actual_git_blob_sha1 != expected_git_blob_sha1:
        raise PatchError(
            "Prerequisite changed since Task 2A was designed:\n"
            f"  path                      : {path}\n"
            f"  expected Git blob SHA-1   : {expected_git_blob_sha1}\n"
            f"  actual Git blob SHA-1     : {actual_git_blob_sha1}\n"
            f"  actual ordinary SHA-256   : {sha256_file(path)}\n"
            "Stop and have the current v3 source re-audited rather than "
            "patching blindly."
        )


def assert_preflight(
    v2_root: Path, repo_root: Path
) -> tuple[str, str]:
    branch = run_git(["branch", "--show-current"], repo_root)
    head = run_git(["rev-parse", "HEAD"], repo_root)
    if branch != EXPECTED_BRANCH:
        raise PatchError(
            f"Expected branch {EXPECTED_BRANCH!r}, got {branch!r}."
        )

    assert_git_blob_sha(
        v2_root / "shared/execution/oof_exact_execution.py",
        EXPECTED_EXECUTION_SHA,
        repo_root,
    )
    assert_git_blob_sha(
        v2_root / "shared/labels/outcome_ontology.py",
        EXPECTED_ONTOLOGY_SHA,
        repo_root,
    )
    assert_git_blob_sha(
        v2_root / "config/mldp_method_registry.json",
        EXPECTED_REGISTRY_SHA,
        repo_root,
    )
    assert_git_blob_sha(
        v2_root / "docs/mldp/TRANSFORMATION_STATE.json",
        EXPECTED_STATE_SHA,
        repo_root,
    )

    state = json.loads(
        (v2_root / "docs/mldp/TRANSFORMATION_STATE.json").read_text(
            encoding="utf-8"
        )
    )
    if state.get("phase") != "TASK1_FOUNDATION_INSTALLED":
        raise PatchError(
            "Task 1 state is not authoritative: "
            f"phase={state.get('phase')!r}"
        )
    if int(state.get("signal_age_seconds_source_default", -1)) != 120:
        raise PatchError(
            "Task 1 signal-age source default is not frozen at 120 seconds."
        )
    return branch, head


def updated_registry_text(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    methods = data.get("methods")
    if not isinstance(methods, list):
        raise PatchError("MLDP method registry has invalid methods list.")

    wanted = {
        "method_id": "quantara_pre_model_exact_bidask_target_resolution",
        "classification": "QUANTARA_EXTENSION",
        "source_reference": (
            "AFML Ch.3/Ch.4 and MLAM Ch.5 labeling/meta-labeling "
            "semantics; Quantara exact Bid/Ask execution is a production "
            "extension"
        ),
        "quantara_status": "TASK2A_INSTALLED_AUDIT_ONLY",
    }

    matches = [
        m
        for m in methods
        if m.get("method_id") == wanted["method_id"]
    ]
    if matches:
        current = matches[0]
        for key in ("classification", "source_reference"):
            if current.get(key) != wanted[key]:
                raise PatchError(
                    "Existing Task-2A registry entry conflicts with patch."
                )
        current["quantara_status"] = wanted["quantara_status"]
    else:
        methods.append(wanted)

    for method in methods:
        if method.get("method_id") == "quantara_exact_bidask_execution":
            method["quantara_status"] = (
                "PRESERVE_SINGLE_RESOLVER_REUSED_BY_TASK2A"
            )

    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def updated_state_text(path: Path, head_before: str) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["patch_id"] = PATCH_ID
    data["reference_head_before_task2a"] = head_before
    data["phase"] = "TASK2A_EXECUTABLE_TARGET_ENGINE_INSTALLED"
    data["task2a_engine_installed"] = True
    data["task2a_training_authority"] = (
        "AUDIT_ONLY_TASK2A_NOT_YET_AUTHORIZED"
    )
    data["edge_pipeline_migrated_to_executable_target"] = False
    data["current_generation_promotable"] = False
    data["next_task"] = "TASK2A_PILOT_EXECUTION_AUDIT"
    data["next_pilot_recommendation"] = "BTCUSD/absorption_long"

    notes = list(data.get("notes", []))
    additions = [
        (
            "Task 2A is shared across assets/edges; the BTC pilot is only "
            "a validation specimen."
        ),
        (
            "Task 2A reuses the existing exact Bid/Ask resolver before "
            "model training and does not duplicate execution math."
        ),
        (
            "Unresolved/no-timely-entry events are not silently labeled "
            "0 or loss; universal semantics must be frozen before Task 2B."
        ),
        (
            "Step 01-09 remains on the legacy gross target until Task "
            "2B/2C explicitly migrates the shared lifecycle."
        ),
    ]
    for note in additions:
        if note not in notes:
            notes.append(note)
    data["notes"] = notes
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def compile_payloads() -> None:
    for rel, source in NEW_FILES.items():
        if rel.endswith(".py"):
            compile(source, rel, "exec")


def dynamic_inventory(v2_root: Path) -> tuple[int, int, int]:
    assets: set[str] = set()
    packages = 0
    with_rows = 0

    for edge_yaml in sorted(
        (v2_root / "assets").glob("*/edges/*/edge.yaml")
    ):
        edge_root = edge_yaml.parent
        if not (edge_root / "runtime.py").exists():
            continue
        if not (edge_root / "model_rows.py").exists():
            continue
        asset = edge_root.parents[1].name
        stem = f"{asset.lower()}_{edge_root.name}"
        model_rows = (
            edge_root
            / "artifacts"
            / "research_dataset"
            / f"{stem}_model_rows.csv"
        )
        assets.add(asset)
        packages += 1
        if model_rows.exists():
            with_rows += 1
    return len(assets), packages, with_rows


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".task2a_tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def run_unittests(v2_root: Path) -> None:
    print("\nFocused Task-2A tests")
    print("---------------------")
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "-v",
            "tests.test_mldp_executable_target_engine",
            "tests.test_mldp_executable_target_discovery",
        ],
        cwd=str(v2_root),
        text=True,
        capture_output=True,
    )
    sys.stdout.write(cp.stdout)
    sys.stdout.write(cp.stderr)
    if cp.returncode != 0:
        raise PatchError("Focused Task-2A tests failed.")

    print("\nTask-1 ontology regression")
    print("--------------------------")
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "-v",
            "tests.test_mldp_outcome_ontology",
        ],
        cwd=str(v2_root),
        text=True,
        capture_output=True,
    )
    sys.stdout.write(cp.stdout)
    sys.stdout.write(cp.stderr)
    if cp.returncode != 0:
        raise PatchError(
            "Task-1 outcome ontology regression test failed."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    v2_root = locate_v2_root(Path.cwd())
    repo_root = git_root(v2_root)
    assert_clean_tracked(repo_root)
    branch, head = assert_preflight(v2_root, repo_root)
    compile_payloads()

    assets, packages, with_rows = dynamic_inventory(v2_root)
    registry_path = v2_root / "config/mldp_method_registry.json"
    state_path = v2_root / "docs/mldp/TRANSFORMATION_STATE.json"
    registry_text = updated_registry_text(registry_path)
    state_text = updated_state_text(state_path, head)

    print(f"Git branch: {branch}")
    print(f"Git HEAD  : {head}")
    print(f"Patcher   : {PATCHER_REVISION}")
    print("Prerequisite identity check: Git blob SHA-1 via git hash-object")
    print("\nTask-2A plan")
    print("------------")
    print(f"Root                        : {v2_root}")
    print(f"Discovered assets           : {assets}")
    print(f"Discovered edge packages    : {packages}")
    print(f"Packages with model-row CSV : {with_rows}")
    print(f"Add/verify files            : {len(NEW_FILES)}")
    print("Update MLDP registry         : yes")
    print("Update transformation state : yes")
    print("Modify edge-specific code    : NO")
    print("Modify Step 01-09            : NO")
    print("Modify exact resolver math   : NO")
    print("Git operations               : NONE")

    print("\nTask-2A scientific boundary")
    print("--------------------------")
    print(
        "- Exact Bid/Ask resolution becomes a reusable PRE-MODEL "
        "audit station."
    )
    print(
        "- Target candidate is quote_executable, not net_executable."
    )
    print("- Unresolved observations receive NO invented label.")
    print(
        "- No Task-2A output is authorized for training until "
        "Task 2B freezes population semantics."
    )

    if args.dry_run:
        print("\nDRY RUN PASS - no files were changed.")
        return

    backup_root = (
        v2_root
        / ".quantara_patch_backups"
        / (
            PATCH_ID
            + "_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
    )
    backup_root.mkdir(parents=True, exist_ok=False)

    modified_existing = [registry_path, state_path]
    created: list[Path] = []

    for path in modified_existing:
        rel = path.relative_to(v2_root)
        dest = backup_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    try:
        for rel, source in NEW_FILES.items():
            path = v2_root / rel
            if path.exists():
                if path.read_text(encoding="utf-8") != source:
                    raise PatchError(
                        "Refusing to overwrite an existing non-identical "
                        f"Task-2A file: {path}"
                    )
            else:
                write_atomic(path, source)
                created.append(path)

        write_atomic(registry_path, registry_text)
        write_atomic(state_path, state_text)

        run_unittests(v2_root)

        cp = subprocess.run(
            [
                sys.executable,
                "build_mldp_executable_targets.py",
                "--all",
                "--dry-run",
            ],
            cwd=str(v2_root),
            text=True,
            capture_output=True,
        )
        if cp.returncode != 0:
            raise PatchError(
                "Repository-wide dynamic discovery smoke failed:\n"
                + cp.stdout
                + cp.stderr
            )

        print("\nRepository-wide dynamic discovery smoke")
        print("---------------------------------------")
        keep = (
            "Discovered edge packages",
            "Assets",
            "Ready model-row packages",
            "Blocked/no artifacts",
            "Training authority",
            "DRY RUN COMPLETE",
        )
        for line in cp.stdout.splitlines():
            if any(token in line for token in keep):
                print(line)

    except Exception:
        for path in modified_existing:
            backup = backup_root / path.relative_to(v2_root)
            if backup.exists():
                shutil.copy2(backup, path)
        for path in reversed(created):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise

    print("\nTASK 2A ENGINE INSTALLED SUCCESSFULLY")
    print(f"Backup: {backup_root.relative_to(v2_root)}")
    print("No git add/commit/push was executed.")

    print("\nNext actions")
    print("------------")
    print("1. Inspect: git diff -- trading_models_v2")
    print(
        "2. Repository-wide compatibility audit:\n"
        "   python .\\build_mldp_executable_targets.py --all --dry-run"
    )
    print(
        "3. ONE pilot outcome audit (not Step 01-09):\n"
        "   python .\\build_mldp_executable_targets.py "
        "--edge-root .\\assets\\BTCUSD\\edges\\absorption_long"
    )
    print(
        "4. Send both console outputs and the pilot Task-2A manifest "
        "back to ChatGPT."
    )


if __name__ == "__main__":
    try:
        main()
    except PatchError as exc:
        raise SystemExit(f"PATCH FAILED: {exc}") from exc
