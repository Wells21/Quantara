"""AFML Ch.12 walkthrough patch (owner decision D052, 2026-10-26) for every edge's Step 03.

AFML s.12.4.2 step 5 (pp.165-166): CPCV assembles phi[N, k] complete out-of-sample backtest PATHS; the strategy is judged
on the distribution over paths, not on one series. Step 03 already ran CombinatorialPurgedCV(5, 2) but averaged each
row's forecasts into one raw_probability. This patch:
  1. N_FOLDS 5 -> 10 (k = 2 kept): 45 splits, phi = 9 paths, training share 0.8 (rule of thumb N = phi + 1, p.165);
  2. keeps every path: an inlined helper assigns each test group's forecasts to its path (skfolio get_path_ids) and the
     OOF file gains one column per path, cpcv_path_XX_raw_probability (raw_probability stays as the averaged diagnostic);
  3. records the paths in the Step 03 manifest.
Inlined, not imported: Step 02/03 run standalone (Colab) and cannot import shared/ (same rule as the bagging block);
tests/test_afml_ch12_cpcv_paths.py checks every copy is identical and correct. Idempotent; every anchor must be unique.

usage (from trading_models_v2):  python ./docs/tools/patch_afml_ch12_step03_cpcv_paths.py [--edges ASSET/edge ...]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
MARK = "# QUANTARA_AFML_CH12_CPCV_PATHS_V1"

HELPER = '''
# QUANTARA_AFML_CH12_CPCV_PATHS_V1
# AFML s.12.4.2 step 5 (pp.165-166), owner decision D052 (2026-10-26): keep every CPCV backtest path.
CPCV_PATH_COLUMN_TEMPLATE = "cpcv_path_{:02d}_raw_probability"


def assign_cpcv_path_predictions(path_probs: np.ndarray, path_ids: np.ndarray, split_index: int,
                                 test_parts: list, test_idx: np.ndarray, preds: np.ndarray) -> None:
    """Write one split's test forecasts into its paths: test group j of split s belongs to path path_ids[s, j]."""
    by_row = dict(zip(np.asarray(test_idx, dtype=int).tolist(), np.asarray(preds, dtype=float).tolist()))
    for j, part in enumerate(test_parts):
        rows = np.atleast_1d(np.asarray(part, dtype=int))
        path_probs[int(path_ids[split_index, j]), rows] = [by_row[int(r)] for r in rows]
# END_QUANTARA_AFML_CH12_CPCV_PATHS_V1
'''

PAIRS = [
    ("N_FOLDS = 5\nN_TEST_FOLDS = 2\n",
     "N_FOLDS = 10  # AFML Ch.12 walkthrough (D052): N = phi + 1 with k = 2 -> 9 CPCV paths, 45 splits (s.12.4.3 p.165)\nN_TEST_FOLDS = 2\n"),
    ("def generate_oof_predictions(df: pd.DataFrame, x_data: pd.DataFrame, y: pd.Series, best_params: dict[str, Any]):",
     HELPER.lstrip("\n") + "\n\ndef generate_oof_predictions(df: pd.DataFrame, x_data: pd.DataFrame, y: pd.Series, best_params: dict[str, Any]):"),
    ('''    prob_sum = np.zeros(len(df), dtype=float)
    prob_count = np.zeros(len(df), dtype=int)
''',
     '''    prob_sum = np.zeros(len(df), dtype=float)
    prob_count = np.zeros(len(df), dtype=int)
    path_ids = np.asarray(cv.get_path_ids())
    path_probs = np.full((int(cv.n_test_paths), len(df)), np.nan, dtype=float)
'''),
    ('''    for fold_num, (train_idx, test_idx) in enumerate(cv.split(x_data), start=1):
        raw_train_rows = len(flatten_cv_indices(train_idx))
''',
     '''    for fold_num, (train_idx, test_idx) in enumerate(cv.split(x_data), start=1):
        test_parts = list(test_idx) if isinstance(test_idx, (list, tuple)) else [test_idx]
        raw_train_rows = len(flatten_cv_indices(train_idx))
'''),
    ('''        prob_sum[test_idx] += preds
        prob_count[test_idx] += 1
''',
     '''        prob_sum[test_idx] += preds
        prob_count[test_idx] += 1
        assign_cpcv_path_predictions(path_probs, path_ids, fold_num - 1, test_parts, test_idx, preds)
'''),
    ('''    oof_df["oof_pred_count"] = prob_count
''',
     '''    oof_df["oof_pred_count"] = prob_count
    for path_no in range(path_probs.shape[0]):  # AFML s.12.4.2 step 5: every path kept (D052)
        oof_df[CPCV_PATH_COLUMN_TEMPLATE.format(path_no)] = path_probs[path_no]
'''),
    ('''        "cpcv": {
            "n_folds": N_FOLDS,
            "n_test_folds": N_TEST_FOLDS,
''',
     '''        "cpcv": {
            "n_folds": N_FOLDS,
            "n_test_folds": N_TEST_FOLDS,
            "n_paths": int(sum(1 for c in oof_df.columns if str(c).startswith("cpcv_path_"))),
            "path_columns": sorted(c for c in oof_df.columns if str(c).startswith("cpcv_path_")),
            "path_policy": "every CPCV path kept (AFML s.12.4.2 step 5, D052); raw_probability = average of paths, diagnostic",
'''),
    ('''    print(f"CPCV combinations expected: C({N_FOLDS}, {N_TEST_FOLDS}) = 10 folds", flush=True)''',
     '''    print(f"CPCV: C({N_FOLDS}, {N_TEST_FOLDS}) splits; every backtest path kept separately (AFML s.12.4, D052)", flush=True)'''),
]


def patch(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        return "already patched"
    for old, new in PAIRS:
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
    roots = ([V2 / "assets" / e.split("/")[0] / "edges" / e.split("/")[1] for e in args.edges] if args.edges
             else sorted(p for p in (V2 / "assets").glob("*/edges/*") if (p / "research").is_dir()))
    failed = 0
    for root in roots:
        path = root / "research" / "03_generate_oof_predictions.py"
        try:
            print(f"{patch(path):16s} {path.relative_to(V2)}")
        except (RuntimeError, FileNotFoundError) as exc:
            print(f"FAILED           {path.relative_to(V2)}: {exc}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
