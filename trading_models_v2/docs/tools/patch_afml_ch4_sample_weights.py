"""AFML Ch.4 walkthrough patch (owner decision 2026-10-23) for every edge's research Steps 01-03.

1. Class weights applied ONCE (AFML s.4.8 p.71-72). Steps 02/03 put class_weight='balanced' on every tree (recomputed
   on each sequential-bootstrap draw = 'balanced_subsample') AND multiplied compute_sample_weight('balanced') into
   sample_weight; sklearn multiplies the two, so balancing was squared. make_train_weights now carries only the AFML
   financial weight; the estimator does the balancing. Step 01 (XGBoost has no class_weight) keeps its single factor.
2. Event end = the training label's own t1 (Task 2A label_t1, fallback gross_t1), snapped to the bar that contains
   it, for uniqueness, return attribution (Snippets 4.1/4.2/4.10) and the sequential bootstrap (Snippets 4.3-4.5).
   Purge/embargo still use gross_t1 (reviewed in Ch.7).
3. Time decay (Snippet 4.11 p.70) wired with TIME_DECAY_C = 1.0 (no decay; c is chosen in Ch.9).
4. Weight contract bumped to quantara_afml_ch4_bar_clock_return_attribution_v2_label_t1 (must equal the shared module).

Idempotent: a file already carrying the v2 contract is skipped. Every anchor must occur exactly once or the file is
left untouched and the run fails.

usage (from trading_models_v2):  python ./docs/tools/patch_afml_ch4_sample_weights.py [--edges ASSET/edge ...]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
OLD_CONTRACT = 'AFML_SAMPLE_WEIGHT_CONTRACT_VERSION = "quantara_afml_ch4_bar_clock_return_attribution_v1"'
NEW_CONTRACT = 'AFML_SAMPLE_WEIGHT_CONTRACT_VERSION = "quantara_afml_ch4_bar_clock_return_attribution_v2_label_t1"'
STEPS = ("01_feature_surgery_clustered_mda.py", "02_train_meta_model_optuna.py", "03_generate_oof_predictions.py")

HELPERS = '''
# AFML Ch.4 walkthrough 2026-10-23: event end = the training label's own t1 (Task 2A label_t1, fallback gross_t1),
# snapped to the bar that contains it (first bar whose Datetime >= the tick time). Local copy of
# shared/models/afml_sample_weights.label_end_times / snap_to_bar_clock so flat_colab mode stays self-contained.
def _label_end_times(frame: pd.DataFrame) -> pd.DatetimeIndex:
    fallback = pd.to_datetime(frame[T1_COL], utc=True, errors="coerce")
    if LABEL_T1_COL in frame.columns:
        fallback = pd.to_datetime(frame[LABEL_T1_COL], utc=True, errors="coerce").fillna(fallback)
    return pd.DatetimeIndex(fallback)


def _snap_to_bar_clock(times: pd.DatetimeIndex, bar_index: pd.DatetimeIndex) -> np.ndarray:
    pos = np.searchsorted(bar_index.as_unit("ns").asi8, pd.DatetimeIndex(times).as_unit("ns").asi8, side="left")
    if (pos >= len(bar_index)).any():
        raise {err}(f"{{int((pos >= len(bar_index)).sum())}} event end time(s) fall after the last bar.")
    return pos.astype("int64")


def _time_decay_factors(index: pd.Index, uniqueness: np.ndarray) -> np.ndarray:
    """AFML Snippet 4.11 (p.70) on cumulative uniqueness; TIME_DECAY_C = 1.0 returns all ones."""
    if TIME_DECAY_C == 1.0:
        return np.ones(len(uniqueness), dtype="float64")
    tw = pd.Series(np.asarray(uniqueness, dtype="float64"), index=pd.DatetimeIndex(index))
    cum = tw.sort_index().cumsum()
    total = float(cum.iloc[-1])
    slope = (1.0 - TIME_DECAY_C) / total if TIME_DECAY_C >= 0 else 1.0 / ((TIME_DECAY_C + 1.0) * total)
    decay = (1.0 - slope * total + slope * cum).clip(lower=0.0)
    return decay.reindex(tw.index).to_numpy(dtype="float64")

'''

CONSTANTS = '''T1_COL = "gross_t1"
# AFML Ch.4 walkthrough 2026-10-23: sample weights + sequential bootstrap end each event at the training label's own
# t1 (Task 2A label_t1), snapped to the bar clock; purge/embargo keep T1_COL until the Ch.7 review.
LABEL_T1_COL = "label_t1"
# AFML Snippet 4.11 time-decay parameter c (1.0 = no decay). Chosen in the Ch.9 hyper-parameter review, not here.
TIME_DECAY_C = 1.0
'''

LOCAL_ENDS_OLD = '''    ends = pd.DatetimeIndex(events[T1_COL])
    bar_index = pd.DatetimeIndex(bars[TIME_COL])
'''
LOCAL_ENDS_NEW = '''    bar_index = pd.DatetimeIndex(bars[TIME_COL])
    ends = bar_index[_snap_to_bar_clock(_label_end_times(events), bar_index)]
'''

MAKE_TRAIN_OLD = '''    class_w = compute_class_weights(y_train, class_weight_mode=class_weight_mode)
    financial_w = _afml_return_weights(train_df)
    final_w = np.asarray(class_w, dtype="float64") * financial_w
'''
MAKE_TRAIN_NEW = '''    # AFML s.4.8 (p.71-72): class balance is applied ONCE, by the estimator's class_weight='balanced' (recomputed on
    # every sequential-bootstrap draw = 'balanced_subsample'). Multiplying compute_sample_weight('balanced') in here
    # as well squared the balancing (fixed 2026-10-23, Ch.4 walkthrough); compute_class_weights only validates mode.
    compute_class_weights(y_train, class_weight_mode=class_weight_mode)
    financial_w = _afml_return_weights(train_df)
    final_w = np.asarray(financial_w, dtype="float64") * _time_decay_factors(train_df.index, compute_uniqueness(train_df))
'''


def _once(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        raise RuntimeError(f"anchor {label!r} found {n} times")
    return text.replace(old, new)


def patch_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if NEW_CONTRACT in text:
        return "already patched"
    err = re.search(r"^class (\w+Error)\(RuntimeError\)", text, re.M)
    if not err:
        raise RuntimeError("no *Error(RuntimeError) class found")
    step = path.name[:2]

    text = _once(text, OLD_CONTRACT, NEW_CONTRACT, "contract")
    text = _once(text, 'T1_COL = "gross_t1"\n', CONSTANTS, "T1_COL constant")
    # local (flat_colab) weight table: read label_t1 too, end events at the snapped label t1
    text = _once(text, "        events = pd.read_csv(model_rows_path, usecols=[TIME_COL, T1_COL])",
                 "        events = pd.read_csv(model_rows_path, usecols=lambda c: c in (TIME_COL, T1_COL, LABEL_T1_COL))",
                 "local usecols")
    text = _once(text, LOCAL_ENDS_OLD, LOCAL_ENDS_NEW, "local ends")
    # shared weight table call
    text = _once(text, "                t1_col=T1_COL,\n", "                t1_col=T1_COL,\n                label_t1_col=LABEL_T1_COL,\n",
                 "shared call")
    # helpers before the local weight-table function
    anchor = "def _compute_afml_event_weight_table_local("
    text = _once(text, anchor, HELPERS.format(err=err.group(1)).lstrip("\n") + "\n" + anchor, "helpers")

    if step in ("02", "03"):
        # sequential bootstrap spans
        old = ('    ends = pd.DatetimeIndex(pd.to_datetime(df[T1_COL], utc=True, errors="coerce"))\n')
        text = _once(text, old, "    ends = _label_end_times(df)\n", "bootstrap ends")
        text = _once(text, "    end_pos = bar_index.get_indexer(ends)\n    if (start_pos < 0).any() or (end_pos < 0).any():",
                     "    end_pos = _snap_to_bar_clock(ends, bar_index)\n    if (start_pos < 0).any() or (end_pos < 0).any():",
                     "bootstrap end_pos")
        text = _once(text, MAKE_TRAIN_OLD, MAKE_TRAIN_NEW, "make_train_weights")
        text = _once(text, '"""Quantara fit weight = class imbalance factor × canonical AFML return weight."""',
                     '"""Fit weight = canonical AFML return-attribution weight x time decay; class balance is the estimator\'s."""',
                     "make_train_weights docstring")
    path.write_text(text, encoding="utf-8")
    return "patched"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edges", nargs="*")
    args = ap.parse_args(argv)
    if args.edges:
        roots = [V2 / "assets" / e.split("/")[0] / "edges" / e.split("/")[1] for e in args.edges]
    else:
        roots = sorted(p for p in (V2 / "assets").glob("*/edges/*") if (p / "research").is_dir())
    failed = 0
    for root in roots:
        for step in STEPS:
            path = root / "research" / step
            if not path.exists():
                print(f"MISSING  {path.relative_to(V2)}")
                failed += 1
                continue
            try:
                print(f"{patch_file(path):16s} {path.relative_to(V2)}")
            except RuntimeError as exc:
                print(f"FAILED   {path.relative_to(V2)}: {exc}")
                failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
