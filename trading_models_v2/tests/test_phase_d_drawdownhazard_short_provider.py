from __future__ import annotations
import importlib.util,json,sys,unittest
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT.parent):
 if str(p) not in sys.path:sys.path.insert(0,str(p))
from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.primary_side_continuations import create_primary_side_provider
from shared.edges.candidate_continuations.providers import drawdownhazard_short as cm
from shared.edges.primary_side_continuations.providers import drawdownhazard_short as pm
def load(path,name):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
def features(n=1500):
 i=np.arange(n,dtype=float);ret=0.00015*np.sin(i/11.0)
 # After the 1000-bar severity baseline is available, create a long causal drawdown.
 ret[1050:]=-.003
 close=np.exp(np.log(100)+np.cumsum(ret));open_=close*np.exp(-.2*ret);high=np.maximum(open_,close)*1.0005;low=np.minimum(open_,close)*.9995;dt=pd.date_range("2026-01-01",periods=n,freq="min",tz="UTC");j=np.arange(n)
 return pd.DataFrame({"Datetime":dt,"Bar_Start":dt,"Open":open_,"High":high,"Low":low,"Close":close,"ATR_14":.5,"Regime_State":(j%4).astype(float),"Regime_Entropy":.5,"Regime_Duration":(j%20).astype(float),"Prob_State_0":.1,"Prob_State_1":.2,"Prob_State_2":.3,"Prob_State_3":.4})
class Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.d=json.loads((ROOT/"portfolio_live/config/phase_d/candidate_contract.json").read_text(encoding="utf-8-sig"));cls.e=[e for e in cls.d["edges"] if (e.get("candidate") or {}).get("candidate_logic_signature") in cm.PROVIDER_SPEC["supported_signatures"]]
  if not cls.e:raise unittest.SkipTest("no live edges currently use the drawdownhazard_short candidate family")
 def pair(self):
  e=self.e[0];c=e["candidate"];m=load(ROOT/c["path"],"haz_oracle");return e,getattr(m,c["runtime_entry_functions"][0]),create_candidate_provider(cm.DRAWDOWNHAZARD_SHORT_SIGNATURE,d1_edge=e,v2_root=str(ROOT))
 def test_candidate_causal_hazard_load_bearing(self):
  _,o,p=self.pair();x=features();a=o(x.copy()).reset_index(drop=True);b=p.replay(x).reset_index(drop=True);self.assertGreater(len(a),0);cols=["Datetime","Event_Vol","Drawdown_Hazard_State_Entry","Drawdown_Hazard_Renewed_Impulse","Drawdown_Severity_100","Drawdown_Severity_Percentile_100_Over_1000","Bars_Since_Peak_100","Recovery_Fraction","Drawdown_Hazard_Probability","Drawdown_Hazard_Cell_Count","Drawdown_Hazard_Backoff_Level","Drawdown_Hazard_Global_Count"];pd.testing.assert_frame_equal(a[cols],b[cols],check_dtype=False,check_exact=False,rtol=8e-11,atol=8e-11)
 def test_restart_preserves_resolved_history(self):
  _,_,a=self.pair();_,_,b=self.pair();x=features(1550);all_=a.replay(x);cut=1300;exp=all_[all_["Datetime"].isin(x["Datetime"].iloc[cut:])].reset_index(drop=True);b.bootstrap(x.iloc[:cut]);rows=[]
  for _,r in x.iloc[cut:].iterrows():
   e=b.update_one(r)
   if e is not None:rows.append(e)
  got=pd.DataFrame(rows);cols=["Datetime","Drawdown_Hazard_Probability","Drawdown_Hazard_Cell_Count","Drawdown_Hazard_Global_Count","Recovery_Fraction"];pd.testing.assert_frame_equal(exp[cols],got[cols],check_dtype=False,check_exact=False,rtol=8e-11,atol=8e-11)
 def test_primary(self):
  e,o,_=self.pair();ev=o(features()).reset_index(drop=True);pr=e["primary_side"];m=load(ROOT/pr["path"],"haz_primary");fn=[n for n in pr["runtime_called_functions"] if n.startswith("filter_")][0];exp=getattr(m,fn)(ev.copy()).reset_index(drop=True);p=create_primary_side_provider(pm.DRAWDOWNHAZARD_SHORT_PRIMARY_SIGNATURE,d1_edge=e,v2_root=str(ROOT));rows=[]
  for _,r in ev.iterrows():p.update_one({"Datetime":r["Datetime"]});rows.append(p.filter_event(r.to_dict()))
  got=pd.DataFrame(rows,columns=list(exp.columns)).reset_index(drop=True);pd.testing.assert_frame_equal(exp,got,check_dtype=False,check_exact=False)
 def test_bind(self):
  for e in self.e:self.assertEqual(create_candidate_provider(cm.DRAWDOWNHAZARD_SHORT_SIGNATURE,d1_edge=e,v2_root=str(ROOT)).identity.edge_id,e["edge_id"])
if __name__=="__main__":unittest.main()
