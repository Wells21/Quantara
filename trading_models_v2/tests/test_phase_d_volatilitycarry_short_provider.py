from __future__ import annotations
import importlib.util,json,sys,unittest
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT.parent):
    if str(p) not in sys.path: sys.path.insert(0,str(p))

from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.candidate_continuations.providers import volatilitycarry_short
from shared.edges.primary_side_continuations import create_primary_side_provider
from shared.edges.primary_side_continuations.providers import volatilitycarry_short as primary_mod

def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec); assert spec.loader is not None; spec.loader.exec_module(mod); return mod

def features(rows=1900):
    i=np.arange(rows,dtype="float64")
    direction=np.where(((i.astype("int64")//300)%2)==0,1.0,-1.0)
    step=direction*0.00030+0.000025*np.sin(i/8.0)
    close=np.exp(np.log(100.0)+np.cumsum(step)); open_=close/np.exp(0.4*step)
    high=np.maximum(open_,close)*1.0007; low=np.minimum(open_,close)*0.9993
    bar_vol=np.full(rows,1.0); bar_vol[(i.astype("int64")%11)==0]=1.8; bar_vol[(i.astype("int64")%7)==0]=0.7
    dt=pd.date_range("2026-01-01T00:00:00Z",periods=rows,freq="min"); j=np.arange(rows,dtype="int64")
    return pd.DataFrame({"Datetime":dt,"Bar_Start":dt,"Open":open_,"High":high,"Low":low,"Close":close,
        "Bar_Volatility_Bps":bar_vol,"Regime_State":(j%4).astype(float),"Regime_Entropy":0.5,
        "Regime_Duration":(j%20).astype(float),"Prob_State_0":0.1,"Prob_State_1":0.2,"Prob_State_2":0.3,"Prob_State_3":0.4})

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d1=json.loads((ROOT/"portfolio_live/config/phase_d/candidate_contract.json").read_text(encoding="utf-8-sig"))
        cls.edges=[e for e in d1["edges"] if (e.get("candidate") or {}).get("candidate_logic_signature") in volatilitycarry_short.PROVIDER_SPEC["supported_signatures"]]
        cls.pedges=[e for e in d1["edges"] if (e.get("primary_side") or {}).get("primary_logic_signature") in primary_mod.PROVIDER_SPEC["supported_signatures"]]
        if not cls.edges:raise unittest.SkipTest("no live edges currently use the volatilitycarry_short candidate family")

    def test_candidate_source_parity_and_restart(self):
        e=self.edges[0]; c=e["candidate"]; oracle=load(ROOT/c["path"],"vc_short_oracle")
        p=create_candidate_provider(volatilitycarry_short.VOLATILITYCARRY_SHORT_SIGNATURE,d1_edge=e,v2_root=str(ROOT))
        x=features(); expected=getattr(oracle,c["runtime_entry_functions"][0])(x.copy()).reset_index(drop=True)
        actual=p.replay(x.copy()).reset_index(drop=True)
        self.assertGreater(len(expected),0); self.assertEqual(list(expected.columns),list(actual.columns))
        pd.testing.assert_frame_equal(expected,actual,check_dtype=False,check_exact=False,rtol=3e-11,atol=3e-11)
        p2=create_candidate_provider(volatilitycarry_short.VOLATILITYCARRY_SHORT_SIGNATURE,d1_edge=e,v2_root=str(ROOT))
        p2.bootstrap(x.iloc[:1550]); rows=[]
        for _,row in x.iloc[1550:].iterrows():
            ev=p2.update_one(row)
            if ev is not None: rows.append(ev)
        exp=expected[expected["Datetime"].isin(x["Datetime"].iloc[1550:])].reset_index(drop=True)
        got=pd.DataFrame(rows,columns=list(expected.columns)).reset_index(drop=True)
        pd.testing.assert_frame_equal(exp,got,check_dtype=False,check_exact=False,rtol=3e-11,atol=3e-11)

    def test_primary_source_parity(self):
        e=self.pedges[0]; primary=e["primary_side"]; oracle=load(ROOT/primary["path"],"vc_short_primary")
        p=create_primary_side_provider(primary_mod.VOLATILITYCARRY_SHORT_PRIMARY_SIGNATURE,d1_edge=e,v2_root=str(ROOT))
        ts=pd.Timestamp("2026-08-18T12:00:00Z"); p.bootstrap(pd.DataFrame({"Datetime":[ts]}))
        event={"Datetime":ts,"Bar_Start":ts,"edge_id":e["edge_id"],"side":-1,"Event_Vol":0.01,
            "Volatility_Carry_Event_Type":-1,"Drift_TStat":-1.2,"Volatility_Ratio":0.9,"Is_Calm_Vol":True}
        fname=[n for n in primary["runtime_called_functions"] if n.startswith("filter_")][0]
        expected=getattr(oracle,fname)(pd.DataFrame([event])).reset_index(drop=True)
        actual_event=p.filter_event(event); actual=pd.DataFrame([actual_event],columns=list(expected.columns))
        pd.testing.assert_frame_equal(expected,actual,check_dtype=False,check_exact=False)

if __name__=="__main__": unittest.main()
