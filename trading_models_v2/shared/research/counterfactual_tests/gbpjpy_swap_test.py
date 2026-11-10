"""GBPJPY dailyrangecompletionfade_long card C2 (PREREGISTRATION.md): the locked cell's Gate C trades with swap added
back -- does the edge stand before swap? Swap per trade = v3_horizon_lab._xef_apply_carry_costs (the Gate C code)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

V2 = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(V2))
import shared.research.v3_horizon_lab as lab  # noqa: E402
from shared.research.counterfactual_tests.run_counterfactuals import clustered_ci  # noqa: E402

ART = V2 / "assets/GBPJPY/edges/dailyrangecompletionfade_long/artifacts/v3_horizon_lab"
CELL, STAMP = "H6_E100_V168_PT2_SL1", "20261026T185441Z"
cm = json.loads((V2 / "portfolio_allocator/config/broker_profiles/moneta_cost_models/GBPJPY.json").read_text(encoding="utf-8"))
t = pd.read_csv(ART / f"v3_execution_trades_{CELL}_{STAMP}.csv")
summ = pd.read_csv(ART / f"v3_execution_velocity_summary_{STAMP}.csv").set_index("label").loc[CELL]
res_t = t[t.status.eq("resolved")]
c = lab._xef_apply_carry_costs(t, cm)  # noqa: SLF001
r = c[c.status.eq("resolved")]
saved_is_post_carry = abs(res_t.exec_r.mean() - float(summ.exec_mean_R)) < 1e-9
pre_swap = (r.exec_r_pre_carry + r.swap_R) if saved_is_post_carry else (r.exec_r_pre_carry)
post = r.exec_r_pre_carry if saved_is_post_carry else r.exec_r
out = {"cell": CELL, "stamp": STAMP, "summary_exec_mean_R": float(summ.exec_mean_R),
       "saved_exec_r_is_post_carry": bool(saved_is_post_carry), "mean_swap_R": round(float(r.swap_R.mean()), 5),
       "mean_commission_R": round(float(r.commission_R.mean()), 5),
       "net_R_as_certified": clustered_ci(post, r.entry_ts), "net_R_before_swap": clustered_ci(pre_swap, r.entry_ts)}
b = out["net_R_before_swap"]
out["verdict"] = "PASS" if b["ci_lo"] > 0 else ("INCONCLUSIVE" if b["mean_R"] > 0 else "FAIL")
(V2 / "logs/counterfactual_tests_20261031/GBPJPY_dailyrangecompletionfade_long_C2_swap.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
print(json.dumps(out, indent=1))
