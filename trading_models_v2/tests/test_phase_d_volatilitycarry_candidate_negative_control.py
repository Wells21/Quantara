from __future__ import annotations

"""INTENTIONAL FAILURE CONTROL.

This script must exit nonzero. It compares authoritative source events with the
Phase D provider after deliberately corrupting one provider Event_Vol value.
If this script exits zero, the parity harness is not detecting divergence.
"""

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.candidate_continuations.providers import volatilitycarry


def _load_source(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load source {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _features(rows: int = 1800) -> pd.DataFrame:
    i = np.arange(rows, dtype="float64")
    block = (i.astype("int64") // 300) % 2
    direction = np.where(block == 0, 1.0, -1.0)
    step = direction * 0.00030 + 0.000025 * np.sin(i / 8.0)
    close = np.exp(np.log(100.0) + np.cumsum(step))
    open_ = close / np.exp(0.4 * step)
    high = np.maximum(open_, close) * 1.0007
    low = np.minimum(open_, close) * 0.9993
    bar_vol = np.full(rows, 1.0)
    bar_vol[(i.astype("int64") % 11) == 0] = 1.8
    bar_vol[(i.astype("int64") % 7) == 0] = 0.7
    dt = pd.date_range(
        "2026-01-01T00:00:00Z",
        periods=rows,
        freq="min",
    )
    j = np.arange(rows, dtype="int64")
    return pd.DataFrame(
        {
            "Datetime": dt,
            "Bar_Start": dt,
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Bar_Volatility_Bps": bar_vol,
            "Regime_State": (j % 4).astype(float),
            "Regime_Entropy": 0.5 + 0.01 * (j % 10),
            "Regime_Duration": (j % 20).astype(float),
            "Prob_State_0": 0.1,
            "Prob_State_1": 0.2,
            "Prob_State_2": 0.3,
            "Prob_State_3": 0.4,
        }
    )


def main() -> None:
    d1 = json.loads(
        (
            ROOT / "portfolio_live/config/phase_d/candidate_contract.json"
        ).read_text(encoding="utf-8-sig")
    )

    members = [
        edge
        for edge in d1.get("edges") or []
        if str(
            (edge.get("candidate") or {}).get("candidate_logic_signature")
            or ""
        )
        == volatilitycarry.VOLATILITYCARRY_LONG_SIGNATURE
    ]
    if not members:
        raise RuntimeError("No volatilitycarry_long D.1 members found")

    edge = members[0]
    candidate = edge["candidate"]
    module = _load_source(
        ROOT / candidate["path"],
        "d4_volatilitycarry_negative_control_oracle",
    )
    entry_names = candidate.get("runtime_entry_functions") or []
    if len(entry_names) != 1:
        raise RuntimeError(f"Unexpected runtime entries: {entry_names!r}")

    provider = create_candidate_provider(
        volatilitycarry.VOLATILITYCARRY_LONG_SIGNATURE,
        d1_edge=edge,
        v2_root=str(ROOT),
    )

    features = _features()
    expected = getattr(module, entry_names[0])(
        features.copy()
    ).reset_index(drop=True)
    actual = provider.replay(features.copy()).reset_index(drop=True)

    if expected.empty or actual.empty:
        raise RuntimeError(
            "Negative control requires at least one comparable event"
        )

    # Deliberately corrupt a load-bearing parity field by a huge amount relative
    # to the real test tolerance. The equality assertion below MUST fail.
    actual.loc[0, "Event_Vol"] = float(actual.loc[0, "Event_Vol"]) + 0.123456789

    print("INTENTIONAL NEGATIVE CONTROL")
    print("Edge:", edge["edge_id"])
    print("Mutated field: Event_Vol row 0")
    print("Expected behavior: pandas parity assertion FAILS now.")
    print()

    pd.testing.assert_frame_equal(
        expected,
        actual,
        check_dtype=False,
        check_exact=False,
        rtol=3e-11,
        atol=3e-11,
    )

    raise RuntimeError(
        "NEGATIVE CONTROL BROKEN: corrupted provider output incorrectly passed parity"
    )


if __name__ == "__main__":
    main()
