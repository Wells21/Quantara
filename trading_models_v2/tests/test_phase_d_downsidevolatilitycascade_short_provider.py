from __future__ import annotations
import importlib.util,json,sys,unittest
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT.parent):
 if str(p) not in sys.path:sys.path.insert(0,str(p))
from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.primary_side_continuations import create_primary_side_provider
from shared.edges.candidate_continuations.providers import downsidevolatilitycascade_short as cm
from shared.edges.primary_side_continuations.providers import downsidevolatilitycascade_short as pm
def load(path,name):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
def features(n=900):
 ret=np.full(n,0.0002)
 for start in [180,420,680]:
  for j in range(start,min(start+45,n)):ret[j]=(-0.008 if (j-start)%4!=3 else 0.002)
 close=np.exp(np.log(100)+np.cumsum(ret));open_=close*np.exp(-.3*ret);high=np.maximum(open_,close)*1.001;low=np.minimum(open_,close)*.999;dt=pd.date_range("2026-01-01",periods=n,freq="min",tz="UTC");i=np.arange(n)
 return pd.DataFrame({"Datetime":dt,"Bar_Start":dt,"Open":open_,"High":high,"Low":low,"Close":close,"ATR_14":.5,"Regime_State":(i%4).astype(float),"Regime_Entropy":.5,"Regime_Duration":(i%20).astype(float),"Prob_State_0":.1,"Prob_State_1":.2,"Prob_State_2":.3,"Prob_State_3":.4})
class Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.d=json.loads((ROOT/"portfolio_live/config/phase_d/candidate_contract.json").read_text(encoding="utf-8-sig"));cls.e=[e for e in cls.d["edges"] if (e.get("candidate") or {}).get("candidate_logic_signature") in cm.PROVIDER_SPEC["supported_signatures"]]
  if not cls.e:raise unittest.SkipTest("no live edges currently use the downsidevolatilitycascade_short candidate family")
 def pair(self):
  e=self.e[0];c=e["candidate"];m=load(ROOT/c["path"],"cascade_oracle");return e,getattr(m,c["runtime_entry_functions"][0]),create_candidate_provider(cm.DOWNSIDEVOLCASCADE_SHORT_SIGNATURE,d1_edge=e,v2_root=str(ROOT))
 def test_candidate_load_bearing_parity(self):
  _,o,p=self.pair();x=features();a=o(x.copy()).reset_index(drop=True);b=p.replay(x).reset_index(drop=True);self.assertGreater(len(a),0)
  cols=["Datetime","event_id","edge_id","side","candidate_event_generator","event_known_after_close","uses_future_information","Event_Vol","Downside_Volatility_Cascade_Event","Cascade_State_Entry","Cascade_Renewed_Impulse","Cascade_Active_State","Log_Return_1","Downside_Variance_Share_20","Vol_Acceleration_20_100"]
  pd.testing.assert_frame_equal(a[cols],b[cols],check_dtype=False,check_exact=False,rtol=3e-11,atol=3e-11)
 def test_restart(self):
  _,_,a=self.pair();_,_,b=self.pair();x=features(1000);all_=a.replay(x);split=650;exp=all_[all_["Datetime"].isin(x["Datetime"].iloc[split:])].reset_index(drop=True);self.assertGreater(len(exp),0,"restart parity window must contain at least one expected event");b.bootstrap(x.iloc[:split]);rows=[]
  for _,r in x.iloc[split:].iterrows():
   e=b.update_one(r)
   if e is not None:rows.append(e)
  cols=["Datetime","event_id","edge_id","side","candidate_event_generator","event_known_after_close","uses_future_information","Event_Vol","Cascade_State_Entry","Cascade_Renewed_Impulse","Downside_Variance_Share_20","Vol_Acceleration_20_100"];got=pd.DataFrame(rows,columns=list(all_.columns));pd.testing.assert_frame_equal(exp[cols],got[cols],check_dtype=False,check_exact=False,rtol=3e-11,atol=3e-11)
 def test_primary(self):
  e,o,_=self.pair();ev=o(features()).reset_index(drop=True);pr=e["primary_side"];m=load(ROOT/pr["path"],"cascade_primary");fn=[n for n in pr["runtime_called_functions"] if n.startswith("filter_")][0];exp=getattr(m,fn)(ev.copy()).reset_index(drop=True);p=create_primary_side_provider(pm.DOWNSIDEVOLCASCADE_SHORT_PRIMARY_SIGNATURE,d1_edge=e,v2_root=str(ROOT));rows=[]
  for _,r in ev.iterrows():p.update_one({"Datetime":r["Datetime"]});rows.append(p.filter_event(r.to_dict()))
  got=pd.DataFrame(rows,columns=list(exp.columns)).reset_index(drop=True);pd.testing.assert_frame_equal(exp,got,check_dtype=False,check_exact=False)
 def test_bind(self):
  for e in self.e:self.assertEqual(create_candidate_provider(cm.DOWNSIDEVOLCASCADE_SHORT_SIGNATURE,d1_edge=e,v2_root=str(ROOT)).identity.edge_id,e["edge_id"])
if __name__=="__main__":unittest.main()
