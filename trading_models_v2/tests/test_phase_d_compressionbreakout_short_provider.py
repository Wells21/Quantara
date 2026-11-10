from __future__ import annotations
import importlib.util,json,sys,unittest
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT.parent):
 if str(p) not in sys.path:sys.path.insert(0,str(p))
from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.primary_side_continuations import create_primary_side_provider
from shared.edges.candidate_continuations.providers import compressionbreakout_short as cm
from shared.edges.primary_side_continuations.providers import compressionbreakout_short as pm
def load(path,name):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
def features(n=1000):
 i=np.arange(n);base=100+0.15*np.sin(i/13);width=np.where((i%260)>=180,0.12,0.8);close=base.copy()
 # inject first downside crosses after compressed zones
 for k in [235,495,755]: 
  if k<n: close[k]=base[k]-1.2
 open_=base;high=np.maximum(open_,close)+width;low=np.minimum(open_,close)-width
 dt=pd.date_range("2026-01-01",periods=n,freq="min",tz="UTC");j=np.arange(n)
 return pd.DataFrame({"Datetime":dt,"Bar_Start":dt,"Open":open_,"High":high,"Low":low,"Close":close,"ATR_14":1.0,
 "Regime_State":(j%4).astype(float),"Regime_Entropy":.5,"Regime_Duration":(j%20).astype(float),"Prob_State_0":.1,"Prob_State_1":.2,"Prob_State_2":.3,"Prob_State_3":.4})
class Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.d=json.loads((ROOT/"portfolio_live/config/phase_d/candidate_contract.json").read_text(encoding="utf-8-sig"));cls.e=[e for e in cls.d["edges"] if (e.get("candidate") or {}).get("candidate_logic_signature") in cm.PROVIDER_SPEC["supported_signatures"]]
  if not cls.e:raise unittest.SkipTest("no live edges currently use the compressionbreakout_short candidate family")
 def pair(self):
  e=self.e[0];c=e["candidate"];m=load(ROOT/c["path"],"cb_oracle");return e,getattr(m,c["runtime_entry_functions"][0]),create_candidate_provider(cm.COMPRESSION_BREAKOUT_SHORT_SIGNATURE,d1_edge=e,v2_root=str(ROOT))
 def test_candidate_parity(self):
  _,o,p=self.pair();x=features();a=o(x.copy()).reset_index(drop=True);b=p.replay(x).reset_index(drop=True);self.assertGreater(len(a),0);self.assertEqual(list(a.columns),list(b.columns));pd.testing.assert_frame_equal(a,b,check_dtype=False,check_exact=False,rtol=3e-11,atol=3e-11)
 def test_restart(self):
  _,_,a=self.pair();_,_,b=self.pair();x=features(1100);all_=a.replay(x);exp=all_[all_["Datetime"].isin(x["Datetime"].iloc[850:])].reset_index(drop=True);b.bootstrap(x.iloc[:850]);rows=[]
  for _,r in x.iloc[850:].iterrows():
   ev=b.update_one(r)
   if ev is not None:rows.append(ev)
  got=pd.DataFrame(rows,columns=list(all_.columns)).reset_index(drop=True);pd.testing.assert_frame_equal(exp,got,check_dtype=False,check_exact=False,rtol=3e-11,atol=3e-11)
 def test_primary(self):
  e,o,_=self.pair();events=o(features()).reset_index(drop=True);pr=e["primary_side"];m=load(ROOT/pr["path"],"cb_primary");fn=[n for n in pr["runtime_called_functions"] if n.startswith("filter_")][0];exp=getattr(m,fn)(events.copy()).reset_index(drop=True);p=create_primary_side_provider(pm.COMPRESSION_BREAKOUT_SHORT_PRIMARY_SIGNATURE,d1_edge=e,v2_root=str(ROOT));rows=[]
  for _,ev in events.iterrows():p.update_one({"Datetime":ev["Datetime"]});out=p.filter_event(ev.to_dict());rows.append(out)
  got=pd.DataFrame(rows,columns=list(exp.columns)).reset_index(drop=True);pd.testing.assert_frame_equal(exp,got,check_dtype=False,check_exact=False)
 def test_bind(self):
  for e in self.e:self.assertEqual(create_candidate_provider(cm.COMPRESSION_BREAKOUT_SHORT_SIGNATURE,d1_edge=e,v2_root=str(ROOT)).identity.edge_id,e["edge_id"])
if __name__=="__main__":unittest.main()
