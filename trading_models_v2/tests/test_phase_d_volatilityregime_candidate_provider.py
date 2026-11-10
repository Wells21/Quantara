from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.candidate_continuations.providers import volatilityregime

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def features(rows=1600):
    i = np.arange(rows, dtype="int64")
    step = 0.00018*np.sin(i/17.0) + 0.00007*np.cos(i/31.0)
    close = np.exp(np.log(100.0) + np.cumsum(step))
    body_bias = np.where(i % 2 == 0, 0.00025, -0.00015)
    open_ = close / (1.0 + body_bias)
    high = np.maximum(open_, close) * 1.0012
    low = np.minimum(open_, close) * 0.9988
    bar_vol = np.ones(rows)
    bar_vol[(i % 17) == 0] = 1.35
    bar_vol[(i % 43) == 0] = 1.55
    atr = np.full(rows, 0.35)
    dt = pd.date_range("2026-01-01T00:00:00Z", periods=rows, freq="min")
    return pd.DataFrame({
        "Datetime":dt, "Bar_Start":dt, "Open":open_, "High":high, "Low":low,
        "Close":close, "Bar_Volatility_Bps":bar_vol, "ATR_14":atr, "NATR":atr/close,
        "Body_Share":np.abs(close-open_)/(high-low),
        "Regime_State":(i%4).astype(float), "Regime_Entropy":0.5+0.01*(i%10),
        "Regime_Duration":(i%20).astype(float),
        "Prob_State_0":0.1, "Prob_State_1":0.2, "Prob_State_2":0.3, "Prob_State_3":0.4,
    })

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d1 = json.loads((ROOT/"portfolio_live/config/phase_d/candidate_contract.json").read_text(encoding="utf-8-sig"))
        cls.edges = [e for e in d1["edges"] if (e.get("candidate") or {}).get("candidate_logic_signature") in volatilityregime.PROVIDER_SPEC["supported_signatures"]]
        if not cls.edges:
            raise unittest.SkipTest("no live edges currently use the volatilityregime_long candidate family")

    def pair(self):
        e = self.edges[0]
        c = e["candidate"]
        mod = load(ROOT/c["path"], "volreg_oracle")
        provider = create_candidate_provider(volatilityregime.VOLATILITYREGIME_LONG_SIGNATURE, d1_edge=e, v2_root=str(ROOT))
        return getattr(mod, c["runtime_entry_functions"][0]), provider

    def test_all_members_bind(self):
        self.assertGreater(len(self.edges), 0)
        for e in self.edges:
            p = create_candidate_provider(volatilityregime.VOLATILITYREGIME_LONG_SIGNATURE, d1_edge=e, v2_root=str(ROOT))
            self.assertEqual(p.identity.edge_id, e["edge_id"])

    def test_source_replay_parity(self):
        oracle, provider = self.pair()
        x = features()
        expected = oracle(x.copy()).reset_index(drop=True)
        actual = provider.replay(x.copy()).reset_index(drop=True)
        self.assertGreater(len(expected), 0)
        self.assertEqual(list(expected.columns), list(actual.columns))
        pd.testing.assert_frame_equal(expected, actual, check_dtype=False, check_exact=False, rtol=2e-11, atol=2e-11)

    def test_restart_parity(self):
        _, continuous = self.pair()
        _, restarted = self.pair()
        x = features(1800)
        all_events = continuous.replay(x)
        expected = all_events[all_events["Datetime"].isin(x["Datetime"].iloc[1500:])].reset_index(drop=True)
        restarted.bootstrap(x.iloc[:1500])
        rows = []
        for _, row in x.iloc[1500:].iterrows():
            ev = restarted.update_one(row)
            if ev is not None:
                rows.append(ev)
        actual = pd.DataFrame(rows).reset_index(drop=True)
        self.assertEqual(list(expected.columns), list(actual.columns))
        pd.testing.assert_frame_equal(expected, actual, check_dtype=False, check_exact=False, rtol=2e-11, atol=2e-11)

    def test_bounded(self):
        _, p = self.pair()
        p.replay(features(5000))
        s = p.state_summary()
        self.assertLessEqual(s["volatility_history_rows"], max(p.vol_lookback, p.recent_low_vol_lookback))
        self.assertLessEqual(s["close_history_rows"], p.event_vol_lookback)
        self.assertFalse(s["normal_path_history_disk_reads"])
        self.assertFalse(s["normal_path_full_candidate_fallback"])

if __name__ == "__main__":
    unittest.main()
