"""Drift detection for Step 02's inlined research-trial ledger writer.

Same discipline as tests/test_colab_portable_snippet_sync.py (purge/embargo)
and tests/test_step01_colab_portable_snippet_sync.py (denoise/ONC): Step 02
(Optuna hyperparameter search / CPCV model training) runs standalone on
Google Colab and cannot import shared/, so MLDP A6's research-trial ledger
writer (shared/colab_portable/trial_ledger_snippet.py) is copy-pasted
verbatim into every edge's 02_train_meta_model_optuna.py. This test
re-parses every edge script that carries that inlined block and asserts
each function body is byte-for-byte identical (after whitespace
normalization) to the canonical snippet.

See docs/mldp/A6_TRIAL_LEDGER_LOGGING.md.
"""
from __future__ import annotations

import glob
import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CANONICAL_PATH = _REPO_ROOT / "shared" / "colab_portable" / "trial_ledger_snippet.py"

_FUNCTION_NAMES = (
    "new_trial_id",
    "now_utc_iso",
    "build_trial_record",
    "validate_trial_record",
    "_acquire_lock",
    "append_trial_record",
    "read_trial_records",
)

_FUNCTION_PATTERN = "def {name}\\(.*?\\n(?=\\ndef |\\nclass |\\n#|\\Z)"


def _extract_function_body(source: str, function_name: str) -> str | None:
    match = re.search(_FUNCTION_PATTERN.format(name=re.escape(function_name)), source, re.S)
    if match is None:
        return None
    return re.sub(r"\s+", " ", match.group(0)).strip()


def _canonical_bodies() -> dict[str, str]:
    source = _CANONICAL_PATH.read_text(encoding="utf-8")
    bodies = {name: _extract_function_body(source, name) for name in _FUNCTION_NAMES}
    missing = [name for name, body in bodies.items() if body is None]
    if missing:
        raise AssertionError(
            f"Canonical snippet file is missing expected function(s): {missing}. "
            f"Checked: {_CANONICAL_PATH}"
        )
    return bodies


class TestStep02TrialLedgerSnippetSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.canonical = _canonical_bodies()
        pattern = str(_REPO_ROOT / "assets" / "*" / "edges" / "*" / "research" / "02_train_meta_model_optuna.py")
        cls.edge_scripts = sorted(glob.glob(pattern))
        if not cls.edge_scripts:
            raise AssertionError(f"No Step 02 edge research scripts found under pattern: {pattern}")

    def test_canonical_snippet_file_exists_and_is_self_consistent(self):
        self.assertTrue(_CANONICAL_PATH.exists())
        self.assertEqual(set(self.canonical.keys()), set(_FUNCTION_NAMES))

    def test_every_step02_script_with_the_inlined_block_matches_canonical(self):
        mismatches: list[str] = []
        checked = 0
        for path in self.edge_scripts:
            source = Path(path).read_text(encoding="utf-8", errors="ignore")
            if "def append_trial_record" not in source:
                continue  # not yet migrated to MLDP A6 trial ledger (Task 2C rollout in progress)
            checked += 1
            for name in _FUNCTION_NAMES:
                actual_body = _extract_function_body(source, name)
                if actual_body != self.canonical[name]:
                    mismatches.append(f"{path} :: {name}")
        self.assertGreater(
            checked, 0, "Expected at least one Step 02 script with the inlined trial ledger block."
        )
        self.assertEqual(
            mismatches,
            [],
            msg=(
                f"{len(mismatches)} Step 02 script(s) have drifted from the canonical "
                f"trial ledger snippet at {_CANONICAL_PATH}:\n  "
                + "\n  ".join(mismatches[:20])
                + ("\n  ... (truncated)" if len(mismatches) > 20 else "")
            ),
        )


if __name__ == "__main__":
    unittest.main()
