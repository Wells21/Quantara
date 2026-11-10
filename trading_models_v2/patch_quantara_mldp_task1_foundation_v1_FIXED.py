#!/usr/bin/env python3
"""Quantara V3 — MLDP Transformation Task 1 Foundation Patcher.

Run from repo root or trading_models_v2:
    python patch_quantara_mldp_task1_foundation_v1_FIXED.py --dry-run
    python patch_quantara_mldp_task1_foundation_v1_FIXED.py

Then:
    python patch_quantara_mldp_task1_foundation_v1_FIXED.py

This file never runs git add, commit, push, merge, checkout, or reset.
It installs the Task-1 semantic/CV/sizing/trial-accounting foundation only.
It deliberately does not yet migrate edge Step 01-04 to executable targets.
"""

from __future__ import annotations

NEW_FILES = {'shared/labels/outcome_ontology.py': '"""Canonical MLDP/Quantara event-outcome ontology.\n\nAFML/MLAM do not define these four Quantara column names. This is a Quantara\nproduction extension that keeps distinct economic objects distinct:\ngross research, quote-executable, net simulated executable, and live realized.\n\nA model probability, its label, its information end time, and its calibration\ntarget must refer to one and the same outcome object.\n"""\nfrom __future__ import annotations\nfrom dataclasses import dataclass\nfrom typing import Any, Mapping\nimport numpy as np\nimport pandas as pd\n\nONTOLOGY_VERSION = "quantara_mldp_event_outcome_v1"\n\nclass OutcomeOntologyError(RuntimeError):\n    pass\n\n@dataclass(frozen=True)\nclass OutcomeLayer:\n    name: str\n    return_column: str\n    label_column: str\n    end_time_column: str\n    contract_version: str\n    description: str\n\nLAYERS: Mapping[str, OutcomeLayer] = {\n    "gross": OutcomeLayer(\n        "gross", "gross_r", "gross_meta_label", "gross_t1",\n        "quantara_mldp_gross_outcome_v1",\n        "Theoretical side-adjusted research outcome on frozen event geometry.",\n    ),\n    "quote_executable": OutcomeLayer(\n        "quote_executable", "quote_executable_r", "quote_executable_meta_label",\n        "quote_executable_t1", "quantara_mldp_quote_executable_outcome_v1",\n        "Historical Bid/Ask executable outcome under the frozen execution contract.",\n    ),\n    "net_executable": OutcomeLayer(\n        "net_executable", "net_executable_r", "net_executable_meta_label",\n        "net_executable_t1", "quantara_mldp_net_executable_outcome_v1",\n        "Quote-executable outcome after every defensible historically modelled cost.",\n    ),\n    "live_realized": OutcomeLayer(\n        "live_realized", "live_realized_r", "live_realized_meta_label",\n        "live_realized_t1", "quantara_mldp_live_realized_outcome_v1",\n        "Actual broker-fill outcome and actual charged costs.",\n    ),\n}\n\nLEGACY_ALIASES: Mapping[str, str] = {\n    "realized_r": "gross_r",\n    "meta_label": "gross_meta_label",\n    "t1": "gross_t1",\n    "executed_r": "quote_executable_r",\n    "executed_meta_label": "quote_executable_meta_label",\n    "exit_time": "quote_executable_t1",\n}\n\ndef get_layer(name: str) -> OutcomeLayer:\n    try:\n        return LAYERS[str(name)]\n    except KeyError as exc:\n        raise OutcomeOntologyError(\n            f"Unknown outcome layer {name!r}. Expected one of {sorted(LAYERS)}."\n        ) from exc\n\ndef label_from_r(values: Any) -> np.ndarray:\n    r = np.asarray(values, dtype="float64")\n    if not np.isfinite(r).all():\n        raise OutcomeOntologyError("Outcome R must be finite before a binary label is formed.")\n    return (r > 0.0).astype("int64")\n\ndef _series_equal(left: pd.Series, right: pd.Series, canonical: str) -> bool:\n    if canonical.endswith("_t1"):\n        a = pd.to_datetime(left, utc=True, errors="coerce")\n        b = pd.to_datetime(right, utc=True, errors="coerce")\n        return bool(a.equals(b))\n    if canonical.endswith("_meta_label"):\n        a = pd.to_numeric(left, errors="coerce")\n        b = pd.to_numeric(right, errors="coerce")\n        if a.isna().any() or b.isna().any():\n            return False\n        return bool(np.array_equal(a.astype("int64").to_numpy(), b.astype("int64").to_numpy()))\n    a = pd.to_numeric(left, errors="coerce").to_numpy(dtype="float64")\n    b = pd.to_numeric(right, errors="coerce").to_numpy(dtype="float64")\n    return bool(\n        np.all(np.isfinite(a)) and np.all(np.isfinite(b))\n        and np.allclose(a, b, rtol=0.0, atol=1e-12)\n    )\n\ndef canonicalize_legacy_outcomes(\n    frame: pd.DataFrame, *, copy: bool = True, strict: bool = True\n) -> pd.DataFrame:\n    out = frame.copy() if copy else frame\n    for legacy, canonical in LEGACY_ALIASES.items():\n        if legacy not in out.columns:\n            continue\n        if canonical in out.columns:\n            if strict and not _series_equal(out[legacy], out[canonical], canonical):\n                raise OutcomeOntologyError(\n                    f"Legacy column {legacy!r} conflicts with canonical {canonical!r}."\n                )\n        else:\n            out[canonical] = out[legacy]\n    return out\n\ndef validate_layer(\n    frame: pd.DataFrame,\n    layer_name: str,\n    *,\n    t0_column: str = "Datetime",\n    require_label: bool = True,\n) -> dict[str, Any]:\n    layer = get_layer(layer_name)\n    required = [t0_column, layer.return_column, layer.end_time_column]\n    if require_label:\n        required.append(layer.label_column)\n    missing = [c for c in required if c not in frame.columns]\n    if missing:\n        raise OutcomeOntologyError(f"{layer_name}: missing required columns: {missing}")\n    t0 = pd.to_datetime(frame[t0_column], utc=True, errors="coerce")\n    t1 = pd.to_datetime(frame[layer.end_time_column], utc=True, errors="coerce")\n    r = pd.to_numeric(frame[layer.return_column], errors="coerce")\n    if t0.isna().any() or t1.isna().any():\n        raise OutcomeOntologyError(f"{layer_name}: event timestamps contain NaT.")\n    if bool((t1 < t0).any()):\n        raise OutcomeOntologyError(f"{layer_name}: end time precedes event start.")\n    if r.isna().any() or not np.isfinite(r.to_numpy(dtype="float64")).all():\n        raise OutcomeOntologyError(f"{layer_name}: R contains NaN/inf.")\n    expected = label_from_r(r.to_numpy(dtype="float64"))\n    if require_label:\n        label = pd.to_numeric(frame[layer.label_column], errors="coerce")\n        if label.isna().any():\n            raise OutcomeOntologyError(f"{layer_name}: label contains invalid values.")\n        actual = label.astype("int64").to_numpy()\n        if not np.array_equal(actual, expected):\n            mismatches = int(np.count_nonzero(actual != expected))\n            raise OutcomeOntologyError(\n                f"{layer_name}: {layer.label_column} != int({layer.return_column} > 0); "\n                f"mismatches={mismatches}."\n            )\n    return {\n        "status": "PASS",\n        "ontology_version": ONTOLOGY_VERSION,\n        "layer": layer.name,\n        "contract_version": layer.contract_version,\n        "rows": int(len(frame)),\n        "positive_rate": float(expected.mean()) if len(expected) else float("nan"),\n    }\n\ndef target_columns(layer_name: str) -> tuple[str, str, str]:\n    layer = get_layer(layer_name)\n    return layer.return_column, layer.label_column, layer.end_time_column\n', 'shared/validation/mldp_conformity.py': '"""MLDP methodological-conformity primitives for Quantara."""\nfrom __future__ import annotations\nfrom dataclasses import asdict, dataclass\nimport json\nfrom pathlib import Path\nfrom typing import Any, Mapping\nfrom shared.labels.outcome_ontology import ONTOLOGY_VERSION, get_layer\n\nclass MLDPConformityError(RuntimeError):\n    pass\n\n@dataclass(frozen=True)\nclass ProbabilityTargetContract:\n    outcome_layer: str\n    target_contract_version: str\n    target_column: str\n    return_column: str\n    label_end_time_column: str\n    probability_column: str\n    probability_semantics: str\n    ontology_version: str = ONTOLOGY_VERSION\n\n    def to_dict(self) -> dict[str, Any]:\n        return asdict(self)\n\ndef probability_target_contract(\n    layer_name: str, *, probability_column: str\n) -> ProbabilityTargetContract:\n    layer = get_layer(layer_name)\n    semantics = (\n        f"{probability_column} estimates P({layer.label_column}=1 | information "\n        f"available at event t0), where {layer.label_column}=1 iff "\n        f"{layer.return_column}>0 under {layer.contract_version}."\n    )\n    return ProbabilityTargetContract(\n        outcome_layer=layer.name,\n        target_contract_version=layer.contract_version,\n        target_column=layer.label_column,\n        return_column=layer.return_column,\n        label_end_time_column=layer.end_time_column,\n        probability_column=str(probability_column),\n        probability_semantics=semantics,\n    )\n\ndef _coerce_contract(value: ProbabilityTargetContract | Mapping[str, Any]) -> dict[str, Any]:\n    return value.to_dict() if isinstance(value, ProbabilityTargetContract) else dict(value)\n\ndef assert_same_target(\n    model_probability: ProbabilityTargetContract | Mapping[str, Any],\n    calibration_target: ProbabilityTargetContract | Mapping[str, Any],\n) -> dict[str, Any]:\n    left = _coerce_contract(model_probability)\n    right = _coerce_contract(calibration_target)\n    keys = (\n        "ontology_version", "outcome_layer", "target_contract_version",\n        "target_column", "return_column", "label_end_time_column",\n    )\n    mismatches = {\n        key: {"model": left.get(key), "calibration": right.get(key)}\n        for key in keys if left.get(key) != right.get(key)\n    }\n    if mismatches:\n        raise MLDPConformityError(\n            "Probability/calibration target mismatch. Calibration cannot hide "\n            "a change of estimand. " + json.dumps(mismatches, sort_keys=True)\n        )\n    return {\n        "status": "PASS",\n        "same_target": True,\n        "outcome_layer": left["outcome_layer"],\n    }\n\ndef validate_method_registry(path: str | Path) -> dict[str, Any]:\n    p = Path(path)\n    payload = json.loads(p.read_text(encoding="utf-8"))\n    allowed = {\n        "MLDP_CANONICAL", "MLDP_LATER_REFINEMENT",\n        "QUANTARA_EXTENSION", "DATA_CONSTRAINED_ADAPTATION",\n    }\n    methods = payload.get("methods")\n    if not isinstance(methods, list) or not methods:\n        raise MLDPConformityError("Method registry must contain a non-empty methods list.")\n    ids: set[str] = set()\n    for row in methods:\n        method_id = str(row.get("method_id", "")).strip()\n        if not method_id or method_id in ids:\n            raise MLDPConformityError(f"Invalid/duplicate method_id: {method_id!r}")\n        ids.add(method_id)\n        if row.get("classification") not in allowed:\n            raise MLDPConformityError(\n                f"{method_id}: invalid classification {row.get(\'classification\')!r}"\n            )\n        if not str(row.get("source_reference", "")).strip():\n            raise MLDPConformityError(f"{method_id}: source_reference is required.")\n    return {\n        "status": "PASS",\n        "method_count": len(methods),\n        "registry_version": payload.get("registry_version"),\n    }\n', 'shared/models/financial_cv.py': '"""Canonical AFML financial-CV interval primitives.\n\nAFML Chapter 7 purges training labels whose information intervals overlap\ntesting labels and embargoes observations immediately after each testing block.\nThis module is the single low-level interval authority for Quantara.\n"""\nfrom __future__ import annotations\nfrom typing import Any, Sequence\nimport numpy as np\nimport pandas as pd\n\nclass FinancialCVError(RuntimeError):\n    pass\n\ndef _utc_ns(values: Sequence[Any], name: str) -> np.ndarray:\n    series = pd.to_datetime(pd.Series(values), utc=True, errors="coerce")\n    if series.isna().any():\n        raise FinancialCVError(f"{name} contains NaT/unparseable timestamps.")\n    return series.astype("int64").to_numpy(dtype="int64")\n\ndef validate_intervals(\n    t0: Sequence[Any], t1: Sequence[Any]\n) -> tuple[np.ndarray, np.ndarray]:\n    starts = _utc_ns(t0, "t0")\n    ends = _utc_ns(t1, "t1")\n    if len(starts) != len(ends):\n        raise FinancialCVError("t0 and t1 length mismatch.")\n    if np.any(ends < starts):\n        raise FinancialCVError("At least one label interval ends before it starts.")\n    return starts, ends\n\ndef contiguous_index_blocks(indices: Sequence[int]) -> list[tuple[int, int]]:\n    idx = np.sort(np.unique(np.asarray(indices, dtype=int)))\n    if len(idx) == 0:\n        return []\n    blocks: list[tuple[int, int]] = []\n    start = prev = int(idx[0])\n    for value in idx[1:]:\n        value = int(value)\n        if value != prev + 1:\n            blocks.append((start, prev))\n            start = value\n        prev = value\n    blocks.append((start, prev))\n    return blocks\n\ndef merge_time_intervals(\n    starts: np.ndarray, ends: np.ndarray\n) -> tuple[np.ndarray, np.ndarray]:\n    if len(starts) == 0:\n        return np.array([], dtype="int64"), np.array([], dtype="int64")\n    order = np.argsort(starts, kind="stable")\n    s = starts[order]\n    e = ends[order]\n    merged_s: list[int] = [int(s[0])]\n    merged_e: list[int] = [int(e[0])]\n    for start, end in zip(s[1:], e[1:]):\n        start_i, end_i = int(start), int(end)\n        if start_i <= merged_e[-1]:\n            merged_e[-1] = max(merged_e[-1], end_i)\n        else:\n            merged_s.append(start_i)\n            merged_e.append(end_i)\n    return np.asarray(merged_s, dtype="int64"), np.asarray(merged_e, dtype="int64")\n\ndef purge_overlapping_train_indices(\n    train_indices: Sequence[int],\n    test_indices: Sequence[int],\n    t0: Sequence[Any],\n    t1: Sequence[Any],\n) -> np.ndarray:\n    """Remove every train interval intersecting any test information interval."""\n    starts, ends = validate_intervals(t0, t1)\n    trn = np.sort(np.unique(np.asarray(train_indices, dtype=int)))\n    tst = np.sort(np.unique(np.asarray(test_indices, dtype=int)))\n    n = len(starts)\n    if np.any(trn < 0) or np.any(trn >= n) or np.any(tst < 0) or np.any(tst >= n):\n        raise FinancialCVError("train/test index out of bounds.")\n    if len(trn) == 0 or len(tst) == 0:\n        return trn.astype(int)\n    test_s, test_e = merge_time_intervals(starts[tst], ends[tst])\n    keep = np.ones(len(trn), dtype=bool)\n    for pos, row in enumerate(trn):\n        train_start = starts[row]\n        train_end = ends[row]\n        # Find the last test interval whose start is <= train_end.\n        j = int(np.searchsorted(test_s, train_end, side="right") - 1)\n        if j >= 0 and test_e[j] >= train_start:\n            keep[pos] = False\n    return trn[keep].astype(int)\n\ndef embargo_after_test_blocks(\n    train_indices: Sequence[int],\n    test_indices: Sequence[int],\n    n_samples: int,\n    embargo_size: int,\n) -> np.ndarray:\n    """Embargo row positions immediately after each contiguous test block."""\n    if embargo_size < 0:\n        raise FinancialCVError("embargo_size must be >= 0.")\n    trn = np.sort(np.unique(np.asarray(train_indices, dtype=int)))\n    tst = np.sort(np.unique(np.asarray(test_indices, dtype=int)))\n    if len(trn) == 0 or len(tst) == 0 or embargo_size == 0:\n        return trn.astype(int)\n    keep = np.ones(len(trn), dtype=bool)\n    for _, block_end in contiguous_index_blocks(tst):\n        start = block_end + 1\n        stop = min(int(n_samples), start + int(embargo_size))\n        keep &= (trn < start) | (trn >= stop)\n    return trn[keep].astype(int)\n\ndef purge_and_embargo_train_indices(\n    train_indices: Sequence[int],\n    test_indices: Sequence[int],\n    t0: Sequence[Any],\n    t1: Sequence[Any],\n    *,\n    embargo_size: int = 0,\n) -> np.ndarray:\n    purged = purge_overlapping_train_indices(train_indices, test_indices, t0, t1)\n    return embargo_after_test_blocks(\n        purged, test_indices, len(t0), int(embargo_size)\n    )\n', 'shared/sizing/__init__.py': '"""Quantara sizing references and explicitly named extensions."""\nfrom .afml_ch10_reference import average_active_signals, discretize_signal, probability_to_signal\n\n__all__ = ["average_active_signals", "discretize_signal", "probability_to_signal"]\n', 'shared/sizing/afml_ch10_reference.py': '"""Canonical AFML Chapter 10 probability-based bet-sizing reference.\n\nThis implements the AFML reference object without Quantara\'s empirical\nbreak-even rescaling. Quantara-specific sizing extensions must live elsewhere\nand be benchmarked against this reference.\n"""\nfrom __future__ import annotations\nimport math\nfrom typing import Any, Sequence\nimport numpy as np\nimport pandas as pd\n\nclass AFMLBetSizingError(RuntimeError):\n    pass\n\ndef _normal_cdf(x: Any) -> np.ndarray:\n    arr = np.asarray(x, dtype="float64")\n    erf = np.vectorize(math.erf)\n    return 0.5 * (1.0 + erf(arr / math.sqrt(2.0)))\n\ndef probability_to_signal(\n    probability: Any,\n    predicted_label: Any,\n    *,\n    num_classes: int = 2,\n    primary_side: Any | None = None,\n) -> np.ndarray:\n    """AFML 10.3: z=(p-1/K)/sqrt(p(1-p)); signal=pred*(2*Phi(z)-1)."""\n    if int(num_classes) < 2:\n        raise AFMLBetSizingError("num_classes must be >= 2.")\n    p = np.asarray(probability, dtype="float64")\n    pred = np.asarray(predicted_label, dtype="float64")\n    try:\n        p, pred = np.broadcast_arrays(p, pred)\n    except ValueError as exc:\n        raise AFMLBetSizingError(\n            "probability and predicted_label are not broadcast-compatible."\n        ) from exc\n    if not np.isfinite(p).all() or np.any((p <= 0.0) | (p >= 1.0)):\n        raise AFMLBetSizingError("probability must be finite and strictly inside (0,1).")\n    if not np.isfinite(pred).all():\n        raise AFMLBetSizingError("predicted_label must be finite.")\n    p0 = 1.0 / float(num_classes)\n    z = (p - p0) / np.sqrt(p * (1.0 - p))\n    signal = pred * (2.0 * _normal_cdf(z) - 1.0)\n    if primary_side is not None:\n        side = np.asarray(primary_side, dtype="float64")\n        try:\n            signal, side = np.broadcast_arrays(signal, side)\n        except ValueError as exc:\n            raise AFMLBetSizingError("primary_side is not broadcast-compatible.") from exc\n        if not np.isfinite(side).all() or not np.isin(side, [-1.0, 1.0]).all():\n            raise AFMLBetSizingError("primary_side must contain only -1 or +1.")\n        signal = signal * side\n    return np.clip(np.asarray(signal, dtype="float64"), -1.0, 1.0)\n\ndef average_active_signals(\n    signals: pd.DataFrame,\n    *,\n    signal_column: str = "signal",\n    end_time_column: str = "t1",\n) -> pd.Series:\n    """AFML 10.4: average sizes of all bets active at each change point."""\n    if not isinstance(signals.index, pd.DatetimeIndex):\n        raise AFMLBetSizingError("signals must be indexed by event start DatetimeIndex.")\n    missing = [c for c in (signal_column, end_time_column) if c not in signals.columns]\n    if missing:\n        raise AFMLBetSizingError(f"signals missing columns: {missing}")\n    frame = signals.copy().sort_index()\n    if frame.index.tz is None:\n        frame.index = frame.index.tz_localize("UTC")\n    else:\n        frame.index = frame.index.tz_convert("UTC")\n    frame[end_time_column] = pd.to_datetime(\n        frame[end_time_column], utc=True, errors="coerce"\n    )\n    frame[signal_column] = pd.to_numeric(frame[signal_column], errors="coerce")\n    if frame[signal_column].isna().any():\n        raise AFMLBetSizingError("signal contains NaN/non-numeric values.")\n    change_points = set(frame.index)\n    change_points.update(frame[end_time_column].dropna().tolist())\n    out: dict[pd.Timestamp, float] = {}\n    for loc in sorted(change_points):\n        active = (frame.index <= loc) & (\n            frame[end_time_column].isna().to_numpy()\n            | (loc < frame[end_time_column]).to_numpy()\n        )\n        out[pd.Timestamp(loc)] = (\n            float(frame.loc[active, signal_column].mean()) if bool(active.any()) else 0.0\n        )\n    return pd.Series(out, name="signal", dtype="float64").sort_index()\n\ndef discretize_signal(signal: Any, step_size: float) -> Any:\n    """AFML 10.5: round to step size and clip to [-1,1]."""\n    step = float(step_size)\n    if not (0.0 < step <= 1.0):\n        raise AFMLBetSizingError("step_size must be in (0,1].")\n    if isinstance(signal, pd.Series):\n        return ((signal / step).round() * step).clip(-1.0, 1.0)\n    arr = np.asarray(signal, dtype="float64")\n    if not np.isfinite(arr).all():\n        raise AFMLBetSizingError("signal must be finite.")\n    return np.clip(np.round(arr / step) * step, -1.0, 1.0)\n\ndef get_signal(\n    events: pd.DataFrame,\n    probability: Sequence[float],\n    predicted_label: Sequence[float],\n    *,\n    num_classes: int = 2,\n    side_column: str = "side",\n    end_time_column: str = "t1",\n    step_size: float = 0.0,\n) -> pd.Series:\n    """Compose AFML 10.3 -> 10.4 -> optional 10.5."""\n    if len(events) != len(probability) or len(events) != len(predicted_label):\n        raise AFMLBetSizingError("events/probability/predicted_label length mismatch.")\n    side = events[side_column].to_numpy() if side_column in events.columns else None\n    raw = probability_to_signal(\n        probability, predicted_label, num_classes=num_classes, primary_side=side\n    )\n    signals = pd.DataFrame(\n        {"signal": raw, end_time_column: events[end_time_column].to_numpy()},\n        index=events.index,\n    )\n    averaged = average_active_signals(signals, end_time_column=end_time_column)\n    return discretize_signal(averaged, step_size) if step_size > 0 else averaged\n', 'shared/research/__init__.py': '"""Shared Quantara research-factory utilities."""\n', 'shared/research/trial_ledger.py': '"""Append-only Quantara research-trial ledger.\n\nAFML/MLAM require meta-information about the trials used to distill a strategy.\nThis helper records outcome-informed choices for later DSR/FWER/PBO analysis.\nIt never edits or deletes an existing record.\n"""\nfrom __future__ import annotations\nfrom datetime import datetime, timezone\nimport json\nimport os\nfrom pathlib import Path\nimport time\nfrom typing import Any, Mapping\nfrom uuid import uuid4\n\nclass TrialLedgerError(RuntimeError):\n    pass\n\nREQUIRED_FIELDS = {\n    "trial_id", "trial_type", "hypothesis_id", "parameters", "data_window",\n    "code_commit", "metric_name", "result_artifacts", "outcome_seen",\n    "selection_influenced", "historical_backfill", "trial_count_quality",\n    "created_at_utc",\n}\nALLOWED_COUNT_QUALITY = {"exact", "reconstructed", "lower_bound", "unknown"}\n\ndef utc_now() -> str:\n    return datetime.now(timezone.utc).isoformat(timespec="seconds")\n\ndef new_trial_id(prefix: str = "trial") -> str:\n    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")\n    return f"{prefix}-{stamp}-{uuid4().hex[:12]}"\n\ndef validate_trial_record(record: Mapping[str, Any]) -> dict[str, Any]:\n    out = dict(record)\n    missing = sorted(REQUIRED_FIELDS - set(out))\n    if missing:\n        raise TrialLedgerError(f"Trial record missing required fields: {missing}")\n    if not str(out["trial_id"]).strip() or not str(out["hypothesis_id"]).strip():\n        raise TrialLedgerError("trial_id and hypothesis_id must be non-empty.")\n    if not isinstance(out["parameters"], Mapping):\n        raise TrialLedgerError("parameters must be a mapping.")\n    if not isinstance(out["data_window"], Mapping):\n        raise TrialLedgerError("data_window must be a mapping.")\n    if not isinstance(out["result_artifacts"], list):\n        raise TrialLedgerError("result_artifacts must be a list.")\n    for key in ("outcome_seen", "selection_influenced", "historical_backfill"):\n        if not isinstance(out[key], bool):\n            raise TrialLedgerError(f"{key} must be boolean.")\n    if out["trial_count_quality"] not in ALLOWED_COUNT_QUALITY:\n        raise TrialLedgerError(\n            f"trial_count_quality must be one of {sorted(ALLOWED_COUNT_QUALITY)}."\n        )\n    json.dumps(out, sort_keys=True, allow_nan=False, default=str)\n    return out\n\ndef build_trial_record(\n    *,\n    trial_type: str,\n    hypothesis_id: str,\n    parameters: Mapping[str, Any],\n    data_window: Mapping[str, Any],\n    code_commit: str,\n    metric_name: str,\n    result_artifacts: list[str] | None = None,\n    outcome_seen: bool = False,\n    selection_influenced: bool = False,\n    historical_backfill: bool = False,\n    trial_count_quality: str = "exact",\n    parent_trial_id: str | None = None,\n    trial_id: str | None = None,\n    notes: str = "",\n) -> dict[str, Any]:\n    record = {\n        "trial_id": trial_id or new_trial_id(str(trial_type).replace(" ", "_")[:24]),\n        "trial_type": str(trial_type),\n        "hypothesis_id": str(hypothesis_id),\n        "parent_trial_id": parent_trial_id,\n        "parameters": dict(parameters),\n        "data_window": dict(data_window),\n        "code_commit": str(code_commit),\n        "metric_name": str(metric_name),\n        "result_artifacts": list(result_artifacts or []),\n        "outcome_seen": bool(outcome_seen),\n        "selection_influenced": bool(selection_influenced),\n        "historical_backfill": bool(historical_backfill),\n        "trial_count_quality": str(trial_count_quality),\n        "created_at_utc": utc_now(),\n        "notes": str(notes),\n    }\n    return validate_trial_record(record)\n\ndef _acquire_lock(lock_path: Path, timeout_seconds: float) -> int:\n    deadline = time.monotonic() + float(timeout_seconds)\n    while True:\n        try:\n            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)\n        except FileExistsError:\n            if time.monotonic() >= deadline:\n                raise TrialLedgerError(f"Timed out waiting for ledger lock: {lock_path}")\n            time.sleep(0.05)\n\ndef append_trial(\n    ledger_path: str | Path,\n    record: Mapping[str, Any],\n    *,\n    lock_timeout_seconds: float = 5.0,\n) -> None:\n    """Atomically append one JSON line; never rewrite existing ledger data."""\n    payload = validate_trial_record(record)\n    path = Path(ledger_path)\n    path.parent.mkdir(parents=True, exist_ok=True)\n    lock_path = path.with_suffix(path.suffix + ".lock")\n    fd = _acquire_lock(lock_path, lock_timeout_seconds)\n    try:\n        os.write(fd, f"{os.getpid()}\\n".encode("ascii"))\n        os.close(fd)\n        fd = -1\n        line = json.dumps(\n            payload, sort_keys=True, separators=(",", ":"),\n            allow_nan=False, default=str\n        ) + "\\n"\n        with path.open("a", encoding="utf-8", newline="\\n") as handle:\n            handle.write(line)\n            handle.flush()\n            os.fsync(handle.fileno())\n    finally:\n        if fd >= 0:\n            os.close(fd)\n        try:\n            lock_path.unlink()\n        except FileNotFoundError:\n            pass\n\ndef read_trials(ledger_path: str | Path) -> list[dict[str, Any]]:\n    path = Path(ledger_path)\n    if not path.exists():\n        return []\n    rows: list[dict[str, Any]] = []\n    seen: set[str] = set()\n    for line_no, raw in enumerate(\n        path.read_text(encoding="utf-8").splitlines(), start=1\n    ):\n        if not raw.strip():\n            continue\n        try:\n            row = validate_trial_record(json.loads(raw))\n        except Exception as exc:\n            raise TrialLedgerError(f"Invalid ledger row {line_no}: {exc}") from exc\n        if row["trial_id"] in seen:\n            raise TrialLedgerError(\n                f"Duplicate trial_id at row {line_no}: {row[\'trial_id\']}"\n            )\n        seen.add(row["trial_id"])\n        rows.append(row)\n    return rows\n', 'shared/validation/mldp_invalidation.py': '"""Dependency-aware MLDP transformation invalidation scanner.\n\nReports potentially stale artifact scopes. Never deletes files.\n"""\nfrom __future__ import annotations\nfrom pathlib import Path\nfrom typing import Iterable\n\nclass MLDPInvalidationError(RuntimeError):\n    pass\n\nCHANGE_SCOPES = {\n    "bar_semantics": {\n        "minimum_stale_from": "events",\n        "patterns": ["assets/**/artifacts/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],\n    },\n    "candidate_rule": {\n        "minimum_stale_from": "labels",\n        "patterns": ["assets/**/edges/**/artifacts/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],\n    },\n    "event_vol": {\n        "minimum_stale_from": "labels",\n        "patterns": ["assets/**/edges/**/artifacts/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],\n    },\n    "barrier_geometry": {\n        "minimum_stale_from": "labels",\n        "patterns": ["assets/**/edges/**/artifacts/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],\n    },\n    "signal_age": {\n        "minimum_stale_from": "quote_executable_label",\n        "patterns": ["assets/**/edges/**/artifacts/**/*exact*execution*", "assets/**/edges/**/artifacts/**/*calibrat*", "assets/**/edges/**/artifacts/**/*threshold*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],\n    },\n    "target_layer": {\n        "minimum_stale_from": "feature_importance_and_modeling",\n        "patterns": ["assets/**/edges/**/artifacts/feature_surgery/**/*", "assets/**/edges/**/artifacts/model/**/*", "assets/**/edges/**/artifacts/oof/**/*", "assets/**/edges/**/artifacts/calibration/**/*", "assets/**/edges/**/artifacts/threshold/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],\n    },\n    "cv_splitter": {\n        "minimum_stale_from": "feature_selection_and_model_cv",\n        "patterns": ["assets/**/edges/**/artifacts/feature_surgery/**/*", "assets/**/edges/**/artifacts/model/**/*", "assets/**/edges/**/artifacts/oof/**/*", "assets/**/edges/**/artifacts/calibration/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],\n    },\n    "ffd": {\n        "minimum_stale_from": "ffd_feature_consumers",\n        "patterns": ["assets/**/ffd/**/*", "assets/**/edges/**/artifacts/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],\n    },\n    "selected_features": {\n        "minimum_stale_from": "model",\n        "patterns": ["assets/**/edges/**/artifacts/model/**/*", "assets/**/edges/**/artifacts/oof/**/*", "assets/**/edges/**/artifacts/calibration/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],\n    },\n    "calibration": {\n        "minimum_stale_from": "calibration",\n        "patterns": ["assets/**/edges/**/artifacts/calibration/**/*", "assets/**/edges/**/artifacts/threshold/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],\n    },\n    "sizing": {\n        "minimum_stale_from": "sizing_threshold_portfolio",\n        "patterns": ["assets/**/edges/**/artifacts/threshold/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],\n    },\n    "portfolio_covariance": {\n        "minimum_stale_from": "portfolio_allocation",\n        "patterns": ["portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],\n    },\n}\n\ndef scan_invalidation(root: str | Path, changes: Iterable[str]) -> dict:\n    base = Path(root).resolve()\n    requested = [str(c) for c in changes]\n    unknown = sorted(set(requested) - set(CHANGE_SCOPES))\n    if unknown:\n        raise MLDPInvalidationError(\n            f"Unknown change keys: {unknown}; allowed={sorted(CHANGE_SCOPES)}"\n        )\n    found: dict[str, list[str]] = {}\n    for change in requested:\n        files: set[str] = set()\n        for pattern in CHANGE_SCOPES[change]["patterns"]:\n            for path in base.glob(pattern):\n                if path.is_file():\n                    files.add(path.relative_to(base).as_posix())\n        found[change] = sorted(files)\n    return {\n        "root": str(base),\n        "changes": requested,\n        "scopes": {\n            c: {\n                "minimum_stale_from": CHANGE_SCOPES[c]["minimum_stale_from"],\n                "candidate_file_count": len(found[c]),\n                "candidate_files": found[c],\n            }\n            for c in requested\n        },\n        "destructive_actions": False,\n    }\n', 'mldp_invalidation_scan.py': '#!/usr/bin/env python3\n"""Report candidate stale artifacts for an MLDP transformation change."""\nfrom __future__ import annotations\nimport argparse\nimport json\nfrom pathlib import Path\nfrom shared.validation.mldp_invalidation import CHANGE_SCOPES, scan_invalidation\n\ndef main() -> None:\n    parser = argparse.ArgumentParser()\n    parser.add_argument("--change", action="append", required=True, choices=sorted(CHANGE_SCOPES))\n    parser.add_argument("--json-out")\n    args = parser.parse_args()\n    report = scan_invalidation(Path(__file__).resolve().parent, args.change)\n    text = json.dumps(report, indent=2)\n    print(text)\n    if args.json_out:\n        Path(args.json_out).write_text(text + "\\n", encoding="utf-8")\n\nif __name__ == "__main__":\n    main()\n', 'config/research_trial_ledger.schema.json': '{\n  "$schema": "https://json-schema.org/draft/2020-12/schema",\n  "title": "Quantara MLDP Research Trial Ledger Row",\n  "type": "object",\n  "required": [\n    "trial_id",\n    "trial_type",\n    "hypothesis_id",\n    "parameters",\n    "data_window",\n    "code_commit",\n    "metric_name",\n    "result_artifacts",\n    "outcome_seen",\n    "selection_influenced",\n    "historical_backfill",\n    "trial_count_quality",\n    "created_at_utc"\n  ],\n  "properties": {\n    "trial_id": {\n      "type": "string",\n      "minLength": 1\n    },\n    "trial_type": {\n      "type": "string",\n      "minLength": 1\n    },\n    "hypothesis_id": {\n      "type": "string",\n      "minLength": 1\n    },\n    "parent_trial_id": {\n      "type": [\n        "string",\n        "null"\n      ]\n    },\n    "parameters": {\n      "type": "object"\n    },\n    "data_window": {\n      "type": "object"\n    },\n    "code_commit": {\n      "type": "string"\n    },\n    "metric_name": {\n      "type": "string"\n    },\n    "result_artifacts": {\n      "type": "array",\n      "items": {\n        "type": "string"\n      }\n    },\n    "outcome_seen": {\n      "type": "boolean"\n    },\n    "selection_influenced": {\n      "type": "boolean"\n    },\n    "historical_backfill": {\n      "type": "boolean"\n    },\n    "trial_count_quality": {\n      "enum": [\n        "exact",\n        "reconstructed",\n        "lower_bound",\n        "unknown"\n      ]\n    },\n    "created_at_utc": {\n      "type": "string"\n    },\n    "notes": {\n      "type": "string"\n    }\n  },\n  "additionalProperties": true\n}\n', 'config/research_trial_ledger.jsonl': '', 'config/mldp_method_registry.json': '{\n  "registry_version": "quantara_mldp_method_registry_v1",\n  "methodological_authorities": [\n    "Marcos Lopez de Prado, Advances in Financial Machine Learning (2018)",\n    "Marcos Lopez de Prado, Machine Learning for Asset Managers (2020)"\n  ],\n  "classification_values": [\n    "MLDP_CANONICAL",\n    "MLDP_LATER_REFINEMENT",\n    "QUANTARA_EXTENSION",\n    "DATA_CONSTRAINED_ADAPTATION"\n  ],\n  "methods": [\n    {\n      "method_id": "afml_dynamic_threshold_triple_barrier",\n      "classification": "MLDP_CANONICAL",\n      "source_reference": "AFML Ch.3 Sections 3.3-3.4",\n      "quantara_status": "PRESERVE_AND_RECERTIFY"\n    },\n    {\n      "method_id": "afml_meta_labeling",\n      "classification": "MLDP_CANONICAL",\n      "source_reference": "AFML Ch.3 Sections 3.6-3.8",\n      "quantara_status": "PRESERVE_SIDE_SPECIALIZATION"\n    },\n    {\n      "method_id": "afml_concurrency_uniqueness_return_attribution",\n      "classification": "MLDP_CANONICAL",\n      "source_reference": "AFML Ch.4",\n      "quantara_status": "PRESERVE"\n    },\n    {\n      "method_id": "afml_purging_and_embargo",\n      "classification": "MLDP_CANONICAL",\n      "source_reference": "AFML Ch.7 Section 7.4",\n      "quantara_status": "TASK1_EXACT_INTERVAL_PRIMITIVE_INSTALLED"\n    },\n    {\n      "method_id": "afml_probability_bet_sizing",\n      "classification": "MLDP_CANONICAL",\n      "source_reference": "AFML Ch.10 Sections 10.3-10.5",\n      "quantara_status": "TASK1_REFERENCE_INSTALLED"\n    },\n    {\n      "method_id": "afml_cpcv",\n      "classification": "MLDP_CANONICAL",\n      "source_reference": "AFML Ch.12",\n      "quantara_status": "PRESERVE_AND_EXTEND_TO_PATH_EVALUATION"\n    },\n    {\n      "method_id": "afml_psr_dsr_backtest_statistics",\n      "classification": "MLDP_CANONICAL",\n      "source_reference": "AFML Ch.14",\n      "quantara_status": "PLANNED"\n    },\n    {\n      "method_id": "afml_hrp",\n      "classification": "MLDP_CANONICAL",\n      "source_reference": "AFML Ch.16",\n      "quantara_status": "PRESERVE_AS_BASELINE"\n    },\n    {\n      "method_id": "mlam_theory_first_research",\n      "classification": "MLDP_LATER_REFINEMENT",\n      "source_reference": "MLAM Ch.1",\n      "quantara_status": "PLANNED_THEORY_DOSSIER"\n    },\n    {\n      "method_id": "mlam_marcenko_pastur_denoising",\n      "classification": "MLDP_LATER_REFINEMENT",\n      "source_reference": "MLAM Ch.2",\n      "quantara_status": "PLANNED"\n    },\n    {\n      "method_id": "mlam_information_theoretic_distance",\n      "classification": "MLDP_LATER_REFINEMENT",\n      "source_reference": "MLAM Ch.3",\n      "quantara_status": "PLANNED_BENCHMARK"\n    },\n    {\n      "method_id": "mlam_onc",\n      "classification": "MLDP_LATER_REFINEMENT",\n      "source_reference": "MLAM Ch.4",\n      "quantara_status": "PLANNED"\n    },\n    {\n      "method_id": "mlam_nco",\n      "classification": "MLDP_LATER_REFINEMENT",\n      "source_reference": "MLAM Ch.7",\n      "quantara_status": "PLANNED_BENCHMARK"\n    },\n    {\n      "method_id": "mlam_testing_set_overfitting",\n      "classification": "MLDP_LATER_REFINEMENT",\n      "source_reference": "MLAM Ch.8",\n      "quantara_status": "TASK1_TRIAL_LEDGER_FOUNDATION_INSTALLED"\n    },\n    {\n      "method_id": "quantara_exact_bidask_execution",\n      "classification": "QUANTARA_EXTENSION",\n      "source_reference": "Quantara production extension; AFML Ch.14 implementation-shortfall principles",\n      "quantara_status": "PRESERVE"\n    },\n    {\n      "method_id": "quantara_four_layer_outcome_ontology",\n      "classification": "QUANTARA_EXTENSION",\n      "source_reference": "Quantara semantic extension separating gross/executable/net/live outcomes",\n      "quantara_status": "TASK1_INSTALLED"\n    },\n    {\n      "method_id": "quantara_empirical_break_even_rescaled_sizing",\n      "classification": "QUANTARA_EXTENSION",\n      "source_reference": "Existing Quantara extension; not canonical AFML Ch.10 transform",\n      "quantara_status": "MUST_BENCHMARK_AGAINST_AFML_REFERENCE"\n    },\n    {\n      "method_id": "quantara_quote_activity_bars",\n      "classification": "DATA_CONSTRAINED_ADAPTATION",\n      "source_reference": "AFML Ch.2 information-driven sampling adapted to quote updates rather than transaction prints",\n      "quantara_status": "PRESERVE_PENDING_DATA_SEMANTICS_AUDIT"\n    }\n  ]\n}\n', 'config/mldp_deviation_register.json': '{\n  "register_version": "quantara_mldp_deviation_register_v1",\n  "generated_by_patch": "quantara_mldp_task1_foundation_v1",\n  "items": [\n    {\n      "id": "D001",\n      "priority": "P0",\n      "title": "Gross-model probability is calibrated against executable target",\n      "status": "OPEN_TASK2",\n      "resolution": "Train and calibrate on one frozen executable target, or explicitly build a separate execution model."\n    },\n    {\n      "id": "D002",\n      "priority": "P0",\n      "title": "Ambiguous realized_r/executed_r ontology",\n      "status": "FOUNDATION_INSTALLED_PIPELINE_MIGRATION_PENDING",\n      "resolution": "Use gross_r, quote_executable_r, net_executable_r, live_realized_r and matching t1/labels."\n    },\n    {\n      "id": "D003",\n      "priority": "P0",\n      "title": "Shared CPCV helper used min/max test-start approximation",\n      "status": "RESOLVED_BY_TASK1",\n      "resolution": "Use exact interval-overlap purge against testing information intervals."\n    },\n    {\n      "id": "D004",\n      "priority": "P1",\n      "title": "Hard-coded 100-row edge embargo lacks methodology certificate",\n      "status": "OPEN",\n      "resolution": "Select/document unit, rationale and leakage sensitivity."\n    },\n    {\n      "id": "D005",\n      "priority": "P0",\n      "title": "No global immutable research-trial authority",\n      "status": "FOUNDATION_INSTALLED_HISTORICAL_BACKFILL_PENDING",\n      "resolution": "Append all new outcome-informed choices; reconstruct old trials conservatively."\n    },\n    {\n      "id": "D006",\n      "priority": "P0",\n      "title": "Barrier/Event_Vol/horizon scans were selected from gross economics",\n      "status": "OPEN_RESCAN_REQUIRED",\n      "resolution": "Re-run theory-constrained executable-economics research with trial counting, stability tests and locked final evaluation."\n    },\n    {\n      "id": "D007",\n      "priority": "P0",\n      "title": "Quantara payoff-adjusted sizing is labelled as AFML in current pipeline",\n      "status": "OPEN",\n      "resolution": "Retain as named Quantara extension; compare OOS with canonical AFML Ch.10 reference."\n    },\n    {\n      "id": "D008",\n      "priority": "P0",\n      "title": "Signal-age contract drifted from intended 120s to 1000s",\n      "status": "RESOLVED_BY_TASK1_SOURCE_CHANGE_ARTIFACT_REBUILD_PENDING",\n      "resolution": "Set dashboard/execution source defaults to 120s and invalidate executable outcomes downstream."\n    },\n    {\n      "id": "D009",\n      "priority": "P1",\n      "title": "FFD minimum-d stationarity/memory decision not fully certified",\n      "status": "OPEN",\n      "resolution": "Persist train-only d candidates, stationarity criterion and memory-retention curve."\n    },\n    {\n      "id": "D010",\n      "priority": "P1",\n      "title": "Feature/portfolio covariance matrices are not MLAM-denoised/ONC governed",\n      "status": "OPEN",\n      "resolution": "Benchmark Marcenko-Pastur denoising, justified detoning, ONC and NCO without replacing HRP blindly."\n    },\n    {\n      "id": "D011",\n      "priority": "P1",\n      "title": "No uniform DSR/PBO/false-discovery promotion firewall",\n      "status": "OPEN",\n      "resolution": "Add trial-aware selection-risk evidence before promotion."\n    },\n    {\n      "id": "D012",\n      "priority": "P1",\n      "title": "AFML portfolio-oversight lifecycle not explicit end to end",\n      "status": "OPEN",\n      "resolution": "Research -> embargo -> paper -> small-capital graduation -> reallocation -> decommission."\n    }\n  ]\n}\n', 'docs/mldp/TRANSFORMATION_STATE.json': '{\n  "state_version": "quantara_mldp_transformation_state_v1",\n  "patch_id": "quantara_mldp_task1_foundation_v1",\n  "reference_repo": "Wells21/trading_models",\n  "reference_branch": "v3",\n  "reference_head_before_task1": "ee3c64b41f5d923825deda112d4e1245a11147c5",\n  "phase": "TASK1_FOUNDATION_INSTALLED",\n  "edge_pipeline_migrated_to_executable_target": false,\n  "current_generation_promotable": false,\n  "historical_trial_ledger_complete": false,\n  "historical_trial_count_quality": "lower_bound_or_unknown_until_backfill",\n  "signal_age_seconds_source_default": 120,\n  "next_task": "TASK2_EXECUTABLE_TARGET_PILOT",\n  "next_pilot_recommendation": "BTCUSD/absorption_long",\n  "notes": [\n    "Task 1 installs contracts and exact CV primitives but does not regenerate edge research artifacts.",\n    "Changing signal age invalidates prior exact-execution artifacts; when executable target moves upstream it invalidates model and all downstream objects.",\n    "Old outcome-informed geometry scans count as research trials and are not pristine OOS evidence.",\n    "Profitability is not guaranteed; a faithful research factory must reject false/uneconomic edges rather than tune until positive."\n  ]\n}\n', 'docs/mldp/QUANTARA_MLDP_DOCTRINE.md': "# Quantara MLDP Doctrine — Foundation v1\n\n## Authority\n\nQuantara's methodological references are Marcos López de Prado's *Advances in\nFinancial Machine Learning* (2018) and *Machine Learning for Asset Managers*\n(2020). The books are a research methodology, not one fixed trading algorithm.\n\n## Non-negotiable research principles\n\n1. **Theory precedes promotion.** A profitable historical path is not a theory.\n   Every promoted edge must state the mechanism, agent/constraint that can fund\n   the edge, regimes, counterfactuals, and falsifiers.\n2. **One probability, one estimand.** Model training, OOF prediction,\n   calibration, thresholding, and sizing may not silently switch outcomes.\n3. **Keep economic layers separate.** Gross research, quote-executable, net\n   simulated executable, and live realized R are different objects.\n4. **Dynamic labels are causal.** Event target volatility and barrier geometry\n   must be known and frozen at event t0.\n5. **Non-IID observations are treated as non-IID.** Use interval-aware purging,\n   justified embargo, concurrency/uniqueness and AFML return-attribution\n   weighting where applicable.\n6. **Backtests do not manufacture discoveries.** Every outcome-informed\n   experiment is a trial. Geometry, model, threshold and allocator searches\n   must be recorded and selection risk quantified.\n7. **Canonical MLDP methods remain visible.** Quantara extensions are named as\n   extensions and benchmarked against the canonical reference.\n8. **Production realism is preserved.** Exact Bid/Ask replay, broker lots,\n   currency conversion, latency controls, MT5 state and prop constraints are\n   legitimate production extensions.\n9. **Failure is a valid result.** The process must be capable of rejecting every\n   candidate. It must never tune an edge until profitability appears.\n10. **Artifact invalidation is dependency-aware.** Upstream semantic changes\n    force regeneration from the earliest affected station.\n\n## Geometry research rule\n\nEvent_Vol lookback, EWM span, PT/SL multipliers and vertical horizon may be\nresearched again after executable-target migration. They must not be selected\nby simply maximizing executable R over a large grid. Candidate ranges must be\njustified by the mechanism/holding horizon, every evaluated point must be\nrecorded as a trial, development evidence must include stability across time,\nregimes and assets where applicable, and one specification must be frozen\nbefore a locked final evaluation.\n\n## Current Task-1 boundary\n\nTask 1 installs the semantic/CV/sizing/trial-accounting foundation. It does not\nyet change edge Step 01-04 target generation. Until Task 2 migrates and rebuilds\na pilot edge, current V3 research artifacts are prior-generation evidence and\nare not newly MLDP-certified.\n", 'tests/test_mldp_outcome_ontology.py': 'import unittest\nimport pandas as pd\nfrom shared.labels.outcome_ontology import (\n    OutcomeOntologyError,\n    canonicalize_legacy_outcomes,\n    validate_layer,\n)\n\nclass TestOutcomeOntology(unittest.TestCase):\n    def test_legacy_aliases_and_label_identity(self):\n        t0 = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")\n        frame = pd.DataFrame({\n            "Datetime": t0,\n            "realized_r": [1.0, -0.2, 0.0],\n            "meta_label": [1, 0, 0],\n            "t1": t0 + pd.Timedelta(hours=1),\n            "executed_r": [0.5, -0.3, 0.0],\n            "executed_meta_label": [1, 0, 0],\n            "exit_time": t0 + pd.Timedelta(minutes=30),\n        })\n        out = canonicalize_legacy_outcomes(frame)\n        self.assertIn("gross_r", out.columns)\n        self.assertIn("quote_executable_r", out.columns)\n        self.assertEqual(validate_layer(out, "gross")["status"], "PASS")\n        self.assertEqual(validate_layer(out, "quote_executable")["status"], "PASS")\n\n    def test_conflicting_alias_fails(self):\n        frame = pd.DataFrame({"realized_r": [1.0], "gross_r": [-1.0]})\n        with self.assertRaises(OutcomeOntologyError):\n            canonicalize_legacy_outcomes(frame)\n\nif __name__ == "__main__":\n    unittest.main()\n', 'tests/test_mldp_financial_cv.py': 'import unittest\nimport numpy as np\nimport pandas as pd\nfrom shared.models.financial_cv import (\n    embargo_after_test_blocks,\n    purge_overlapping_train_indices,\n)\n\nclass TestFinancialCV(unittest.TestCase):\n    def test_exact_disjoint_interval_purge(self):\n        t0 = pd.date_range("2026-01-01", periods=10, freq="h", tz="UTC")\n        t1 = pd.Series(t0 + pd.Timedelta(minutes=30))\n        # Row 5 sits in the gap between test blocks and must survive.\n        # Row 4 starts in the gap but its label remains active into row 7 test time.\n        t1.iloc[4] = t0[8]\n        test_idx = np.array([2, 3, 7])\n        train_idx = np.array([0, 1, 4, 5, 6, 8, 9])\n        kept = purge_overlapping_train_indices(train_idx, test_idx, t0, t1)\n        self.assertNotIn(4, kept.tolist())\n        self.assertIn(5, kept.tolist())\n\n    def test_embargo_is_after_contiguous_test_blocks(self):\n        kept = embargo_after_test_blocks(\n            train_indices=np.array([0, 1, 4, 5, 6, 8, 9]),\n            test_indices=np.array([2, 3, 7]),\n            n_samples=10,\n            embargo_size=2,\n        )\n        self.assertEqual(kept.tolist(), [0, 1, 6])\n\nif __name__ == "__main__":\n    unittest.main()\n', 'tests/test_mldp_afml_ch10_reference.py': 'import unittest\nimport numpy as np\nimport pandas as pd\nfrom shared.sizing.afml_ch10_reference import (\n    average_active_signals,\n    discretize_signal,\n    probability_to_signal,\n)\n\nclass TestAFMLCh10Reference(unittest.TestCase):\n    def test_binary_null_probability_has_zero_size(self):\n        value = probability_to_signal([0.5], [1], num_classes=2)\n        self.assertAlmostEqual(float(value[0]), 0.0, places=12)\n\n    def test_meta_side_changes_sign(self):\n        long = probability_to_signal([0.9], [1], primary_side=[1])\n        short = probability_to_signal([0.9], [1], primary_side=[-1])\n        self.assertGreater(float(long[0]), 0.0)\n        self.assertAlmostEqual(float(long[0]), -float(short[0]), places=12)\n\n    def test_meta_predicted_pass_has_zero_size(self):\n        value = probability_to_signal([0.9], [0], primary_side=[1])\n        self.assertAlmostEqual(float(value[0]), 0.0, places=12)\n\n    def test_active_average_and_discretization(self):\n        idx = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")\n        frame = pd.DataFrame(\n            {\n                "signal": [1.0, -1.0],\n                "t1": [\n                    idx[0] + pd.Timedelta(hours=2),\n                    idx[1] + pd.Timedelta(hours=2),\n                ],\n            },\n            index=idx,\n        )\n        avg = average_active_signals(frame)\n        self.assertAlmostEqual(float(avg.loc[idx[1]]), 0.0)\n        self.assertAlmostEqual(\n            float(avg.loc[idx[0] + pd.Timedelta(hours=2)]), -1.0\n        )\n        disc = discretize_signal(np.array([0.13, -0.27]), 0.1)\n        np.testing.assert_allclose(disc, [0.1, -0.3], atol=1e-12)\n\nif __name__ == "__main__":\n    unittest.main()\n', 'tests/test_mldp_research_trial_ledger.py': 'import tempfile\nimport unittest\nfrom pathlib import Path\nfrom shared.research.trial_ledger import append_trial, build_trial_record, read_trials\n\nclass TestResearchTrialLedger(unittest.TestCase):\n    def test_append_only_round_trip(self):\n        with tempfile.TemporaryDirectory() as td:\n            path = Path(td) / "ledger.jsonl"\n            row = build_trial_record(\n                trial_type="geometry_grid_point",\n                hypothesis_id="H-001",\n                parameters={"H": 10, "EWM": 50, "pt": 1.0, "sl": 1.0},\n                data_window={"start": "2024-01-01", "end": "2024-12-31"},\n                code_commit="abc123",\n                metric_name="quote_executable_mean_r",\n                outcome_seen=True,\n                selection_influenced=False,\n            )\n            append_trial(path, row)\n            rows = read_trials(path)\n            self.assertEqual(len(rows), 1)\n            self.assertEqual(rows[0]["trial_id"], row["trial_id"])\n\nif __name__ == "__main__":\n    unittest.main()\n', 'tests/test_mldp_conformity.py': 'import unittest\nfrom shared.validation.mldp_conformity import (\n    MLDPConformityError,\n    assert_same_target,\n    probability_target_contract,\n)\n\nclass TestMLDPConformity(unittest.TestCase):\n    def test_same_target_passes(self):\n        a = probability_target_contract(\n            "quote_executable", probability_column="raw_probability"\n        )\n        b = probability_target_contract(\n            "quote_executable", probability_column="calibrated_probability"\n        )\n        self.assertEqual(assert_same_target(a, b)["status"], "PASS")\n\n    def test_target_switch_fails(self):\n        gross = probability_target_contract(\n            "gross", probability_column="raw_probability"\n        )\n        exe = probability_target_contract(\n            "quote_executable", probability_column="calibrated_probability"\n        )\n        with self.assertRaises(MLDPConformityError):\n            assert_same_target(gross, exe)\n\nif __name__ == "__main__":\n    unittest.main()\n'}

