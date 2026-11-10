from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
V2_ROOT = Path(__file__).resolve().parents[2]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))
from shared.calibration.sigmoid_afml import (
    AFML_BET_SIZE_COL, AFML_SIZING_METHOD, PAYOFF_PROFILE_VERSION,
    empirical_payoff_profile, payoff_profile_afml_components, validate_payoff_profile,
)


def main() -> None:
    r = np.array([0.8] * 25 + [-0.4] * 25 + [0.0] * 2, dtype=float)
    p = validate_payoff_profile(empirical_payoff_profile(r))
    assert p["payoff_profile_version"] == PAYOFF_PROFILE_VERSION
    assert np.isclose(p["empirical_reward_risk_ratio"], 2.0)
    zero_rate = 2.0 / 52.0
    expected_pbe = (1.0 - zero_rate) * (1.0 / 3.0)
    assert np.isclose(p["payoff_zero_rate"], zero_rate)
    assert np.isclose(p["empirical_break_even_probability"], expected_pbe)
    probs = np.array([expected_pbe, expected_pbe + 0.01, 0.50, 0.80])
    a = payoff_profile_afml_components(probs, p)[AFML_BET_SIZE_COL]
    assert np.isclose(a[0], 0.0, atol=1e-12)
    assert a[1] > 0.0 and a[-1] > a[1]
    assert AFML_SIZING_METHOD == "empirical_exact_executable_break_even_rescaled_afml_v1"
    print("PASS: Phase 2 executable payoff semantics unit test passed")


if __name__ == "__main__": main()
