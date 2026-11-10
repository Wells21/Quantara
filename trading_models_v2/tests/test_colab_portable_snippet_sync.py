"""Drift detection for Colab-portable snippets.

Edge Step 01/02/03 research scripts run standalone on Google Colab (only the
single script + data files travel there, never the `shared/` package), so
they cannot import `shared.models.financial_cv` at runtime -- the verified
purge/embargo logic is instead copy-pasted directly into each script. This
test does not require those scripts to import anything; it independently
re-parses every edge script's local `purge_train_overlaps`/`apply_embargo`
function bodies and asserts they are byte-for-byte identical (after
whitespace normalization) to the single canonical copy in
`shared/colab_portable/purge_embargo_snippet.py`. This is how "one
authoritative source" is enforced for code that cannot literally share one
running copy: not by import, but by an automated equality check every time
the test suite runs.

See docs/mldp/A3_PURGE_EMBARGO_VERIFICATION.md for the full investigation
this test formalizes.
"""
from __future__ import annotations

import glob
import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CANONICAL_PATH = _REPO_ROOT / "shared" / "colab_portable" / "purge_embargo_snippet.py"

_FUNCTION_PATTERN = "def {name}\\(.*?\\n(?=\\ndef |\\nclass |\\n#|\\Z)"


def _extract_function_body(source: str, function_name: str) -> str | None:
    match = re.search(_FUNCTION_PATTERN.format(name=function_name), source, re.S)
    if match is None:
        return None
    return re.sub(r"\s+", " ", match.group(0)).strip()


def _canonical_bodies() -> dict[str, str]:
    source = _CANONICAL_PATH.read_text(encoding="utf-8")
    bodies = {
        name: _extract_function_body(source, name)
        for name in ("purge_train_overlaps", "apply_embargo")
    }
    missing = [name for name, body in bodies.items() if body is None]
    if missing:
        raise AssertionError(
            f"Canonical snippet file is missing expected function(s): {missing}. "
            f"Checked: {_CANONICAL_PATH}"
        )
    return bodies


class TestColabPortableSnippetSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.canonical = _canonical_bodies()
        pattern = str(_REPO_ROOT / "assets" / "*" / "edges" / "*" / "research" / "*.py")
        cls.edge_scripts = sorted(glob.glob(pattern))
        if not cls.edge_scripts:
            raise AssertionError(f"No edge research scripts found under pattern: {pattern}")

    def test_canonical_snippet_file_exists_and_is_self_consistent(self):
        self.assertTrue(_CANONICAL_PATH.exists())
        self.assertEqual(set(self.canonical.keys()), {"purge_train_overlaps", "apply_embargo"})

    def test_every_edge_copy_matches_the_canonical_snippet(self):
        mismatches: list[str] = []
        checked = 0
        for path in self.edge_scripts:
            source = Path(path).read_text(encoding="utf-8", errors="ignore")
            if "def purge_train_overlaps" not in source:
                continue  # this script doesn't carry a local copy at all
            checked += 1
            for name, canonical_body in self.canonical.items():
                actual_body = _extract_function_body(source, name)
                if actual_body != canonical_body:
                    mismatches.append(f"{path} :: {name}")
        self.assertGreater(
            checked, 0, "Expected at least one edge script with a local purge_train_overlaps copy."
        )
        self.assertEqual(
            mismatches,
            [],
            msg=(
                f"{len(mismatches)} edge script(s) have drifted from the canonical "
                f"Colab-portable snippet at {_CANONICAL_PATH}:\n  "
                + "\n  ".join(mismatches[:20])
                + ("\n  ... (truncated)" if len(mismatches) > 20 else "")
            ),
        )


if __name__ == "__main__":
    unittest.main()