# ---------------------------------------------------------------------------
# Patcher implementation
# ---------------------------------------------------------------------------

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PATCH_ID = "quantara_mldp_task1_foundation_v1"
PATCH_MARKER = "# QUANTARA_MLDP_TASK1_FOUNDATION_V1"
EXPECTED_BRANCH = "v3"
REFERENCE_HEAD = "ee3c64b41f5d923825deda112d4e1245a11147c5"
EXPECTED_BLOBS = {
    "shared/models/cpcv_training.py": "1754818daec2d9867be2d9b6736b5807744cbd61",
    "portfolio_live/live_settings.py": "28272f8ff4f2bafab155990f4999acb72f35cf2d",
}

class PatchError(RuntimeError):
    pass

def now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()

def run_capture(args: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        args, cwd=str(cwd), text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False,
    )
    return proc.returncode, proc.stdout.strip()

def locate_v2_root() -> Path:
    here = Path.cwd().resolve()
    script_parent = Path(__file__).resolve().parent
    candidates = [
        here, here / "trading_models_v2",
        script_parent, script_parent / "trading_models_v2",
    ]
    for path in candidates:
        if (
            (path / "shared").is_dir()
            and (path / "portfolio_live" / "live_settings.py").is_file()
            and (path / "config" / "edge_universe.json").is_file()
        ):
            return path
    raise PatchError(
        "Could not locate trading_models_v2 root. Run this file from the repo "
        "root or from trading_models_v2."
    )

