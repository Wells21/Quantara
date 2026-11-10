"""Drift detection for the AFML Ch.6 + Ch.4 SS4.5 sequential-bootstrap
bagging block inlined in every edge's Step 02/03 scripts.

Step 02/03 run standalone on Google Colab and cannot import shared/ at
runtime, so they each carry their own inlined copy of
shared/models/sequential_bootstrap.py + shared/models/sequential_bootstrap_
bagging.py instead of importing them (same constraint as LEAKAGE_COLS; see
tests/test_leakage_columns_sync.py). This test extracts the inlined block
(delimited by its version marker comment through the class's last method)
from every edge's Step02/03 script and asserts it is byte-identical across
all edges -- catching an edge left on a stale template after a partial
propagation, exactly the class of bug found and fixed this session (only 1
of 141 edges had the AFML provenance numeric-tolerance fix before tonight).

It also executes the extracted block in an isolated namespace and re-runs
the same numerical proof from shared/models/sequential_bootstrap.py's own
self-test (AFML SS4.5.3's worked example, p.65-66) against it, so this is a
functional-correctness check, not just a text-equality one.
"""
from __future__ import annotations

import glob
import re
import unittest
from pathlib import Path

import numpy as np

from shared.models.sequential_bootstrap_bagging import _cross_process_self_test

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_NAMES = (
    "02_train_meta_model_optuna.py",
    "03_generate_oof_predictions.py",
)
_MARKER = "# QUANTARA_AFML_CH6_CH4_5_SEQUENTIAL_BOOTSTRAP_BAGGING_V1"
_END_MARKER = "        return float(np.mean(off_diag))"


def _extract_block(source: str) -> str | None:
    start = source.find(_MARKER)
    if start < 0:
        return None
    end = source.find(_END_MARKER, start)
    if end < 0:
        return None
    return source[start : end + len(_END_MARKER)]


class TestSequentialBootstrapBaggingSync(unittest.TestCase):
    def test_every_edge_step02_03_has_identical_bagging_block(self):
        # A separate reference per script name: Step02 and Step03 are not
        # expected to be textually identical to EACH OTHER (their comments/
        # surrounding call sites differ), only every edge's Step02 must match
        # every other edge's Step02, and likewise for Step03.
        missing: list[str] = []
        for script_name in _SCRIPT_NAMES:
            reference: str | None = None
            reference_path: Path | None = None
            mismatches: list[str] = []
            checked = 0
            pattern = str(_REPO_ROOT / "assets" / "*" / "edges" / "*" / "research" / script_name)
            for path_str in sorted(glob.glob(pattern)):
                path = Path(path_str)
                source = path.read_text(encoding="utf-8")
                block = _extract_block(source)
                if block is None:
                    missing.append(str(path.relative_to(_REPO_ROOT)))
                    continue
                checked += 1
                if reference is None:
                    reference, reference_path = block, path
                elif block != reference:
                    mismatches.append(str(path.relative_to(_REPO_ROOT)))

            self.assertGreater(checked, 0, f"No {script_name} scripts contained the sequential-bootstrap-bagging marker.")
            self.assertEqual(
                mismatches, [],
                f"{len(mismatches)} {script_name} script(s) have a bagging block that differs from "
                f"the reference ({reference_path}): {mismatches[:10]}{'...' if len(mismatches) > 10 else ''}",
            )
        self.assertEqual(
            missing, [],
            f"{len(missing)} edge script(s) are missing the sequential-bootstrap-bagging block entirely "
            f"(still on the pre-AFML-Ch.6 XGBoost template): {missing[:10]}{'...' if len(missing) > 10 else ''}",
        )

    def test_inlined_block_reproduces_afml_worked_example(self):
        pattern = str(
            _REPO_ROOT / "assets" / "XAUUSD" / "edges" / "volatilitycarry_long"
            / "research" / "02_train_meta_model_optuna.py"
        )
        matches = glob.glob(pattern)
        self.assertTrue(matches, "Reference edge script not found.")
        source = Path(matches[0]).read_text(encoding="utf-8")
        block = _extract_block(source)
        self.assertIsNotNone(block, "Could not extract the bagging block for execution.")

        namespace: dict[str, object] = {"np": np}
        exec(compile(block, "<inlined_sequential_bootstrap_bagging>", "exec"), namespace)  # noqa: S102

        # AFML SS4.5.3 worked example (p.65-66), identical to shared/models/
        # sequential_bootstrap.py's own self-test.
        start_pos = np.array([0, 2, 4], dtype="int64")
        end_pos = np.array([2, 3, 5], dtype="int64")
        concurrency = np.zeros(6, dtype="float64")
        concurrency[start_pos[1] : end_pos[1] + 1] += 1.0
        avg_u = namespace["average_uniqueness_if_added"](start_pos, end_pos, concurrency)
        np.testing.assert_allclose(avg_u, np.array([5.0 / 6.0, 1.0 / 2.0, 1.0]))
        prob = avg_u / avg_u.sum()
        np.testing.assert_allclose(prob, np.array([5.0 / 14.0, 3.0 / 14.0, 6.0 / 14.0]))

    def test_model_pkl_survives_genuine_cross_process_load(self):
        """Regression test for a critical bug found and fixed this session:
        joblib.dump'ing the inline-defined class instance directly produces
        a model.pkl unloadable by any other process (Step05-09,
        EdgeScorer, live scoring) -- a same-process pickle round trip does
        not catch this. See shared/models/sequential_bootstrap_bagging.py's
        to_portable_payload docstring for the full explanation and fix.
        Runs a genuine subprocess as the producer (simulating Step02 on
        Colab), not just an in-process pickle.loads(pickle.dumps(...))."""
        _cross_process_self_test()  # raises SystemExit on any failure


if __name__ == "__main__":
    unittest.main()
