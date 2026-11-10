import json
import numpy as np
from scipy import stats
from shared.backtest_statistics.afml_ch14_dsr import expected_max_sr, z_stat
L = "logs/ch14_dsr_20261026"
N_THESES = {"GBPJPY_dailyrangecompletionfade_long": 51, "HK_drawdownhazardcontinuation_short": 50,
            "JPN_rangestructure_long": 52, "XAUUSD_rangestructure_long": 50}
out = {}
for key, n_th in N_THESES.items():
    m = json.load(open(f"{L}/{key}.json")); s = json.load(open(f"{L}/sens_{key}.json"))
    sel = m["selected"]; f = sel["frequency"]
    k = 2 * n_th  # approved approximation: ~2 effective trials per thesis (ONC on the deployed theses' 406 trials)
    z0 = z_stat(sel["sr"], sel["T"], 0.0, sel["skew"], sel["kurt"])
    alpha_k = 1 - (1 - (1 - stats.norm.cdf(z0))) ** k
    row = {"n_theses": n_th, "E_K_level2": k, "alpha_K": float(alpha_k), "fwer_pass": bool(alpha_k < 0.05)}
    for var in ("A_book", "B_eq_weight", "C_afml_V_K"):
        v = s[var]["V_annualised_sr"]
        sr_star = np.sqrt(v / f) * expected_max_sr(k)
        row[var] = {"V_annualised": v, "sr_star_annualised": float(sr_star * np.sqrt(f)),
                    "dsr": float(stats.norm.cdf(z_stat(sel["sr"], sel["T"], sr_star, sel["skew"], sel["kurt"])))}
    out[m["edge"]] = row
    print(f"{m['edge']:40s} theses {n_th}  E[K] {k}  DSR A {row['A_book']['dsr']:.4f} B {row['B_eq_weight']['dsr']:.4f} C {row['C_afml_V_K']['dsr']:.4f}  | SR* ann A {row['A_book']['sr_star_annualised']:.3f}  sel SR ann {sel['annualised_sr']:.3f} | FWER aK {alpha_k:.4f}")
json.dump(out, open(f"{L}/level2_thesis_search.json", "w"), indent=2)