def ensure_git_context(root: Path, args: argparse.Namespace) -> None:
    rc, _ = run_capture(["git", "rev-parse", "--show-toplevel"], root)
    if rc != 0:
        raise PatchError("This patcher must run inside the Quantara Git checkout.")
    rc, branch = run_capture(["git", "branch", "--show-current"], root)
    if rc != 0:
        raise PatchError("Could not determine current Git branch.")
    if branch != EXPECTED_BRANCH and not args.allow_branch:
        raise PatchError(
            f"Expected branch {EXPECTED_BRANCH!r}, found {branch!r}. "
            "Use --allow-branch only if deliberate."
        )
    rc, head = run_capture(["git", "rev-parse", "HEAD"], root)
    if rc == 0:
        print(f"Git branch: {branch}")
        print(f"Git HEAD  : {head}")
        if head != REFERENCE_HEAD:
            print(
                f"NOTE: HEAD differs from Task-1 reference {REFERENCE_HEAD}. "
                "Target-file blob guards still protect source edits."
            )
    rc, dirty = run_capture(
        ["git", "status", "--porcelain", "--untracked-files=no"], root
    )
    if rc != 0:
        raise PatchError("Could not inspect tracked working-tree status.")
    if dirty and not args.allow_dirty:
        raise PatchError(
            "Tracked working tree is not clean. Commit/stash first, or use "
            "--allow-dirty only after reviewing the risk.\n" + dirty
        )

