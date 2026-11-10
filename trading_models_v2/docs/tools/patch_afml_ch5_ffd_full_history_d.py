"""AFML Ch.5 walkthrough patch (owner decision 2026-10-23): estimate FFD d on the full research history.

Changes shared/ffd/fractional_differentiation.py, build_asset_ffd_features.py, verify_asset_ffd_manifest.py and
shared/research/holdout_replay.py:
  * d = minimum d with ADF p < 0.05 (FracdiffStat, statsmodels default ADF) on FFD(log Close) of the FULL research
    history (AFML s.5.6 / Snippet 5.4), fixed 1,000-term window (D032); frozen afterwards
  * manifest records the ADF result (full sample + each half); an estimated d whose FFD fails ADF fails the build
  * --frozen-d CLI flag; the holdout workspace passes the repo's frozen d and never re-estimates it (no holdout leak)
  * verifier rejects manifests built under the retired first-20,000-bars rule
Idempotent: skips a file that already carries the change. Fails without writing if an anchor is not unique.

usage (from trading_models_v2):  python ./docs/tools/patch_afml_ch5_ffd_full_history_d.py
"""
from __future__ import annotations

import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
MARK = "AFML Ch.5 walkthrough"


def _apply(rel: str, pairs: list[tuple[str, str]]) -> str:
    path = V2 / rel
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        return f"already patched  {rel}"
    for old, new in pairs:
        n = text.count(old)
        if n != 1:
            raise SystemExit(f"FAILED {rel}: anchor found {n} times: {old[:70]!r}")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return f"patched          {rel}"


RUNTIME = [
    ("""V2 contract
-----------
This module preserves that idea, but makes it reusable across assets/edges.
""", """V2 contract
-----------
This module preserves that idea, but makes it reusable across assets/edges.

d selection (AFML Ch.5 walkthrough, owner decision 2026-10-23)
-------------------------------------------------------------
AFML s.5.6 / Snippet 5.4 (pp.84-85): d* is the minimum d whose FFD(log price) passes the ADF test at 95%,
measured on the whole series. Quantara: FracdiffStat (binary search on [0, 1], precision 0.01, ADF =
statsmodels defaults with AIC lag selection) on log Close of the FULL research history (every bar in the
research store; the sealed holdout and live never enter), fixed 1,000-term window (deviation D032). d is then
frozen: holdout replays and live receive it (frozen_d) and never re-estimate it. The retired rule (first
20,000 bars only) gave d too small for 8 of 10 assets, whose FFD series then failed ADF on the full history.
"""),
    ("DEFAULT_D_ESTIMATION_BARS = 20_000",
     """DEFAULT_D_ESTIMATION_BARS: int | None = None  # None = full research history (AFML Snippet 5.4)
D_POLICY_ESTIMATED = "full_research_history_then_freeze"
D_POLICY_SUPPLIED = "frozen_d_supplied"
ADF_PVALUE = 0.05"""),
    ("    d_estimation_bars: int = DEFAULT_D_ESTIMATION_BARS",
     "    d_estimation_bars: int | None = DEFAULT_D_ESTIMATION_BARS"),
    ("""        if self.d_estimation_bars <= 0:
            raise FFDContractError("d_estimation_bars must be > 0")""",
     """        if self.d_estimation_bars is not None and self.d_estimation_bars <= 0:
            raise FFDContractError("d_estimation_bars must be > 0 (or None for the full research history)")"""),
    ('''def estimate_optimal_d(close_series: pd.Series, *, window: int, estimation_bars: int) -> float:
    """Estimate d from the initial training segment only, then freeze it."""

    if len(close_series) < max(window + 10, estimation_bars):
        raise FFDContractError(
            f"Not enough rows to estimate d safely. Need at least {max(window + 10, estimation_bars)} rows, "
            f"got {len(close_series)}."
        )

    train_segment = close_series.iloc[:estimation_bars].copy()''',
     '''def estimate_optimal_d(close_series: pd.Series, *, window: int, estimation_bars: int | None = None) -> float:
    """Minimum d whose FFD(log Close) passes ADF (p < 0.05): full research history when estimation_bars is None,
    else the first estimation_bars rows. The caller freezes it."""

    need = window + 10 if estimation_bars is None else max(window + 10, estimation_bars)
    if len(close_series) < need:
        raise FFDContractError(f"Not enough rows to estimate d safely. Need at least {need} rows, got {len(close_series)}.")

    train_segment = close_series.copy() if estimation_bars is None else close_series.iloc[:estimation_bars].copy()'''),
    ("def build_live_bundle(",
     '''def stationarity_report(ffd_bars: pd.DataFrame) -> dict[str, Any]:
    """ADF (statsmodels defaults, the test FracdiffStat uses) on Close_FFD: full sample and each half (diagnostic)."""
    from statsmodels.tsa.stattools import adfuller

    x = ffd_bars["Close_FFD"].to_numpy(dtype="float64")
    half = len(x) // 2

    def _adf(values: np.ndarray) -> dict[str, float]:
        stat, pvalue, lags, nobs, *_ = adfuller(values)
        return {"adf_stat": float(stat), "p_value": float(pvalue), "lags": int(lags), "nobs": int(nobs)}

    full = _adf(x)
    return {
        "test": "statsmodels.adfuller defaults (regression='c', autolag='AIC')",
        "p_value_threshold": ADF_PVALUE,
        "full": full,
        "first_half": _adf(x[:half]),
        "second_half": _adf(x[half:]),
        "full_passed": bool(full["p_value"] < ADF_PVALUE),
    }


def build_live_bundle('''),
    ("""    retention = memory_retention_report(ffd_bars)
    min_corr = min(retention.values())
""", """    retention = memory_retention_report(ffd_bars)
    min_corr = min(retention.values())
    stationarity = stationarity_report(ffd_bars)
    # An estimated d must give a stationary feature on the data it was chosen on (AFML s.5.6). A supplied d (holdout
    # workspace, reproduction) is reported, not gated: its sample includes data the d was not chosen on.
    if frozen_d is None and not stationarity["full_passed"]:
        raise FFDContractError(
            f"Close_FFD at estimated d={d:.6f} fails ADF on the estimation sample (p={stationarity['full']['p_value']:.4f})."
        )
"""),
    ('''        "d_estimation_policy": "initial_training_segment_only_then_freeze",
        "d_estimation_bars": int(cfg.d_estimation_bars),''',
     '''        "d_estimation_policy": D_POLICY_SUPPLIED if frozen_d is not None else D_POLICY_ESTIMATED,
        "d_estimation_bars": None if cfg.d_estimation_bars is None else int(cfg.d_estimation_bars),
        "d_estimation_rows": None if frozen_d is not None else int(len(df) if cfg.d_estimation_bars is None else cfg.d_estimation_bars),
        "d_selection_rule": "min d on [0,1] (precision 0.01) with ADF p < 0.05 on FFD(log Close), fixed window (AFML Snippet 5.4)",
        "d_selection_scope": "full research history; sealed holdout and live never used; frozen after selection",
        "stationarity": stationarity,'''),
    ('''            "d estimated from the initial training segment only, then frozen.",''',
     '''            "d estimated on the full research history (AFML Snippet 5.4), then frozen; holdout/live receive it.",'''),
]

