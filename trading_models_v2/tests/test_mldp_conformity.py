import unittest
from shared.validation.mldp_conformity import (
    MLDPConformityError,
    assert_same_target,
    probability_target_contract,
)

class TestMLDPConformity(unittest.TestCase):
    def test_same_target_passes(self):
        a = probability_target_contract(
            "quote_executable", probability_column="raw_probability"
        )
        b = probability_target_contract(
            "quote_executable", probability_column="calibrated_probability"
        )
        self.assertEqual(assert_same_target(a, b)["status"], "PASS")

    def test_target_switch_fails(self):
        gross = probability_target_contract(
            "gross", probability_column="raw_probability"
        )
        exe = probability_target_contract(
            "quote_executable", probability_column="calibrated_probability"
        )
        with self.assertRaises(MLDPConformityError):
            assert_same_target(gross, exe)

if __name__ == "__main__":
    unittest.main()
