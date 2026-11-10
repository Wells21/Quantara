"""Tests for shared/features/afml_ch18_entropy.py (AFML Ch.18 entropy features; D066-D069)."""
from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from shared.features.afml_ch18_entropy import (
    ENTROPY_COLUMNS,
    ENTROPY_WINDOW,
    EntropyFeatureError,
    binary_sign_encode,
    encoding_manifest_path,
    entropy_features,
    entropy_row,
    kontoyiannis_entropy,
    lempel_ziv_entropy_rate,
    lempel_ziv_lib,
    load_entropy_encoding,
    match_length,
    plug_in,
    pmf1,
    quantile_encode,
    sigma_encode,
    validate_encoding,
)


# --- AFML Snippets 18.1, 18.3, 18.4 verbatim (p.265, p.267, p.268-269); Py2 -> Py3 only: xrange -> range, / -> // ---
def book_pmf1(msg, w):
    lib = {}
    if not isinstance(msg, str): msg = ''.join(map(str, msg))
    for i in range(w, len(msg)):
        msg_ = msg[i - w:i]
        if msg_ not in lib: lib[msg_] = [i - w]
        else: lib[msg_] = lib[msg_] + [i - w]
    pmf = float(len(msg) - w)
    pmf = {i: len(lib[i]) / pmf for i in lib}
    return pmf


def book_matchLength(msg, i, n):
    subS = ''
    for l in range(n):
        msg1 = msg[i:i + l + 1]
        for j in range(i - n, i):
            msg0 = msg[j:j + l + 1]
            if msg1 == msg0:
                subS = msg1
                break
    return len(subS) + 1, subS


