from __future__ import annotations
import importlib.util,json,sys,unittest
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT.parent):
 if str(p) not in sys.path:sys.path.insert(0,str(p))
from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.primary_side_continuations import create_primary_side_provider
from shared.edges.candidate_continuations.providers import dailyrangefade_long as cm
from shared.edges.primary_side_continuations.providers import dailyrangefade_long as pm
def load(path,name):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
def features(days=28,bpd=60):
 rows=[]
 for d in range(days):
  start=pd.Timestamp("2026-01-01",tz="UTC")+pd.Timedelta(days=d);dayopen=100+.05*np.sin(d)
  for j in range(bpd):
   progress=j/(bpd-1);close=dayopen-1.7*progress+0.05*np.sin(j/4);open_=dayopen if j==0 else rows[-1]["Close"];high=max(open_,close)+.08;low=min(open_,close)-.10
   rows.append({"Datetime":start+pd.Timedelta(minutes=j),"Bar_Start":start+pd.Timedelta(minutes=j),"Open":open_,"High":high,"Low":low,"Close":close,"ATR_14":1.0,"Regime_State":float(d%4),"Regime_Entropy":.5,"Regime_Duration":float(j),"Prob_State_0":.1,"Prob_State_1":.2,"Prob_State_2":.3,"Prob_State_3":.4})
 return pd.DataFrame(rows)
class Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.d=json.loads((ROOT/"portfolio_live/config/phase_d/candidate_contract.json").read_text(encoding="utf-8-sig"));cls.e=[e for e in cls.d["edges"] if (e.get("candidate") or {}).get("candidate_logic_signature") in cm.PROVIDER_SPEC["supported_signatures"]]
  if not cls.e:raise unittest.SkipTest("no live edges currently use the dailyrangefade_long candidate family")
 def pair(self):
  e=self.e[0];c=e["candidate"];m=load(ROOT/c["path"],"daily_oracle");return e,getattr(m,c["runtime_entry_functions"][0]),create_candidate_provider(cm.DAILY_RANGE_FADE_LONG_SIGNATURE,d1_edge=e,v2_root=str(ROOT))
 def test_candidate_load_bearing(self):
  _,o,p=self.pair();x=features();a=o(x.copy()).reset_index(drop=True);b=p.replay(x).reset_index(drop=True);self.assertGreater(len(a),0);cols=["Datetime","event_id","edge_id","side","candidate_event_generator","event_known_after_close","uses_future_information","Event_Vol","Daily_Date","Day_Open","Day_High_So_Far","Day_Low_So_Far","Expected_Daily_Range","Daily_Range_Completion","Downside_Extension_ATR","Day_Position","Bars_Into_Day","Daily_Fade_Sample_Count","Daily_Fade_Bias_Mean","Daily_Fade_Bias_Std","Daily_Fade_Bias_TStat","Daily_Range_Fade_Event"];pd.testing.assert_frame_equal(a[cols],b[cols],check_dtype=False,check_exact=False,rtol=5e-11,atol=5e-11)
 def test_restart(self):
  _,o,_=self.pair();_,_,b=self.pair();x=features(30);cut=24*60;all_=o(x.copy()).reset_index(drop=True);exp=all_[all_["Datetime"].isin(x["Datetime"].iloc[cut:])].reset_index(drop=True);self.assertGreater(len(exp),0,"restart parity window must contain source-authority events");b.bootstrap(x.iloc[:cut]);rows=[]
  for _,r in x.iloc[cut:].iterrows():
   e=b.update_one(r)
   if e is not None:rows.append(e)
  self.assertGreater(len(rows),0,"restarted provider emitted no events in a source-active tail");got=pd.DataFrame(rows);cols=["Datetime","event_id","edge_id","side","candidate_event_generator","event_known_after_close","uses_future_information","Event_Vol","Expected_Daily_Range","Daily_Range_Completion","Daily_Fade_Sample_Count"];pd.testing.assert_frame_equal(exp[cols],got[cols],check_dtype=False,check_exact=False,rtol=5e-11,atol=5e-11)
 def test_primary(self):
  e,o,_=self.pair();ev=o(features()).reset_index(drop=True);pr=e["primary_side"];m=load(ROOT/pr["path"],"daily_primary");fn=[n for n in pr["runtime_called_functions"] if n.startswith("filter_")][0];exp=getattr(m,fn)(ev.copy()).reset_index(drop=True);p=create_primary_side_provider(pm.DAILY_RANGE_FADE_LONG_PRIMARY_SIGNATURE,d1_edge=e,v2_root=str(ROOT));rows=[]
  for _,r in ev.iterrows():p.update_one({"Datetime":r["Datetime"]});rows.append(p.filter_event(r.to_dict()))
  got=pd.DataFrame(rows,columns=list(exp.columns)).reset_index(drop=True);pd.testing.assert_frame_equal(exp,got,check_dtype=False,check_exact=False)
 def test_bind(self):
  for e in self.e:self.assertEqual(create_candidate_provider(cm.DAILY_RANGE_FADE_LONG_SIGNATURE,d1_edge=e,v2_root=str(ROOT)).identity.edge_id,e["edge_id"])
if __name__=="__main__":unittest.main()
