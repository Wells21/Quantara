from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.candidate_continuations.providers import rangestructure_short
from shared.edges.primary_side_continuations import create_primary_side_provider
from shared.edges.primary_side_continuations.providers import rangestructure_short as primary_mod

def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

def _features(rows=1700):
    i = np.arange(rows, dtype="float64")
    regime = np.where(((i.astype("int64") // 260) % 2) == 0, 1.0, -1.0)
    step = regime * 0.00032 + 0.00004 * np.sin(i / 13.0)
    close = np.exp(np.log(100.0) + np.cumsum(step))
    open_ = close / np.exp(0.45 * step)
    high = np.maximum(open_, close) * 1.0008
    low = np.minimum(open_, close) * 0.9992
    dt = pd.date_range("2026-01-01T00:00:00Z", periods=rows, freq="min")
    j = np.arange(rows, dtype="int64")
    return pd.DataFrame({
        "Datetime":dt,"Bar_Start":dt,"Open":open_,"High":high,"Low":low,"Close":close,
        "Regime_State":(j%4).astype(float),"Regime_Entropy":0.5,
        "Regime_Duration":(j%20).astype(float),
        "Prob_State_0":0.1,"Prob_State_1":0.2,"Prob_State_2":0.3,"Prob_State_3":0.4,
    })

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d1 = json.loads((ROOT/"portfolio_live/config/phase_d/candidate_contract.json").read_text(encoding="utf-8-sig"))
        cls.edges = [e for e in d1["edges"] if (e.get("candidate") or {}).get("candidate_logic_signature") in rangestructure_short.PROVIDER_SPEC["supported_signatures"]]
        cls.primary_edges = [e for e in d1["edges"] if (e.get("primary_side") or {}).get("primary_logic_signature") in primary_mod.PROVIDER_SPEC["supported_signatures"]]
        if not cls.edges:
            raise unittest.SkipTest("no live edges currently use the rangestructure_short candidate family")

    def test_all_members_bind(self):
        self.assertGreater(len(self.edges), 0)
        for e in self.edges:
            p = create_candidate_provider(
                rangestructure_short.RANGESTRUCTURE_SHORT_SIGNATURE,
                d1_edge=e, v2_root=str(ROOT)
            )
            self.assertEqual(p.identity.edge_id, e["edge_id"])

    def test_candidate_source_replay_parity(self):
        e = self.edges[0]
        c = e["candidate"]
        oracle = _load(ROOT/c["path"], "rs_short_oracle")
        entry = c["runtime_entry_functions"][0]
        provider = create_candidate_provider(
            rangestructure_short.RANGESTRUCTURE_SHORT_SIGNATURE,
            d1_edge=e, v2_root=str(ROOT)
        )
        x = _features()
        expected = getattr(oracle, entry)(x.copy()).reset_index(drop=True)
        actual = provider.replay(x.copy()).reset_index(drop=True)
        self.assertGreater(len(expected), 0)
        self.assertEqual(list(expected.columns), list(actual.columns))
        pd.testing.assert_frame_equal(expected, actual, check_dtype=False, check_exact=False, rtol=3e-11, atol=3e-11)

    def test_restart_parity(self):
        e = self.edges[0]
        p1 = create_candidate_provider(rangestructure_short.RANGESTRUCTURE_SHORT_SIGNATURE, d1_edge=e, v2_root=str(ROOT))
        p2 = create_candidate_provider(rangestructure_short.RANGESTRUCTURE_SHORT_SIGNATURE, d1_edge=e, v2_root=str(ROOT))
        x = _features(1900)
        all_events = p1.replay(x)
        expected = all_events[all_events["Datetime"].isin(x["Datetime"].iloc[1550:])].reset_index(drop=True)
        p2.bootstrap(x.iloc[:1550])
        rows=[]
        for _, row in x.iloc[1550:].iterrows():
            ev=p2.update_one(row)
            if ev is not None: rows.append(ev)
        actual=pd.DataFrame(rows, columns=list(all_events.columns)).reset_index(drop=True)
        pd.testing.assert_frame_equal(expected, actual, check_dtype=False, check_exact=False, rtol=3e-11, atol=3e-11)

    def test_primary_source_parity(self):
        e = self.primary_edges[0]
        primary = e["primary_side"]
        oracle = _load(ROOT/primary["path"], "rs_short_primary_oracle")
        provider = create_primary_side_provider(
            primary_mod.RANGESTRUCTURE_SHORT_PRIMARY_SIGNATURE,
            d1_edge=e, v2_root=str(ROOT)
        )
        ts=pd.Timestamp("2026-08-18T12:00:00Z")
        provider.bootstrap(pd.DataFrame({"Datetime":[ts]}))
        event={
            "Datetime":ts,"Bar_Start":ts,"edge_id":e["edge_id"],"side":-1,
            "Event_Vol":0.01,"Range_Structure_Event_Type":1,
            "Structure_Bias_TStat":-1.2,
        }
        fname=[n for n in primary["runtime_called_functions"] if n.startswith("filter_")][0]
        expected=getattr(oracle,fname)(pd.DataFrame([event])).reset_index(drop=True)
        actual_event=provider.filter_event(event)
        actual=pd.DataFrame([actual_event],columns=list(expected.columns))
        pd.testing.assert_frame_equal(expected,actual,check_dtype=False,check_exact=False)

if __name__=="__main__":
    unittest.main()
