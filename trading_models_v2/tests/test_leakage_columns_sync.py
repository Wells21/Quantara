"""Drift detection for the LEAKAGE_COLS set inlined in Step 01/02.

Step 01/02 run standalone on Google Colab and cannot import shared/ at
runtime, so they carry their own inlined copy of
shared/labels/leakage_columns.py's LEAKAGE_COLS set instead of importing it.
This test re-parses every edge's Step 01/02 script, extracts its inlined
LEAKAGE_COLS set literal, resolves any bare-name entries (references to
module-level constants like TARGET_COL) to their string values, and asserts
the resulting set is exactly the canonical shared module's LEAKAGE_COLS --
the same drift-detection discipline already used for the purge/embargo,
denoise/ONC, and trial-ledger Colab-portable snippets.
"""
from __future__ import annotations

import glob
import re
import unittest
from pathlib import Path

from shared.labels.leakage_columns import LEAKAGE_COLS as CANONICAL_LEAKAGE_COLS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_NAMES = (
    "01_feature_surgery_clustered_mda.py",
    "02_train_meta_model_optuna.py",
    "03_generate_oof_predictions.py",
)

_ENTRY_PATTERN = re.compile(r'"((?:[^"\\]|\\.)*)"|([A-Za-z_][A-Za-z0-9_]*)')


def _strip_comments(body: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in body.splitlines())


def _extract_leakage_cols_entries(source: str) -> list[str] | None:
    match = re.search(r"^LEAKAGE_COLS = \{(.*?)^\}", source, re.S | re.M)
    if match is None:
        return None
    body = _strip_comments(match.group(1))
    entries = []
    for literal, name in _ENTRY_PATTERN.findall(body):
        entries.append(literal if literal or name == "" else name)
    return [e for e in entries if e]


def _resolve_entry(source: str, entry: str) -> str:
    """An entry is either already a literal string value, or a bare name
    referencing an earlier module-level `NAME = "value"` constant."""
    const_match = re.search(rf'^{re.escape(entry)} = "([^"]+)"', source, re.M)
    return const_match.group(1) if const_match else entry


class TestLeakageColumnsSync(unittest.TestCase):
    def test_every_step01_02_script_matches_canonical_leakage_cols(self):
        mismatches: list[str] = []
        checked = 0
        for script_name in _SCRIPT_NAMES:
            pattern = str(_REPO_ROOT / "assets" / "*" / "edges" / "*" / "research" / script_name)
            for path in sorted(glob.glob(pattern)):
                source = Path(path).read_text(encoding="utf-8", errors="ignore")
                entries = _extract_leakage_cols_entries(source)
                if entries is None:
                    mismatches.append(f"{path} :: LEAKAGE_COLS not found")
                    continue
                checked += 1
                actual = {_resolve_entry(source, e) for e in entries}
                if actual != set(CANONICAL_LEAKAGE_COLS):
                    missing = set(CANONICAL_LEAKAGE_COLS) - actual
                    extra = actual - set(CANONICAL_LEAKAGE_COLS)
                    mismatches.append(f"{path} :: missing={sorted(missing)} extra={sorted(extra)}")
        self.assertGreater(checked, 0, "Expected at least one Step01/02 script to check.")
        self.assertEqual(
            mismatches,
            [],
            msg=f"{len(mismatches)} script(s) drifted from canonical LEAKAGE_COLS:\n  "
            + "\n  ".join(mismatches[:20]),
        )


if __name__ == "__main__":
    unittest.main()