def verify_source_blobs(root: Path, args: argparse.Namespace) -> None:
    for rel, expected in EXPECTED_BLOBS.items():
        path = root / rel
        if not path.exists():
            raise PatchError(f"Required source file missing: {rel}")
        text = path.read_text(encoding="utf-8")
        if PATCH_MARKER in text:
            continue
        actual = git_blob_sha(path)
        if actual != expected and not args.allow_source_drift:
            raise PatchError(
                f"{rel}: source blob drifted.\nExpected: {expected}\nActual  : {actual}\n"
                "Refusing anchor-based modification. Re-audit source or use "
                "--allow-source-drift only if exact anchors remain valid."
            )

def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected exactly one anchor, found {count}.")
    return text.replace(old, new, 1)

def patch_cpcv(text: str) -> str:
    if PATCH_MARKER in text:
        return text
    import_anchor = """try:
    from shared.models.afml_sample_weights import compute_afml_event_weight_table
except ImportError:  # direct execution from shared/models
    from afml_sample_weights import compute_afml_event_weight_table
"""
    import_new = import_anchor + """
try:
    from shared.models.financial_cv import (
        embargo_after_test_blocks,
        purge_overlapping_train_indices,
    )
except ImportError:  # direct execution from shared/models
    from financial_cv import embargo_after_test_blocks, purge_overlapping_train_indices

""" + PATCH_MARKER + "\n"
    text = replace_once(text, import_anchor, import_new, label="cpcv import")

    old_purge = """    def _purge_mask(self, train_mask: np.ndarray, test_indices: np.ndarray) -> np.ndarray:
        if self.t1 is None or self.time_index is None:
            return train_mask
        t0 = self.time_index.reset_index(drop=True)
        t1 = self.t1.reset_index(drop=True)
        if len(t0) != len(train_mask) or len(t1) != len(train_mask):
            raise CPCVTrainingError("t1/time_index length must match input rows")
        test_starts = t0.iloc[test_indices]
        test_start_min = test_starts.min()
        test_start_max = test_starts.max()
        if pd.isna(test_start_min) or pd.isna(test_start_max):
            raise CPCVTrainingError("Test start times contain NaT; cannot purge safely")
        train_idx = np.flatnonzero(train_mask)
        overlap = (t0.iloc[train_idx] <= test_start_max).to_numpy() & (t1.iloc[train_idx] >= test_start_min).to_numpy()
        train_mask[train_idx[overlap]] = False
        return train_mask
"""
    new_purge = """    def _purge_mask(self, train_mask: np.ndarray, test_indices: np.ndarray) -> np.ndarray:
        if self.t1 is None or self.time_index is None:
            return train_mask
        t0 = self.time_index.reset_index(drop=True)
        t1 = self.t1.reset_index(drop=True)
        if len(t0) != len(train_mask) or len(t1) != len(train_mask):
            raise CPCVTrainingError("t1/time_index length must match input rows")
        train_idx = np.flatnonzero(train_mask)
        try:
            kept = purge_overlapping_train_indices(train_idx, test_indices, t0, t1)
        except Exception as exc:
            raise CPCVTrainingError(f"Exact AFML interval purge failed: {exc}") from exc
        out = np.zeros(len(train_mask), dtype=bool)
        out[kept] = True
        return out
"""
    text = replace_once(text, old_purge, new_purge, label="cpcv exact purge")

    old_embargo = """    def _embargo_mask(self, train_mask: np.ndarray, test_indices: np.ndarray, n_samples: int) -> np.ndarray:
        if self.embargo_size <= 0:
            return train_mask
        for idx in test_indices:
            start = int(idx) + 1
            stop = min(n_samples, start + self.embargo_size)
            train_mask[start:stop] = False
        return train_mask
"""
    new_embargo = """    def _embargo_mask(self, train_mask: np.ndarray, test_indices: np.ndarray, n_samples: int) -> np.ndarray:
        if self.embargo_size <= 0:
            return train_mask
        train_idx = np.flatnonzero(train_mask)
        try:
            kept = embargo_after_test_blocks(
                train_idx, test_indices, n_samples, self.embargo_size
            )
        except Exception as exc:
            raise CPCVTrainingError(f"AFML test-block embargo failed: {exc}") from exc
        out = np.zeros(n_samples, dtype=bool)
        out[kept] = True
        return out
"""
    text = replace_once(text, old_embargo, new_embargo, label="cpcv block embargo")
    return text

