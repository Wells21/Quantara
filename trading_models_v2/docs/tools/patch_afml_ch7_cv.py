"""AFML Ch.7 walkthrough patch (owner decision 2026-10-24): purge on the label's own t1, embargo scales with rows.

Every edge's Steps 01/02/03 and shared/calibration/fit_sigmoid_calibrator_and_thresholds.py (Step 04):
  1. purge (AFML s.7.4.1, Snippet 7.1 p.106) uses each event's training-label end time: Task 2A label_t1, falling back
     to gross_t1 (same t1 as the Ch.4 sample weights). Was gross_t1.
  2. embargo (AFML s.7.4.2 p.107, h ~ .01T) = max(MANUAL_EMBARGO_SIZE = 100, ceil(1% of rows)) observations after each
     contiguous test block. Was a flat 100. Manifests record the rule ("embargo_rule") and the purge t1 ("purge_t1").
Idempotent. Every anchor must occur exactly once or the file is left untouched and the run fails.

usage (from trading_models_v2):  python ./docs/tools/patch_afml_ch7_cv.py [--edges ASSET/edge ...]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
MARK = "AFML Ch.7 walkthrough 2026-10-24"
RULE = "max(100, ceil(0.01 x rows)) observations after each contiguous test block (AFML s.7.4.2 p.107)"

CONST_OLD = "MANUAL_EMBARGO_SIZE = 100\n"
CONST_NEW = f'''MANUAL_EMBARGO_SIZE = 100  # minimum embargo (observations)
# {MARK}: embargo h ~ .01T (AFML s.7.4.2 p.107) -> max(100, ceil(1% of rows)); purge on the label's own t1.
EMBARGO_PCT = 0.01
EMBARGO_RULE = "{RULE}"
PURGE_T1_RULE = "label_t1 (Task 2A training-label end), fallback gross_t1"


def embargo_size_for(n_rows: int) -> int:
    return max(MANUAL_EMBARGO_SIZE, int(np.ceil(EMBARGO_PCT * int(n_rows))))
'''

STEP_PAIRS = [
    ("        train_idx = purge_train_overlaps(train_idx, test_idx, df.index, df[T1_COL])\n"
     "        train_idx = apply_embargo(train_idx, test_idx, len(df), MANUAL_EMBARGO_SIZE)\n",
     "        train_idx = purge_train_overlaps(train_idx, test_idx, df.index, pd.Series(_label_end_times(df), index=df.index))\n"
     "        train_idx = apply_embargo(train_idx, test_idx, len(df), embargo_size_for(len(df)))\n"),
    ('            "manual_embargo_size": MANUAL_EMBARGO_SIZE,\n',
     '            "manual_embargo_size": MANUAL_EMBARGO_SIZE,\n'
     '            "embargo_rule": EMBARGO_RULE,\n'
     '            "purge_t1": PURGE_T1_RULE,\n'),
]

CAL_PAIRS = [
    ("        train_idx = purge_train_overlaps(train_idx=train_idx, test_idx=test_idx, index=work.index, t1=work[T1_COL])\n"
     "        train_idx = apply_embargo(train_idx=train_idx, test_idx=test_idx, n_samples=n, embargo_size=MANUAL_EMBARGO_SIZE)\n",
     "        train_idx = purge_train_overlaps(train_idx=train_idx, test_idx=test_idx, index=work.index, t1=_purge_t1(work))\n"
     "        train_idx = apply_embargo(train_idx=train_idx, test_idx=test_idx, n_samples=n, embargo_size=embargo_size_for(n))\n"),
    ('            "embargo_size": EMBARGO_SIZE, "manual_embargo_size": MANUAL_EMBARGO_SIZE,\n',
     '            "embargo_size": EMBARGO_SIZE, "manual_embargo_size": MANUAL_EMBARGO_SIZE,\n'
     '            "embargo_rule": EMBARGO_RULE, "purge_t1": PURGE_T1_RULE,\n'),
    ("def purge_train_overlaps(",
     '''def _purge_t1(frame: pd.DataFrame) -> pd.Series:
    """Training-label end per OOF row: label_t1 (Task 2A), fallback gross_t1 (AFML Ch.7 walkthrough 2026-10-24)."""
    fallback = pd.to_datetime(frame[T1_COL], utc=True, errors="coerce")
    if "label_t1" in frame.columns:
        fallback = pd.to_datetime(frame["label_t1"], utc=True, errors="coerce").fillna(fallback)
    return pd.Series(fallback.to_numpy(), index=frame.index)


def purge_train_overlaps('''),
]


def _apply(path: Path, pairs: list[tuple[str, str]]) -> str:
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        return "already patched"
    for old, new in [(CONST_OLD, CONST_NEW), *pairs]:
        n = text.count(old)
        if n != 1:
            raise RuntimeError(f"anchor found {n} times: {old[:70]!r}")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return "patched"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edges", nargs="*")
    args = ap.parse_args(argv)
    targets: list[tuple[Path, list]] = []
    if not args.edges:
        targets.append((V2 / "shared/calibration/fit_sigmoid_calibrator_and_thresholds.py", CAL_PAIRS))
    roots = ([V2 / "assets" / e.split("/")[0] / "edges" / e.split("/")[1] for e in args.edges] if args.edges
             else sorted(p for p in (V2 / "assets").glob("*/edges/*") if (p / "research").is_dir()))
    for root in roots:
        for name in ("01_feature_surgery_clustered_mda.py", "02_train_meta_model_optuna.py", "03_generate_oof_predictions.py"):
            targets.append((root / "research" / name, STEP_PAIRS))
    failed = 0
    for path, pairs in targets:
        try:
            print(f"{_apply(path, pairs):16s} {path.relative_to(V2)}")
        except (RuntimeError, FileNotFoundError) as exc:
            print(f"FAILED           {path.relative_to(V2)}: {exc}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
