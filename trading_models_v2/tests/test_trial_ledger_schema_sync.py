"""Drift detection between the Colab-portable trial-ledger snippet's
hardcoded required-field list and the real schema file. Same discipline as
tests/test_colab_portable_snippet_sync.py: the snippet must stay usable
without a runtime import of the schema file (it may run standalone on
Colab), so its required-field list is hardcoded -- this test is what keeps
that hardcoded copy honest.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from shared.colab_portable.trial_ledger_snippet import REQUIRED_FIELDS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_PATH = _REPO_ROOT / "config" / "research_trial_ledger.schema.json"


class TestTrialLedgerSchemaSync(unittest.TestCase):
    def test_hardcoded_required_fields_match_schema_file(self):
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        schema_required = set(schema["required"])
        snippet_required = set(REQUIRED_FIELDS)
        self.assertEqual(
            schema_required,
            snippet_required,
            msg=(
                f"Drift detected between {_SCHEMA_PATH} 'required' array and "
                f"shared/colab_portable/trial_ledger_snippet.py's "
                f"REQUIRED_FIELDS. Schema-only: "
                f"{schema_required - snippet_required}; snippet-only: "
                f"{snippet_required - schema_required}."
            ),
        )


if __name__ == "__main__":
    unittest.main()