def patch_live_settings(text: str) -> str:
    if PATCH_MARKER in text:
        if (
            "PORTFOLIO_DASHBOARD_MAX_SIGNAL_AGE_SECONDS = 120" not in text
            or "PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS = 120" not in text
        ):
            raise PatchError(
                "live_settings.py carries Task-1 marker but signal ages are not both 120."
            )
        return text
    text = replace_once(
        text,
        "PORTFOLIO_DASHBOARD_MAX_SIGNAL_AGE_SECONDS = 1000",
        PATCH_MARKER + "\nPORTFOLIO_DASHBOARD_MAX_SIGNAL_AGE_SECONDS = 120",
        label="dashboard signal age",
    )
    text = replace_once(
        text,
        "PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS = 1000",
        "PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS = 120",
        label="execution signal age",
    )
    return text

def compile_python(text: str, path: Path) -> None:
    try:
        ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        raise PatchError(f"Python source would not parse: {path}: {exc}") from exc

def check_new_file_conflicts(root: Path) -> None:
    for rel, content in NEW_FILES.items():
        path = root / rel
        if not path.exists():
            continue
        existing = path.read_text(encoding="utf-8")
        if existing != content:
            raise PatchError(
                f"Task-1 path already exists with different content: {rel}. "
                "Refusing to overwrite an unknown implementation."
            )

