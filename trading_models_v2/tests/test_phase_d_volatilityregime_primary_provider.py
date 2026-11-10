from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.edges.primary_side_continuations import create_primary_side_provider
from shared.edges.primary_side_continuations.providers import volatilityregime

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d1 = json.loads((ROOT/"portfolio_live/config/phase_d/candidate_contract.json").read_text(encoding="utf-8-sig"))
        cls.edges = [e for e in d1["edges"] if (e.get("primary_side") or {}).get("primary_logic_signature") in volatilityregime.PROVIDER_SPEC["supported_signatures"]]
        if not cls.edges:
            raise unittest.SkipTest("no live edges currently use the volatilityregime_long primary-side family")

    def test_all_members_bind(self):
        self.assertGreater(len(self.edges), 0)
        for e in self.edges:
            p = create_primary_side_provider(volatilityregime.VOLATILITYREGIME_LONG_PRIMARY_SIGNATURE, d1_edge=e, v2_root=str(ROOT))
            self.assertEqual(p.identity.edge_id, e["edge_id"])

    def test_accept_matches_source(self):
        e = self.edges[0]
        primary = e["primary_side"]
        mod = load(ROOT/primary["path"], "volreg_primary_oracle")
        p = create_primary_side_provider(volatilityregime.VOLATILITYREGIME_LONG_PRIMARY_SIGNATURE, d1_edge=e, v2_root=str(ROOT))
        ts = pd.Timestamp("2026-08-18T12:00:00Z")
        p.bootstrap(pd.DataFrame({"Datetime":[ts]}))
        event = {
            "Datetime":ts, "Bar_Start":ts, "edge_id":e["edge_id"], "side":0,
            "Event_Vol":0.01, "event_id":"evt", "candidate_event_generator":"raw",
            "Vol_Regime_Event":1, "Current_Vol_Ratio":1.4, "Was_Compressed":1,
            "Vol_Expansion":1, "Body_Direction":1, "Body_Share":0.35,
            "Close_Location":0.75, "event_known_after_close":True,
            "uses_future_information":False,
        }
        fname = [n for n in primary["runtime_called_functions"] if n.startswith("filter_")][0]
        expected = getattr(mod, fname)(pd.DataFrame([event])).reset_index(drop=True)
        actual_event = p.filter_event(event)
        actual = pd.DataFrame([actual_event], columns=list(expected.columns))
        pd.testing.assert_frame_equal(expected, actual, check_dtype=False, check_exact=False)

    def test_rejects_bearish(self):
        e = self.edges[0]
        p = create_primary_side_provider(volatilityregime.VOLATILITYREGIME_LONG_PRIMARY_SIGNATURE, d1_edge=e, v2_root=str(ROOT))
        ts = pd.Timestamp("2026-08-18T12:00:00Z")
        p.bootstrap(pd.DataFrame({"Datetime":[ts]}))
        event = {
            "Datetime":ts, "edge_id":e["edge_id"], "side":0, "Event_Vol":0.01,
            "Vol_Regime_Event":1, "Current_Vol_Ratio":1.4, "Was_Compressed":1,
            "Vol_Expansion":1, "Body_Direction":-1, "Body_Share":0.35,
            "Close_Location":0.75,
        }
        self.assertIsNone(p.filter_event(event))

    def test_state(self):
        e = self.edges[0]
        p = create_primary_side_provider(volatilityregime.VOLATILITYREGIME_LONG_PRIMARY_SIGNATURE, d1_edge=e, v2_root=str(ROOT))
        dt = pd.date_range("2026-01-01T00:00:00Z", periods=1000, freq="min")
        p.bootstrap(pd.DataFrame({"Datetime":dt}))
        s = p.state_summary()
        self.assertEqual(s["processed_rows"], 1000)
        self.assertFalse(s["normal_path_ffd_disk_reads"])
        self.assertFalse(s["normal_path_history_disk_reads"])
        self.assertFalse(s["normal_path_full_primary_fallback"])

if __name__ == "__main__":
    unittest.main()
