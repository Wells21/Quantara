from __future__ import annotations
import importlib.util,json,sys,unittest
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT.parent):
 if str(p) not in sys.path:sys.path.insert(0,str(p))
from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.primary_side_continuations import create_primary_side_provider
from shared.edges.candidate_continuations.providers import trendpullback_short
from shared.edges.primary_side_continuations.providers import trendpullback_short as pm
def load(path,name):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
def features(n=900):
 i=np.arange(n,dtype=float)
 # persistent downtrend with oscillating upward pullbacks and bearish re-entry bars
 close=120-0.03*i+0.35*np.sin(i/6.0);open_=close+0.08+0.04*np.sin(i/4.0);high=np.maximum(open_,close)+.18;low=np.minimum(open_,close)-.22
 dt=pd.date_range("2026-01-01",periods=n,freq="min",tz="UTC")
 return pd.DataFrame({"Datetime":dt,"Bar_Start":dt,"Open":open_,"High":high,"Low":low,"Close":close,"ATR_14":1.0,"Close_FFD":close*.995})
class Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.d=json.loads((ROOT/"portfolio_live/config/phase_d/candidate_contract.json").read_text(encoding="utf-8-sig"));cls.edges=[e for e in cls.d["edges"] if (e.get("candidate") or {}).get("candidate_logic_signature") in trendpullback_short.PROVIDER_SPEC["supported_signatures"]]
  if not cls.edges:raise unittest.SkipTest("no live edges currently use the trendpullback_short candidate family")
 def pair(self):
  e=self.edges[0];c=e["candidate"];m=load(ROOT/c["path"],"tps_oracle");return e,getattr(m,c["runtime_entry_functions"][0]),create_candidate_provider(trendpullback_short.TRENDPULLBACK_SHORT_SIGNATURE,d1_edge=e,v2_root=str(ROOT))
 def test_candidate_parity(self):
  e,o,p=self.pair();x=features();a=o(x.copy()).reset_index(drop=True);b=p.replay(x).reset_index(drop=True);self.assertGreater(len(a),0);self.assertEqual(list(a.columns),list(b.columns));pd.testing.assert_frame_equal(a,b,check_dtype=False,check_exact=False,rtol=3e-11,atol=3e-11)
 def test_restart(self):
  _,_,a=self.pair();_,_,b=self.pair();x=features(1000);all_=a.replay(x);exp=all_[all_["Datetime"].isin(x["Datetime"].iloc[750:])].reset_index(drop=True);b.bootstrap(x.iloc[:750]);rows=[]
  for _,r in x.iloc[750:].iterrows():
   ev=b.update_one(r)
   if ev is not None:rows.append(ev)
  got=pd.DataFrame(rows,columns=list(all_.columns)).reset_index(drop=True);pd.testing.assert_frame_equal(exp,got,check_dtype=False,check_exact=False,rtol=3e-11,atol=3e-11)
 def test_primary_with_current_context(self):
  e,o,_=self.pair();x=features();events=o(x.copy()).reset_index(drop=True);pr=e["primary_side"];m=load(ROOT/pr["path"],"tps_primary_oracle");fn=[n for n in pr["runtime_called_functions"] if n.startswith("filter_")][0];exp=getattr(m,fn)(events.copy(),x.copy()).reset_index(drop=True)
  p=create_primary_side_provider(pm.TRENDPULLBACK_SHORT_PRIMARY_SIGNATURE,d1_edge=e,v2_root=str(ROOT));event_map={pd.Timestamp(r["Datetime"]):r for _,r in events.iterrows()};rows=[]
  for _,r in x.iterrows():
   p.update_one(r)
   ts=pd.Timestamp(r["Datetime"])
   if ts in event_map:
    out=p.filter_event(event_map[ts].to_dict())
    if out is not None:rows.append(out)
  got=pd.DataFrame(rows,columns=list(exp.columns)).reset_index(drop=True);pd.testing.assert_frame_equal(exp,got,check_dtype=False,check_exact=False,rtol=3e-11,atol=3e-11)
 def test_bind(self):
  for e in self.edges:self.assertEqual(create_candidate_provider(trendpullback_short.TRENDPULLBACK_SHORT_SIGNATURE,d1_edge=e,v2_root=str(ROOT)).identity.edge_id,e["edge_id"])
if __name__=="__main__":unittest.main()
