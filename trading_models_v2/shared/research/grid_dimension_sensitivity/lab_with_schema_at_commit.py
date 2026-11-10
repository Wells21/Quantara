"""Run v3_horizon_lab with each edge's feature_schema.json as of one git commit (EWM span test, 2026-10-31).

Why: the stored research datasets were exported with the pre-Ch.17 schema (168 entries); the current schema (187) names
columns they do not hold until the RECERT re-export, and the lab uses the schema to pick valid bar times (Gate A control
parity, Gate C eligibility). Reading the schema at the commit the old full replays ran under (63f4a570) reproduces those
runs on the same data. Only schema_feature_names() is replaced; the lab is otherwise unchanged.

usage: python lab_with_schema_at_commit.py <all v3_horizon_lab arguments>     (env SCHEMA_COMMIT, default 63f4a570)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(V2))
import shared.research.v3_horizon_lab as lab  # noqa: E402

COMMIT = os.environ.get("SCHEMA_COMMIT", "63f4a570")


def schema_feature_names_at_commit(runtime) -> list[str]:
    rel = (Path(runtime.__file__).resolve().parent / "contracts" / "feature_schema.json").relative_to(V2.parent).as_posix()
    doc = json.loads(subprocess.run(["git", "show", f"{COMMIT}:{rel}"], cwd=V2.parent, capture_output=True,
                                    check=True).stdout)
    names = [str(f["feature_name"]) for f in doc["features"]]
    print(f"schema_feature_names from {COMMIT}:{rel} -> {len(names)} features", flush=True)
    if not names:
        raise lab.HorizonLabError("Edge feature schema contains zero features.")
    return names


lab.schema_feature_names = schema_feature_names_at_commit

if __name__ == "__main__":
    sys.argv[0] = str(Path(lab.__file__))
    lab.main()
