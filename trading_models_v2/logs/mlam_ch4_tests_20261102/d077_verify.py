"""D077 wiring check: onc_best_of_restarts (seed 0, 20 restarts, 4 workers) on the cached XAUUSD volatilitycarry VI matrix
must return the T9 seeds 0-19 block winner (seed 5, q 4.8574)."""
import json, sys
from pathlib import Path
import pandas as pd
V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
from shared.features.clustered_feature_importance import onc_best_of_restarts  # noqa: E402
if __name__ == "__main__":
    out = Path(__file__).resolve().parent
    obs = pd.read_pickle(out / "cache" / "obs_XAUUSD_volatilitycarry_long.pkl")
    _, _, search = onc_best_of_restarts(obs, seed=0, workers=4)
    t9 = json.loads((out / "t9_stage1.json").read_text())["XAUUSD_volatilitycarry_long"]["S20"]
    res = {"search": search, "t9_block0_winner_seed": t9["block_winner_seeds"][0], "t9_block0_q": t9["block_winner_q"][0],
           "match": search["onc_best_seed"] == t9["block_winner_seeds"][0]}
    print(json.dumps(res), flush=True)
    (out / "d077_verify.json").write_text(json.dumps(res, indent=1))
