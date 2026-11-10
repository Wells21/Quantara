"""Load every .pkl / .joblib artifact under assets/, portfolio_live/, shared/ and config/ with the CURRENT interpreter.

Run it on venv39 (live, Phase C, allocator) after anything written on venv312 (pandas 3): a pandas object pickled
under pandas 3 does not unpickle on pandas 2 (found 2026-10-30, AFML_MLAM_WALKTHROUGH.md s.4Q.4). Exit code 1 if any
file fails.

usage (from trading_models_v2):  ..\\venv39\\Scripts\\python.exe docs\\tools\\scan_pickles_loadable.py
"""
from __future__ import annotations

import glob
import json
import sys
import warnings
from pathlib import Path

import joblib

V2 = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(V2), str(V2.parent)]
warnings.filterwarnings("ignore")

PATTERNS = ("assets/**/*.pkl", "assets/**/*.joblib", "portfolio_live/**/*.pkl", "portfolio_live/**/*.joblib",
            "shared/**/*.pkl", "config/**/*.pkl")


def main() -> int:
    files = sorted({f for pat in PATTERNS for f in glob.glob(str(V2 / pat), recursive=True)
                    if "support_bundles" not in f and "archive" not in f.lower()})
    bad = []
    for f in files:
        try:
            joblib.load(f)
        except Exception as exc:  # noqa: BLE001 - report every failure
            bad.append({"file": str(Path(f).relative_to(V2)), "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
    print(json.dumps({"python": sys.version.split()[0], "files": len(files), "failed": len(bad)}))
    for b in bad:
        print(json.dumps(b))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