def validate_all_generated_python(root: Path, cpcv_text: str, live_text: str) -> None:
    for rel, content in NEW_FILES.items():
        if rel.endswith(".py"):
            compile_python(content, root / rel)
    compile_python(cpcv_text, root / "shared/models/cpcv_training.py")
    compile_python(live_text, root / "portfolio_live/live_settings.py")

def print_plan(root: Path, cpcv_changed: bool, live_changed: bool) -> None:
    print("\nTask-1 plan")
    print("-----------")
    print(f"Root: {root}")
    print(f"Add/verify {len(NEW_FILES)} foundation files.")
    print(f"Patch shared/models/cpcv_training.py : {cpcv_changed}")
    print(f"Patch portfolio_live/live_settings.py: {live_changed}")
    print("Git operations                         : NONE")
    print("Research artifact regeneration        : NONE in Task 1")
    print("\nImmediate invalidation:")
    print("- 1000s -> 120s signal-age source contract makes prior exact-execution artifacts stale.")
    print("- Task 2 executable-target migration will invalidate feature/model/calibration downstream.")
    print("- Existing geometry scans remain historical trials, not pristine OOS evidence.")

def local_override_warning(root: Path) -> None:
    path = root / "portfolio_live/local_live_settings.json"
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        print(
            "WARNING: local_live_settings.json exists but could not be parsed; "
            "inspect it manually."
        )
        return
    keys = [
        "PORTFOLIO_DASHBOARD_MAX_SIGNAL_AGE_SECONDS",
        "PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS",
    ]
    overrides = {k: payload.get(k) for k in keys if k in payload}
    if overrides:
        print(
            "WARNING: local_live_settings.json overrides signal age. The patcher "
            "does NOT edit ignored/private settings. Review: "
            + json.dumps(overrides, sort_keys=True)
        )

