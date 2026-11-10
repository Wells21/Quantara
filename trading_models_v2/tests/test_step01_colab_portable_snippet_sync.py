"""Drift detection for Step 01's inlined denoise/ONC block.

Same discipline as tests/test_colab_portable_snippet_sync.py (purge/embargo)
and tests/test_trial_ledger_schema_sync.py: Step 01 (feature surgery) runs
standalone on Google Colab and cannot import shared/, so MLDP A4's
Marcenko-Pastur denoising + ONC (shared/colab_portable/denoise_onc_snippet.py)
is copy-pasted verbatim into every edge's 01_feature_surgery_clustered_mda.py.
This test re-parses every edge script that carries that inlined block and
asserts each function body is byte-for-byte identical (after whitespace
normalization) to the canonical snippet, so drift is caught by the test
suite rather than by a runtime import that cannot exist here.

See docs/mldp/A4_DENOISE_ONC_UTILITY.md.
"""
from __future__ import annotations

import glob
import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CANONICAL_PATH = _REPO_ROOT / "shared" / "colab_portable" / "denoise_onc_snippet.py"

_FUNCTION_NAMES = (
    "cov_to_corr",
    "corr_to_cov",
    "get_pca",
    "mp_pdf",
    "fit_kde",
    "_err_pdfs",
    "find_max_eval",
    "denoised_corr_constant_residual_eigenvalue",
    "denoised_corr_targeted_shrinkage",
    "detone_corr",
    "denoise_correlation",
    "_cluster_quality",
    "cluster_kmeans_base",
    "_make_new_outputs",
    "cluster_kmeans_top",
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


class TestStep01ColabPortableSnippetSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.canonical = _canonical_bodies()
        pattern = str(_REPO_ROOT / "assets" / "*" / "edges" / "*" / "research" / "01_feature_surgery_clustered_mda.py")
        cls.edge_scripts = sorted(glob.glob(pattern))
        if not cls.edge_scripts:
            raise AssertionError(f"No Step 01 edge research scripts found under pattern: {pattern}")

    def test_canonical_snippet_file_exists_and_is_self_consistent(self):
        self.assertTrue(_CANONICAL_PATH.exists())
        self.assertEqual(set(self.canonical.keys()), set(_FUNCTION_NAMES))

    def test_every_step01_script_with_the_inlined_block_matches_canonical(self):
        mismatches: list[str] = []
        checked = 0
        for path in self.edge_scripts:
            source = Path(path).read_text(encoding="utf-8", errors="ignore")
            if "def denoise_correlation" not in source:
                continue  # not yet migrated to MLDP A4 denoise/ONC (Task 2C rollout in progress)
            checked += 1
            for name in _FUNCTION_NAMES:
                actual_body = _extract_function_body(source, name)
                if actual_body != self.canonical[name]:
                    mismatches.append(f"{path} :: {name}")
        self.assertGreater(
            checked, 0, "Expected at least one Step 01 script with the inlined denoise/ONC block."
        )
        self.assertEqual(
            mismatches,
            [],
            msg=(
                f"{len(mismatches)} Step 01 script(s) have drifted from the canonical "
                f"denoise/ONC snippet at {_CANONICAL_PATH}:\n  "
                + "\n  ".join(mismatches[:20])
                + ("\n  ... (truncated)" if len(mismatches) > 20 else "")
            ),
        )


if __name__ == "__main__":
    unittest.main()
