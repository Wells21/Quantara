"""AFML Ch.18 "Entropy Features".

Source: Marcos Lopez de Prado, *Advances in Financial Machine Learning*
(AFML, 2018), Chapter 18, book p.263-279. Ports Snippets 18.1 (plug-in/ML
entropy estimator, p.265), 18.2 (Lempel-Ziv library, p.266), 18.3 (longest-
match length, p.267), and 18.4 (Kontoyiannis' LZ entropy-rate estimator --
the book's own preferred, most-refined estimator of the three, p.268-269).

Features (AFML Ch.18 walkthrough, owner approved 2026-10-31; D066-D069):
* r_t = log(Close_t) - log(Close_{t-1}) on the bars ending at the CURRENT bar
  (lag 0, as Ch.17 D061); the message is the last ENTROPY_WINDOW returns.
* Three encodings of s.18.5 (p.269-271): binary (sign, zeros removed), quantile
  (10 letters, boundaries from the research history = the in-sample period) and
  sigma (step sigma = the standard deviation of all research-history bar
  returns, exercise 18.1(c); codes counted from the message's own minimum). The
  boundaries and sigma are frozen per asset like the FFD d
  (assets/{A}/bars/entropy/entropy_encoding_manifest.json); holdout and live
  never re-estimate them.
* Snippet 18.4 with its default expanding window (window=None); a message of
  odd length loses its first letter (p.269 "remove the first bit").
* Not the fractionally differentiated series of p.270: its level gives one
  letter in 29-57 % of 100-bar windows and its first difference is no longer
  fractional (registered deviation D068).

`entropy_row` is the single rule for research (`entropy_features`, every
feature_expansion.py) and live (native_one_row_feature_engine), so the two are
identical by construction.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

V2_ROOT = Path(__file__).resolve().parents[2]
ENTROPY_WINDOW = 100
ENTROPY_MIN_MESSAGE = 8
QUANTILE_LETTERS = 10
ENTROPY_COLUMNS = {
    "Entropy_Kontoyiannis_Binary_100": "binary",
    "Entropy_Kontoyiannis_Quantile10_100": "quantile10",
    "Entropy_Kontoyiannis_Sigma_100": "sigma",
}
ENCODING_MANIFEST_SCHEMA = "afml_ch18_entropy_encoding_v1"


class EntropyFeatureError(RuntimeError):
    """Fail-closed error for the AFML Ch.18 entropy module."""


def binary_sign_encode(returns: pd.Series) -> str:
    """AFML p.270 (Section 18.5.1): encode a return series as a message
    over the binary alphabet {'0','1'}: '1' if r_t > 0, '0' if r_t < 0,
    dropping r_t == 0 observations (the book's own stated rule)."""
    values = returns.to_numpy(dtype="float64")
    values = values[np.isfinite(values) & (values != 0.0)]
    return "".join("1" if v > 0 else "0" for v in values)


def quantile_encode(returns: np.ndarray, boundaries: np.ndarray) -> str:
    """AFML p.270 (Section 18.5.2): code k for r_t in [b_{k-1}, b_k), with the
    boundaries b_1..b_{q-1} taken from the in-sample period (q <= 10 letters)."""
    codes = np.searchsorted(np.asarray(boundaries, dtype="float64"), returns, side="right")
    return "".join(map(str, codes))


def sigma_encode(returns: np.ndarray, sigma: float) -> str:
    """AFML p.270-271 (Section 18.5.3): code k for r_t in [min{r} + k*sigma,
    min{r} + (k+1)*sigma), ceil[(max{r} - min{r}) / sigma] codes in total (a
    maximum on the last edge goes to the last code). One character per code:
    chr(48 + k), i.e. '0'..'9' then ':' ';' ... (only equality matters)."""
    r = np.asarray(returns, dtype="float64")
    lo, hi = float(r.min()), float(r.max())
    n_codes = max(1, math.ceil((hi - lo) / sigma))
    codes = np.minimum(np.floor((r - lo) / sigma).astype("int64"), n_codes - 1)
    return "".join(chr(48 + int(k)) for k in codes)


def pmf1(msg: str, w: int) -> dict[str, float]:
    """AFML Snippet 18.1 (book p.265): probability mass function of all
    length-w substrings ("words") occurring in msg. Counts all len-w+1 words;
    the book's loop range(w, len(msg)) drops the last one (registered
    deviation D067). Not used by any feature."""
    lib: dict[str, list[int]] = {}
    if not isinstance(msg, str):
        msg = "".join(map(str, msg))
    for i in range(w, len(msg) + 1):
        msg_ = msg[i - w : i]
        if msg_ not in lib:
            lib[msg_] = [i - w]
        else:
            lib[msg_] = lib[msg_] + [i - w]
    denom = float(len(msg) - w + 1)
    return {key: len(value) / denom for key, value in lib.items()}


def plug_in(msg: str, w: int) -> tuple[float, dict[str, float]]:
    """AFML Snippet 18.1 (book p.265): plug-in (maximum-likelihood) entropy
    rate estimate, H_hat_{n,w} = -(1/w) * sum_y pmf_w[y]*log2(pmf_w[y])."""
    pmf = pmf1(msg, w)
    out = -sum(pmf[i] * np.log2(pmf[i]) for i in pmf) / w
    return float(out), pmf


def lempel_ziv_lib(msg: str) -> list[str]:
    """AFML Snippet 18.2 (book p.266): build the Lempel-Ziv dictionary of
    non-redundant substrings composing msg."""
    i, lib = 1, [msg[0]]
    while i < len(msg):
        for j in range(i, len(msg)):
            msg_ = msg[i : j + 1]
            if msg_ not in lib:
                lib.append(msg_)
                break
        i = j + 1
    return lib


def lempel_ziv_entropy_rate(msg: str) -> float:
    """LZ compression-rate estimate of entropy: dictionary size relative to
    message length (book p.265-266's stated intuition -- complex/high-
    entropy messages require larger dictionaries relative to their length)."""
    if not msg:
        raise EntropyFeatureError("Cannot estimate entropy of an empty message.")
    return float(len(lempel_ziv_lib(msg)) / len(msg))


def match_length(msg: str, i: int, n: int) -> tuple[int, str]:
    """AFML Snippet 18.3 (book p.267): 1 + the longest l <= n such that the l
    letters starting at i match l letters starting at some j in [i-n, i)
    (overlap allowed), and that matched substring (Gao, Kontoyiannis and
    Bienenstock 2008, p.6: L = 1 + the longest match length; the formula
    printed on p.266 is one short). The book's code tries every length l for
    every j; this compares letter by letter, which returns the same pair (a
    longer match contains every shorter one) -- tests/test_afml_ch18_entropy.py
    checks it against the verbatim snippet. Requires i >= n."""
    if i < n:
        raise EntropyFeatureError(f"match_length requires i >= n (i={i}, n={n})")
    best, size = 0, len(msg)
    for j in range(i - n, i):
        k = 0
        while k < n and i + k < size and msg[j + k] == msg[i + k]:
            k += 1
        if k > best:
            best = k
            if best == n:
                break
    return best + 1, msg[i : i + best]


def kontoyiannis_entropy(msg: str, window: "int | None" = None) -> dict[str, Any]:
    """AFML Snippet 18.4 (book p.268-269): Kontoyiannis' LZ entropy-rate
    estimate -- the inverse of the average length of the shortest non-
    redundant substring, using a symmetric (centered) matching window per
    the book's own caveats (p.269) on why the window must be symmetric.

    window=None uses an expanding window (len(msg) should be even, p.268, or
    the last letter is never read). 'r' is the snippet's redundancy, divided
    by log2(len(msg)) as in the code (p.264 defines it with log2 of the
    alphabet size).
    """
    if not isinstance(msg, str):
        msg = "".join(map(str, msg))
    out: dict[str, Any] = {"num": 0, "sum": 0.0, "subS": []}
    if window is None:
        points = range(1, len(msg) // 2 + 1)
    else:
        window = min(window, len(msg) // 2)
        points = range(window, len(msg) - window + 1)
    for i in points:
        if window is None:
            length, msg_ = match_length(msg, i, i)
            out["sum"] += np.log2(i + 1) / length
        else:
            length, msg_ = match_length(msg, i, window)
            out["sum"] += np.log2(window + 1) / length
        out["subS"].append(msg_)
        out["num"] += 1
    out["h"] = out["sum"] / out["num"] if out["num"] else np.nan
    out["r"] = 1 - out["h"] / np.log2(len(msg)) if len(msg) > 1 else np.nan
    return out


def _even(msg: str) -> str:
    """p.269: 'One solution is to remove the first bit of a message with odd length.'"""
    return msg[len(msg) % 2 :]


def entropy_row(closes: np.ndarray, encoding: Mapping[str, Any]) -> dict[str, float]:
    """The three entropy features at one bar from the ENTROPY_WINDOW + 1 closes
    ending at that bar (lag 0). NaN for a window with a non-finite or
    non-positive close, or a message shorter than ENTROPY_MIN_MESSAGE letters."""
    out = {name: math.nan for name in ENTROPY_COLUMNS}
    c = np.asarray(closes, dtype="float64")
    if len(c) != ENTROPY_WINDOW + 1 or not np.isfinite(c).all() or (c <= 0).any():
        return out
    r = np.diff(np.log(c))
    msgs = {
        "binary": binary_sign_encode(pd.Series(r)),
        "quantile10": quantile_encode(r, np.asarray(encoding["quantile_boundaries"], dtype="float64")),
        "sigma": sigma_encode(r, float(encoding["sigma"])),
    }
    for name, kind in ENTROPY_COLUMNS.items():
        msg = _even(msgs[kind])
        if len(msg) >= ENTROPY_MIN_MESSAGE:
            out[name] = float(kontoyiannis_entropy(msg)["h"])
    return out


def entropy_features(close: pd.Series, encoding: Mapping[str, Any]) -> pd.DataFrame:
    """Research rule: entropy_row at every bar t on close[t-ENTROPY_WINDOW .. t]."""
    values = close.to_numpy(dtype="float64")
    out = {name: np.full(len(values), np.nan) for name in ENTROPY_COLUMNS}
    for t in range(ENTROPY_WINDOW, len(values)):
        row = entropy_row(values[t - ENTROPY_WINDOW : t + 1], encoding)
        for name in ENTROPY_COLUMNS:
            out[name][t] = row[name]
    return pd.DataFrame(out, index=close.index)


def encoding_manifest_path(asset: str, v2_root: Path = V2_ROOT) -> Path:
    return v2_root / "assets" / str(asset).upper() / "bars" / "entropy" / "entropy_encoding_manifest.json"


def build_encoding_manifest(asset: str, v2_root: Path = V2_ROOT) -> dict[str, Any]:
    """Freeze the quantile boundaries and sigma on the asset's full research
    history (the bars the FFD d is estimated on; sealed holdout and live never
    used), like frozen_d."""
    asset = str(asset).upper()
    ffd_manifest = json.loads((v2_root / "assets" / asset / "bars" / "ffd" / "ffd_manifest.json").read_text(encoding="utf-8"))
    bars_rel = ffd_manifest["source_bars_path"]
    bars_path = v2_root / bars_rel
    raw = bars_path.read_bytes()
    bars = pd.read_csv(bars_path, usecols=["Datetime", "Close"])
    close = pd.to_numeric(bars["Close"], errors="coerce").to_numpy(dtype="float64")
    if not np.isfinite(close).all() or (close <= 0).any():
        raise EntropyFeatureError(f"{asset}: research bars contain non-finite or non-positive closes")
    r = np.diff(np.log(close))
    levels = np.arange(1, QUANTILE_LETTERS) / QUANTILE_LETTERS
    boundaries = np.quantile(r, levels)
    codes = np.searchsorted(boundaries, r, side="right")
    return {
        "schema_version": ENCODING_MANIFEST_SCHEMA,
        "asset": asset,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_bars_path": bars_rel,
        "source_bars_sha256": hashlib.sha256(raw).hexdigest(),
        "source_bars_rows": int(len(close)),
        "source_first_datetime": str(bars["Datetime"].iloc[0]),
        "source_last_datetime": str(bars["Datetime"].iloc[-1]),
        "return_definition": "r_t = numpy.diff(numpy.log(Close)) on the research bars",
        "returns": int(len(r)),
        "estimation_policy": "full_research_history_then_freeze (as the FFD d); sealed holdout and live never used",
        "quantile_letters": QUANTILE_LETTERS,
        "quantile_levels": [float(x) for x in levels],
        "quantile_method": "numpy.quantile default (linear)",
        "quantile_boundaries": [float(x) for x in boundaries],
        "quantile_in_sample_letter_shares": [float(x) for x in np.bincount(codes, minlength=QUANTILE_LETTERS) / len(r)],
        "sigma": float(np.std(r)),
        "sigma_definition": "standard deviation of all research-history bar returns (AFML exercise 18.1(c)), numpy ddof=0",
        "zero_return_share": float(np.mean(r == 0.0)),
        "book": "AFML s.18.5.2 p.270 (quantile boundaries from the in-sample period), s.18.5.3 p.270-271 (sigma encoding), exercise 18.1(b)-(c) p.277",
    }


def validate_encoding(encoding: Mapping[str, Any], asset: str) -> dict[str, Any]:
    boundaries = np.asarray(encoding.get("quantile_boundaries", []), dtype="float64")
    sigma = float(encoding.get("sigma", math.nan))
    if encoding.get("schema_version") != ENCODING_MANIFEST_SCHEMA or str(encoding.get("asset")).upper() != str(asset).upper():
        raise EntropyFeatureError(f"{asset}: entropy encoding manifest has the wrong schema or asset")
    if len(boundaries) != QUANTILE_LETTERS - 1 or not np.isfinite(boundaries).all() or (np.diff(boundaries) < 0).any():
        raise EntropyFeatureError(f"{asset}: entropy quantile boundaries must be {QUANTILE_LETTERS - 1} finite sorted values")
    if not (math.isfinite(sigma) and sigma > 0):
        raise EntropyFeatureError(f"{asset}: entropy sigma must be finite and positive")
    return dict(encoding)


@lru_cache(maxsize=None)
def _load_encoding_cached(path: str, asset: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise EntropyFeatureError(f"{asset}: missing entropy encoding manifest {p} (shared/features/build_asset_entropy_encoding.py)")
    return validate_encoding(json.loads(p.read_text(encoding="utf-8")), asset)


def load_entropy_encoding(asset: str, v2_root: Path = V2_ROOT) -> dict[str, Any]:
    """Frozen per-asset quantile boundaries and sigma (read once per process)."""
    return _load_encoding_cached(str(encoding_manifest_path(asset, v2_root)), str(asset).upper())