BUILDER = [
    ('''"""
Build asset-level V2 FFD features from verified standard_quote_tick bars.
''', '''"""
Build asset-level V2 FFD features from verified standard_quote_tick bars.

AFML Ch.5 walkthrough (2026-10-23): d is estimated on the full research history unless --frozen-d is given.
'''),
    ("    d_estimation_bars: int = DEFAULT_D_ESTIMATION_BARS,",
     "    d_estimation_bars: int | None = DEFAULT_D_ESTIMATION_BARS,\n    frozen_d: float | None = None,"),
    ("""        config=config,
    )

    manifest.update(""", """        config=config,
        frozen_d=frozen_d,
    )

    manifest.update("""),
    ('    parser.add_argument("--d-estimation-bars", type=int, default=DEFAULT_D_ESTIMATION_BARS)',
     '''    parser.add_argument("--d-estimation-bars", type=int, default=DEFAULT_D_ESTIMATION_BARS,
                        help="estimate d on the first N bars only (default: full research history, AFML Snippet 5.4)")
    parser.add_argument("--frozen-d", type=float, default=None,
                        help="use this d instead of estimating it (holdout workspace / reproduction)")'''),
    ("""        live_buffer_margin=args.live_buffer_margin,
        overwrite=args.overwrite,""", """        live_buffer_margin=args.live_buffer_margin,
        frozen_d=args.frozen_d,
        overwrite=args.overwrite,"""),
    ("""        "min_memory_retention_corr": float(manifest["min_memory_retention_corr"]),
        "ffd_features":""", """        "min_memory_retention_corr": float(manifest["min_memory_retention_corr"]),
        "adf_p_full": float(manifest["stationarity"]["full"]["p_value"]),
        "adf_p_first_half": float(manifest["stationarity"]["first_half"]["p_value"]),
        "adf_p_second_half": float(manifest["stationarity"]["second_half"]["p_value"]),
        "ffd_features":"""),
    ("""    print(f"Memory retention passed: {result['memory_retention_passed']}")""",
     """    print(f"Memory retention passed: {result['memory_retention_passed']}")
    print(f"ADF p full / first half / second half: {result['adf_p_full']:.4f} / "
          f"{result['adf_p_first_half']:.4f} / {result['adf_p_second_half']:.4f}")"""),
]

VERIFIER = [
    ('''    _require_equal(manifest, "d_estimation_policy", "initial_training_segment_only_then_freeze")''',
     '''    # AFML Ch.5 walkthrough 2026-10-23: d is estimated on the full research history, or supplied frozen (holdout
    # workspace). Manifests from the retired first-20,000-bars rule fail here and must be rebuilt.
    if manifest.get("d_estimation_policy") not in ("full_research_history_then_freeze", "frozen_d_supplied"):
        raise FFDManifestVerificationError(
            f"d_estimation_policy={manifest.get('d_estimation_policy')!r} is retired; rebuild FFD "
            "(python ./shared/ffd/build_asset_ffd_features.py --asset <ASSET> --overwrite)."
        )
    if manifest.get("d_estimation_policy") == "full_research_history_then_freeze" and (
        manifest.get("stationarity", {}).get("full_passed") is not True
    ):
        raise FFDManifestVerificationError("Close_FFD failed the ADF stationarity check at the estimated d.")'''),
]

HOLDOUT = [
    ('''         "--d-estimation-bars", str(_first(ffd_m, "d_estimation_bars")),''',
     '''         # AFML Ch.5 walkthrough: the workspace holds holdout bars, so it takes the repo's frozen d, never re-estimates
         "--frozen-d", repr(float(_first(ffd_m, "frozen_d"))),'''),
]


def main() -> int:
    for rel, pairs in (("shared/ffd/fractional_differentiation.py", RUNTIME),
                       ("shared/ffd/build_asset_ffd_features.py", BUILDER),
                       ("shared/ffd/verify_asset_ffd_manifest.py", VERIFIER),
                       ("shared/research/holdout_replay.py", HOLDOUT)):
        print(_apply(rel, pairs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
