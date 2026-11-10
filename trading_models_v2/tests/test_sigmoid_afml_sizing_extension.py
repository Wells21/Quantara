"""Tests for shared/calibration/sigmoid_afml.py's bet-sizing functions,
focused on two things the A5 pass (see docs/mldp/A5_SIZING_VERIFICATION.md)
was specifically for:

1. `afml_probability_bet_size` now delegates to the single canonical AFML
   Ch.10 reference (shared/sizing/afml_ch10_reference.py) instead of
   maintaining a second, independent copy of the same z/Phi formula. This
   was a real, unforced duplication (unlike the Step 01/02 purge/embargo
   case) since this module does not run on Google Colab -- there was no
   operational reason for the duplicate implementation to exist.

2. `payoff_aware_afml_components` (Quantara's empirical break-even rescaling
   extension) is verified as its own, separately-named, separately-tested
   object -- never silently presented as if it were the canonical formula.
"""
from __future__ import annotations

import unittest

import numpy as np
from scipy.stats import norm

from shared.calibration.sigmoid_afml import (
    afml_probability_bet_size,
    payoff_aware_afml_components,
)
from shared.sizing.afml_ch10_reference import probability_to_signal


class TestCanonicalDelegationNotDuplication(unittest.TestCase):
    def test_matches_canonical_reference_across_probability_range(self):
        ps = np.linspace(0.001, 0.999, 200)
        from_extension_module = afml_probability_bet_size(ps)
        from_canonical_reference = np.clip(
            probability_to_signal(ps.tolist(), [1.0] * len(ps), num_classes=2),
            0.0,
            1.0,
        )
        np.testing.assert_allclose(
            from_extension_module, from_canonical_reference, atol=1e-12
        )

    def test_matches_hand_computed_scipy_reference(self):
        for p in (0.5, 0.6, 0.75, 0.9, 0.4, 0.2):
            with self.subTest(p=p):
                z = (p - 0.5) / np.sqrt(p * (1.0 - p))
                expected = max(0.0, 2.0 * norm.cdf(z) - 1.0)
                actual = float(afml_probability_bet_size(np.array([p]))[0])
                self.assertAlmostEqual(actual, expected, places=10)

    def test_sub_half_probability_clips_to_zero_not_negative(self):
        # Documented, preserved behavior: this function is only ever fed an
        # "effective probability" >= 0.5 by its caller, but if it were
        # called below 0.5 it must clip to 0, not propagate a negative size
        # (that would imply an opposite-direction bet from a magnitude-only
        # function, which is a caller-side bug this function must not mask
        # by inventing a signed result on its own).
        result = afml_probability_bet_size(np.array([0.3]))
        self.assertEqual(float(result[0]), 0.0)


class TestPayoffAwareExtensionIsSeparatelyNamedAndTested(unittest.TestCase):
    """Quantara's empirical break-even rescaling. Deliberately NOT compared
    against the canonical reference for equality -- it is a named extension
    that is EXPECTED to differ, since it rescales the input probability
    before applying the same underlying bet-size transform. What matters is
    that its own formula is correct and that it never gets called "AFML
    Ch.10" without qualification anywhere in generated artifacts."""

    def test_break_even_probability_gives_zero_bet_size(self):
        # At exactly the break-even probability, economic_strength = 0,
        # effective_probability = 0.5, bet_size = 0.
        components = payoff_aware_afml_components(
            calibrated_probability=[0.4], reward_risk_ratio=[1.5]
        )
        break_even = float(components["break_even_probability"][0])
        components_at_be = payoff_aware_afml_components(
            calibrated_probability=[break_even], reward_risk_ratio=[1.5]
        )
        self.assertAlmostEqual(
            float(components_at_be["afml_economic_strength"][0]), 0.0, places=10
        )
        self.assertAlmostEqual(
            float(components_at_be["afml_bet_size"][0]), 0.0, places=8
        )

    def test_full_confidence_probability_gives_near_maximal_bet_size(self):
        # economic_strength = clip((p - break_even)/(1 - break_even), 0, 1);
        # at p=0.999 this is close to but not exactly 1.0 (only p=1.0 itself
        # -- which clip_probability forbids -- would give exactly 1.0), so
        # assert "close to the ceiling" rather than "equal to the ceiling."
        components = payoff_aware_afml_components(
            calibrated_probability=[0.999], reward_risk_ratio=[1.5]
        )
        self.assertGreater(float(components["afml_economic_strength"][0]), 0.99)
        self.assertGreater(float(components["afml_bet_size"][0]), 0.9)

    def test_reward_risk_ratio_of_one_gives_break_even_probability_one_half(self):
        components = payoff_aware_afml_components(
            calibrated_probability=[0.5], reward_risk_ratio=[1.0]
        )
        self.assertAlmostEqual(
            float(components["break_even_probability"][0]), 0.5, places=12
        )

    def test_extension_output_differs_from_naive_canonical_call_at_same_probability(self):
        # Demonstrates the two are genuinely different objects, not
        # accidentally identical -- guards against a future edit silently
        # collapsing the extension back into the canonical formula (which
        # would hide the rescaling this extension exists to apply).
        p = 0.6
        extension_result = payoff_aware_afml_components(
            calibrated_probability=[p], reward_risk_ratio=[2.0]
        )["afml_bet_size"][0]
        canonical_result = float(afml_probability_bet_size(np.array([p]))[0])
        self.assertNotAlmostEqual(
            float(extension_result), canonical_result, places=6
        )


if __name__ == "__main__":
    unittest.main()