def write_transactionally(root: Path, cpcv_text: str, live_text: str) -> Path:
    backup_root = (
        root / ".quantara_patch_backups" / f"{PATCH_ID}_{now_tag()}"
    )
    backup_root.mkdir(parents=True, exist_ok=False)
    modified_existing = [
        Path("shared/models/cpcv_training.py"),
        Path("portfolio_live/live_settings.py"),
    ]
    created: list[Path] = []
    try:
        for rel in modified_existing:
            src = root / rel
            dst = backup_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        for rel, content in NEW_FILES.items():
            path = root / rel
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
            created.append(Path(rel))

        (root / "shared/models/cpcv_training.py").write_text(
            cpcv_text, encoding="utf-8", newline="\n"
        )
        (root / "portfolio_live/live_settings.py").write_text(
            live_text, encoding="utf-8", newline="\n"
        )

        rc, out = run_capture(
            [
                sys.executable, "-m", "unittest", "discover",
                "-s", "tests", "-p", "test_mldp_*.py", "-v",
            ],
            root,
        )
        print("\nFocused Task-1 tests")
        print("--------------------")
        print(out)
        if rc != 0:
            raise PatchError("Focused Task-1 unit tests failed.")

        rc, out = run_capture(
            [sys.executable, "shared/models/cpcv_training.py"], root
        )
        print("\nShared CPCV self-test")
        print("---------------------")
        print(out)
        if rc != 0:
            raise PatchError("Patched shared CPCV self-test failed.")

        report = {
            "patch_id": PATCH_ID,
            "applied_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "backup_root": str(backup_root.relative_to(root)),
            "signal_age_seconds": 120,
            "task1_tests": "PASS",
            "git_actions": "NONE",
            "edge_artifacts_regenerated": False,
        }
        report_path = root / "docs/mldp/task1_patch_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nPatch report: {report_path.relative_to(root)}")
        return backup_root
    except Exception:
        print("\nERROR during apply: rolling back Task-1 filesystem changes...")
        for rel in modified_existing:
            backup = backup_root / rel
            if backup.exists():
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
        for rel in created:
            try:
                (root / rel).unlink()
            except FileNotFoundError:
                pass
        try:
            (root / "docs/mldp/task1_patch_report.json").unlink()
        except FileNotFoundError:
            pass
        raise

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install Quantara MLDP Task-1 foundation locally."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate and print plan without writing files."
    )
    parser.add_argument(
        "--allow-dirty", action="store_true",
        help="Allow tracked local changes. Not recommended."
    )
    parser.add_argument(
        "--allow-branch", action="store_true",
        help="Allow branch other than v3."
    )
    parser.add_argument(
        "--allow-source-drift", action="store_true",
        help="Allow target blob mismatch if exact anchors still match."
    )
    args = parser.parse_args()

    root = locate_v2_root()
    ensure_git_context(root, args)
    verify_source_blobs(root, args)
    check_new_file_conflicts(root)

    cpcv_path = root / "shared/models/cpcv_training.py"
    live_path = root / "portfolio_live/live_settings.py"
    cpcv_before = cpcv_path.read_text(encoding="utf-8")
    live_before = live_path.read_text(encoding="utf-8")
    cpcv_after = patch_cpcv(cpcv_before)
    live_after = patch_live_settings(live_before)

    validate_all_generated_python(root, cpcv_after, live_after)
    print_plan(root, cpcv_after != cpcv_before, live_after != live_before)
    local_override_warning(root)

    if args.dry_run:
        print("\nDRY RUN PASS — no files were changed.")
        return

    backup = write_transactionally(root, cpcv_after, live_after)
    print("\nTASK 1 FOUNDATION APPLIED SUCCESSFULLY")
    print(f"Backup: {backup.relative_to(root)}")
    print("No git add/commit/push was executed.")
    print("\nNext action:")
    print("1. Inspect `git diff -- trading_models_v2` from repo root.")
    print("2. Send the full patcher console output back to ChatGPT.")
    print("3. Do NOT rerun edge Step 01-09 yet; Task 2 moves the executable target upstream first.")

if __name__ == "__main__":
    try:
        main()
    except PatchError as exc:
        print(f"\nPATCH FAILED: {exc}", file=sys.stderr)
        raise SystemExit(2)