def book_konto(msg, window=None):
    out = {'num': 0, 'sum': 0, 'subS': []}
    if not isinstance(msg, str): msg = ''.join(map(str, msg))
    if window is None:
        points = range(1, len(msg) // 2 + 1)
    else:
        window = min(window, len(msg) // 2)
        points = range(window, len(msg) - window + 1)
    for i in points:
        if window is None:
            l, msg_ = book_matchLength(msg, i, i)
            out['sum'] += np.log2(i + 1) / l
        else:
            l, msg_ = book_matchLength(msg, i, window)
            out['sum'] += np.log2(window + 1) / l
        out['subS'].append(msg_)
        out['num'] += 1
    out['h'] = out['sum'] / out['num']
    out['r'] = 1 - out['h'] / np.log2(len(msg))
    return out


ENCODING = {
    "schema_version": "afml_ch18_entropy_encoding_v1", "asset": "TEST",
    "quantile_boundaries": list(np.quantile(np.random.default_rng(9).normal(0, 1e-3, 50_000), np.arange(1, 10) / 10)),
    "sigma": 1e-3,
}


def _messages(seed: int, count: int):
    rng = np.random.default_rng(seed)
    for k in range(count):
        letters = "01" if k % 3 == 0 else ("0123456789" if k % 3 == 1 else "0123456789:;<=>?")
        p = rng.dirichlet(np.ones(len(letters)) * (0.3 if k % 2 else 3.0))
        yield "".join(rng.choice(list(letters), size=int(rng.integers(2, 121)), p=p))


class TestBookSnippets(unittest.TestCase):
    def test_book_example_p269(self):
        # p.269: "the entropy rate of '11100001' is 0.96, while the entropy rate for '01100001' is 0.84";
        # '10000111' and '10000110' have the same entropy rate (third caveat). The book truncates to two decimals
        # (Snippet 18.4 gives 0.968 and 0.843).
        self.assertEqual(math.floor(kontoyiannis_entropy("11100001")["h"] * 100), 96)
        self.assertEqual(math.floor(kontoyiannis_entropy("01100001")["h"] * 100), 84)
        self.assertEqual(kontoyiannis_entropy("10000111")["h"], kontoyiannis_entropy("10000110")["h"])

    def test_match_length_equals_snippet_18_3(self):
        cases = 0
        for msg in _messages(0, 400):
            for i in range(1, len(msg)):
                for n in {1, i, max(1, i // 2), min(i, 7)}:
                    if n <= i:
                        self.assertEqual(match_length(msg, i, n), book_matchLength(msg, i, n), (msg, i, n))
                        cases += 1
        self.assertGreater(cases, 10_000)

    def test_match_length_is_one_plus_the_longest_match(self):
        # Gao, Kontoyiannis and Bienenstock (2008) p.6: L = 1 + the longest match length.
        self.assertEqual(match_length("0101", 2, 2), (3, "01"))
        self.assertEqual(match_length("0011", 2, 2), (1, ""))  # no match: L = 1 + 0
        self.assertEqual(match_length("0001", 2, 2), (2, "0"))

    def test_match_length_rejects_i_below_n(self):
        with self.assertRaises(EntropyFeatureError):
            match_length("010101", 2, 3)

    def test_kontoyiannis_equals_snippet_18_4(self):
        for msg in _messages(1, 300):
            got, want = kontoyiannis_entropy(msg), book_konto(msg)
            self.assertEqual(got["h"], want["h"])
            self.assertEqual(got["subS"], want["subS"])
            for window in (1, 3, 10):
                if len(msg) >= 2:
                    self.assertEqual(kontoyiannis_entropy(msg, window)["h"], book_konto(msg, window)["h"])

    def test_pmf1_counts_every_word_registered_deviation(self):
        # D067: Snippet 18.1 loops range(w, len(msg)) and drops the last word; the port counts all len-w+1.
        msg = "0110100110"
        for w in (1, 2, 3):
            ours, book = pmf1(msg, w), book_pmf1(msg, w)
            self.assertAlmostEqual(sum(ours.values()), 1.0, places=12)
            self.assertAlmostEqual(sum(book.values()), 1.0, places=12)
            last = msg[-w:]
            counts_ours = {k: v * (len(msg) - w + 1) for k, v in ours.items()}
            counts_book = {k: v * (len(msg) - w) for k, v in book.items()}
            for k in counts_ours:
                self.assertAlmostEqual(counts_ours[k] - counts_book.get(k, 0.0), 1.0 if k == last else 0.0, places=9)

    def test_plug_in_zero_for_one_letter(self):
        self.assertAlmostEqual(plug_in("0000000000", 1)[0], 0.0, places=12)

    def test_lempel_ziv(self):
        self.assertEqual(lempel_ziv_lib("aabab")[0], "a")
        rng = np.random.default_rng(0)
        self.assertLess(lempel_ziv_entropy_rate("10" * 200), lempel_ziv_entropy_rate("".join(rng.choice(["0", "1"], size=400))))
        with self.assertRaises(EntropyFeatureError):
            lempel_ziv_entropy_rate("")


class TestEncodings(unittest.TestCase):
    def test_binary_drops_zeros(self):
        self.assertEqual(binary_sign_encode(pd.Series([0.5, -0.2, 0.0, 0.1, -0.9])), "1010")

    def test_quantile_half_open_bins(self):
        b = np.array([-1.0, 0.0, 1.0])
        self.assertEqual(quantile_encode(np.array([-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 3.0]), b), "0112233")

    def test_quantile_in_sample_uniform(self):
        r = np.random.default_rng(3).standard_t(3, 100_000)
        msg = quantile_encode(r, np.quantile(r, np.arange(1, 10) / 10))
        shares = np.bincount([int(x) for x in msg], minlength=10) / len(r)
        self.assertTrue(np.allclose(shares, 0.1, atol=1e-4))

    def test_sigma_steps_from_min(self):
        # p.270: 0 for [min, min + s), 1 for [min + s, min + 2s), ...; ceil((max - min)/s) codes.
        self.assertEqual(sigma_encode(np.array([0.0, 0.9, 1.0, 2.5]), 1.0), "0012")
        self.assertEqual(sigma_encode(np.array([0.0, 1.0, 2.0]), 1.0), "011")  # max on the last edge -> last code
        self.assertEqual(sigma_encode(np.array([5.0, 5.0]), 1.0), "00")
        self.assertEqual(sigma_encode(np.array([0.0, 12.0]), 1.0), "0" + chr(48 + 11))


class TestFeatureRule(unittest.TestCase):
    def _closes(self, seed: int, n: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        return 100.0 * np.exp(np.cumsum(rng.normal(0, 1e-3, n)))

    def test_row_matches_independent_computation(self):
        c = self._closes(4, ENTROPY_WINDOW + 1)
        r = np.diff(np.log(c))
        row = entropy_row(c, ENCODING)
        b = "".join("1" if x > 0 else "0" for x in r if x != 0)
        b = b[len(b) % 2:]
        q = "".join(str(int(np.searchsorted(ENCODING["quantile_boundaries"], x, side="right"))) for x in r)
        s = sigma_encode(r, ENCODING["sigma"])
        self.assertEqual(row["Entropy_Kontoyiannis_Binary_100"], book_konto(b)["h"])
        self.assertEqual(row["Entropy_Kontoyiannis_Quantile10_100"], book_konto(q)["h"])
        self.assertEqual(row["Entropy_Kontoyiannis_Sigma_100"], book_konto(s)["h"])

    def test_odd_binary_message_loses_first_letter(self):
        c = self._closes(5, ENTROPY_WINDOW + 1)
        c[1] = c[0]  # one zero return -> 99 letters
        r = np.diff(np.log(c))
        b = "".join("1" if x > 0 else "0" for x in r if x != 0)
        self.assertEqual(len(b), 99)
        self.assertEqual(entropy_row(c, ENCODING)["Entropy_Kontoyiannis_Binary_100"], book_konto(b[1:])["h"])

    def test_lag_zero_uses_the_current_close(self):
        c = self._closes(6, ENTROPY_WINDOW + 1)
        d = c.copy()
        d[-1] = d[-2] * (1.0 - 5e-3)
        self.assertNotEqual(entropy_row(c, ENCODING)["Entropy_Kontoyiannis_Sigma_100"],
                            entropy_row(d, ENCODING)["Entropy_Kontoyiannis_Sigma_100"])

    def test_invalid_windows_are_nan(self):
        c = self._closes(7, ENTROPY_WINDOW + 1)
        for bad in (c[:-1], np.r_[c[:-1], np.nan], np.r_[c[:-1], 0.0], np.full(ENTROPY_WINDOW + 1, 50.0)):
            row = entropy_row(bad, ENCODING)
            if len(bad) == ENTROPY_WINDOW + 1 and np.all(bad == 50.0):
                self.assertTrue(math.isnan(row["Entropy_Kontoyiannis_Binary_100"]))  # no non-zero return
                self.assertFalse(math.isnan(row["Entropy_Kontoyiannis_Quantile10_100"]))
            else:
                self.assertTrue(all(math.isnan(v) for v in row.values()))

    def test_research_series_equals_row_rule(self):
        c = pd.Series(self._closes(8, 260))
        frame = entropy_features(c, ENCODING)
        self.assertEqual(list(frame.columns), list(ENTROPY_COLUMNS))
        self.assertTrue(frame.iloc[:ENTROPY_WINDOW].isna().all().all())
        for t in (ENTROPY_WINDOW, 150, 259):
            row = entropy_row(c.to_numpy()[t - ENTROPY_WINDOW:t + 1], ENCODING)
            for name in ENTROPY_COLUMNS:
                self.assertEqual(frame[name].iat[t], row[name])


class TestEncodingManifest(unittest.TestCase):
    def test_every_asset_manifest_loads(self):
        for asset in ("BTCUSD", "DAX", "GBPJPY", "HK", "JPN", "NASDAQ", "US500", "USDMXN", "USOIL", "XAUUSD"):
            self.assertTrue(encoding_manifest_path(asset).exists(), asset)
            enc = load_entropy_encoding(asset)
            self.assertEqual(len(enc["quantile_boundaries"]), 9)
            self.assertGreater(enc["sigma"], 0)
            self.assertTrue(np.allclose(enc["quantile_in_sample_letter_shares"], 0.1, atol=1e-3))

    def test_validation_fails_closed(self):
        with self.assertRaises(EntropyFeatureError):
            validate_encoding({**ENCODING, "sigma": 0.0}, "TEST")
        with self.assertRaises(EntropyFeatureError):
            validate_encoding({**ENCODING, "quantile_boundaries": ENCODING["quantile_boundaries"][::-1]}, "TEST")
        with self.assertRaises(EntropyFeatureError):
            validate_encoding(ENCODING, "JPN")


if __name__ == "__main__":
    unittest.main()
