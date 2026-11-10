from __future__ import annotations
import importlib.util, json, sys, unittest
from pathlib import Path
import numpy as np, pandas as pd

ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT.parent):
    if str(p) not in sys.path: sys.path.insert(0,str(p))

from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.primary_side_continuations import create_primary_side_provider
from shared.edges.candidate_continuations.providers import reversal
from shared.edges.primary_side_continuations.providers import reversal as reversal_primary

def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def features(rows=1400):
    i=np.arange(rows,dtype="float64")
    # Smooth oscillation plus sparse shocks creates both overbought and oversold z events.
    close=100.0+0.8*np.sin(i/19.0)+0.25*np.sin(i/5.0)
    close[(i.astype(int)%211)==0]-=1.4
    close[(i.astype(int)%307)==0]+=1.2
    open_=close+0.05*np.sin(i/3.0)
    high=np.maximum(open_,close)+0.20
    low=np.minimum(open_,close)-0.20
    dt=pd.date_range("2026-01-01T00:00:00Z",periods=rows,freq="min")
    j=np.arange(rows)
    return pd.DataFrame({
        "Datetime":dt,"Bar_Start":dt,"Open":open_,"High":high,"Low":low,"Close":close,
        "Regime_State":(j%4).astype(float),"Regime_Entropy":0.5,
        "Regime_Duration":(j%20).astype(float),"Prob_State_0":0.1,
        "Prob_State_1":0.2,"Prob_State_2":0.3,"Prob_State_3":0.4,
    })

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d1=json.loads((ROOT/"portfolio_live/config/phase_d/candidate_contract.json").read_text(encoding="utf-8-sig"))
        cls.edges=[e for e in cls.d1["edges"] if (e.get("candidate") or {}).get("candidate_logic_signature") in reversal.PROVIDER_SPEC["supported_signatures"]]
        if not cls.edges: raise unittest.SkipTest("no live edges currently use the reversal_long candidate family")
    def pair(self):
        e=self.edges[0]; c=e["candidate"]; mod=load(ROOT/c["path"],"reversal_oracle")
        fn=getattr(mod,c["runtime_entry_functions"][0])
        p=create_candidate_provider(reversal.REVERSAL_LONG_SIGNATURE,d1_edge=e,v2_root=str(ROOT))
        return e,fn,p
    def test_all_members_bind(self):
        for e in self.edges:
            p=create_candidate_provider(reversal.REVERSAL_LONG_SIGNATURE,d1_edge=e,v2_root=str(ROOT))
            self.assertEqual(p.identity.edge_id,e["edge_id"])
    def test_candidate_source_parity(self):
        e,oracle,p=self.pair(); x=features()
        expected=oracle(x.copy()).reset_index(drop=True); actual=p.replay(x.copy()).reset_index(drop=True)
        self.assertGreater(len(expected),0)
        self.assertEqual(list(expected.columns),list(actual.columns))
        pd.testing.assert_frame_equal(expected,actual,check_dtype=False,check_exact=False,rtol=3e-11,atol=3e-11)
    def test_restart_parity(self):
        _,_,a=self.pair(); _,_,b=self.pair(); x=features(1600)
        all_events=a.replay(x); expected=all_events[all_events["Datetime"].isin(x["Datetime"].iloc[1300:])].reset_index(drop=True)
        b.bootstrap(x.iloc[:1300]); rows=[]
        for _,row in x.iloc[1300:].iterrows():
            ev=b.update_one(row)
            if ev is not None: rows.append(ev)
        actual=pd.DataFrame(rows,columns=list(all_events.columns)).reset_index(drop=True)
        pd.testing.assert_frame_equal(expected,actual,check_dtype=False,check_exact=False,rtol=3e-11,atol=3e-11)
    def test_primary_sequence_matches_source(self):
        e,oracle,_=self.pair(); events=oracle(features()).reset_index(drop=True)
        self.assertGreaterEqual(len(events),2)
        primary=e["primary_side"]; mod=load(ROOT/primary["path"],"reversal_primary_oracle")
        fname=[n for n in primary["runtime_called_functions"] if n.startswith("filter_")][0]
        expected=getattr(mod,fname)(events.copy()).reset_index(drop=True)
        p=create_primary_side_provider(reversal_primary.REVERSAL_LONG_PRIMARY_SIGNATURE,d1_edge=e,v2_root=str(ROOT))
        p.reset(); rows=[]
        for _,ev in events.iterrows():
            # composed runtime advances primary current timestamp first
            p.update_one({"Datetime":ev["Datetime"]})
            out=p.filter_event(ev.to_dict())
            if out is not None: rows.append(out)
        actual=pd.DataFrame(rows,columns=list(expected.columns)).reset_index(drop=True)
        pd.testing.assert_frame_equal(expected,actual,check_dtype=False,check_exact=False,rtol=3e-11,atol=3e-11)

if __name__=="__main__": unittest.main()
