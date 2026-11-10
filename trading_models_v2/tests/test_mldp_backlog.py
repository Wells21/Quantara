"""Owner rule 2026-10-26: nothing approved in a chapter may be forgotten. Every deviation-register item whose status says
it is not yet fully in effect must appear in config/mldp_implementation_backlog.json.

Run from trading_models_v2:  python -m unittest tests.test_mldp_backlog
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

V2 = Path(__file__).resolve().parents[1]
OPEN_MARKERS = ("retrain", "pending", "open", "recorded", "approved_", "still_open")
CLOSED_MARKERS = ("closed", "resolved_by_chapter", "superseded")


class BacklogCoversOpenItems(unittest.TestCase):
    def test_every_open_register_item_is_in_the_backlog(self) -> None:
        reg = json.loads((V2 / "config" / "mldp_deviation_register.json").read_text(encoding="utf-8"))["items"]
        backlog = json.loads((V2 / "config" / "mldp_implementation_backlog.json").read_text(encoding="utf-8"))
        listed = {i["id"] for i in backlog["items"]}
        states = set(backlog["states"])
        for i in backlog["items"]:
            self.assertIn(i["state"], states, i["id"])
            self.assertTrue(i["left"], i["id"])
        missing = []
        for it in reg:
            st = str(it.get("status", "")).lower()
            is_open = any(m in st for m in OPEN_MARKERS) and not st.startswith(CLOSED_MARKERS)
            # approved deviations that need no further work (e.g. a kept design) say so explicitly
            if "no code change" in st or "no further work" in st:
                is_open = False
            if is_open and it["id"] not in listed:
                missing.append(f"{it['id']}: {it['status'][:80]}")
        self.assertEqual(missing, [], "open register items missing from config/mldp_implementation_backlog.json")


if __name__ == "__main__":
    unittest.main()
