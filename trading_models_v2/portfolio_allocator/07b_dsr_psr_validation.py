"""
V2 Phase B2.07B -- Probabilistic / Deflated Sharpe Ratio validation, with every trial counted.

Book (AFML = Advances in Financial Machine Learning, 2018; MLAM = Machine Learning for Asset Managers, 2020):
  AFML s.14.7.2 (p.203): PSR[SR*] = Z[(SR - SR*) sqrt(T - 1) / sqrt(1 - g3 SR + (g4 - 1)/4 SR^2)], g4 = 3 for Gaussian
  returns (Pearson kurtosis).
  AFML s.14.7.3 (pp.204-205): DSR = PSR[SR*], SR* = sqrt(V[{SR_n}]) ((1 - gamma) Z^-1[1 - 1/N] + gamma Z^-1[1 - 1/(N e)]);
  third law (Snippet 14.5, p.205): every backtest result must be reported with ALL the trials involved in its production.
  AFML p.206: PSR and DSR "should exceed 0.95".
  MLAM s.8.7 (pp.113-116) revises N and V (owner rule: MLAM over AFML where it revises): E[K] = ONC clusters of the trials'
  return correlation matrix, denoised and detoned when a market component exists (MLAM Ch.2 p.24, p.87; D076, owner
  2026-11-02; shared/backtest_statistics/afml_ch14_dsr.py trial_clustering_corr); V from one aggregate series per cluster, annualised, rescaled to the selected strategy's
  frequency. Cluster weights: the robust minimum-variance approximation of Lopez de Prado & Lewis (2018, SSRN 3167017)
  Appendix A.4 Snippet 4 (getIVP with extended terms), the method MLAM p.114 points to (owner decision 2026-10-26).
  MLAM s.8.8 (pp.116-123): familywise error rate (Sidak) and Type II error under multiple testing.

AFML Ch.14 walkthrough rewrite (owner-approved 2026-10-26, docs/mldp/AFML_MLAM_WALKTHROUGH.md s.4N). The old version used
the deployed edges' own return streams as the "trials" (ONC -> K = 2 across survivors), which ignores the search that
produced each edge. Now:
  * per edge: the trials are the edge's full Gate C run (every configuration replayed), estimated at the Gate C stage by
    shared/research/dsr_all_trials.py and stored in artifacts/feature_importance_audit/ch14_dsr_all_trials.json
    (E[K], V of annualised cluster SRs). 07b applies that E[K] and V to the edge's allocator return stream. Fails closed
    when an edge has no record (run dsr_all_trials first).
  * portfolio: the trials are every HRP scenario return series built by Step 07 (linkage x return mode x universe);
    ONC + robust cluster weights on those series give E[K] and V for the selected scenario.
  * every series is put on a calendar-day grid (0 on days without a return), as the Gate C trials are.
Reports PSR[0], DSR, FWER alpha_K and Type II beta_K per target, and the pass marks DSR > 0.95 and alpha_K < 0.05.
Gate enforcement (removing a failing edge) is decided separately by the owner; this script reports.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for p in (V2_ROOT, REPO_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from shared.backtest_statistics.afml_ch14_dsr import (  # noqa: E402
    CLUSTER_WEIGHTING,
    DSR_PASS,
    FWER_PASS,
    cluster_trial_variance,
    dsr_report,
    onc_trial_clusters,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

SCHEMA_VERSION = "v2.0"
RETURN_STREAMS_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "return_streams"
HRP_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "hrp_linkage_robustness_outputs"
OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "dsr_psr_validation_outputs"

INPUT_EDGE_RETURNS = (
    RETURN_STREAMS_DIR / "profile_return_streams_daily_core_plus_watchlist_compound_velocity.csv"
)
# The deployed HRP scenario: core_plus_watchlist / raw_r / single linkage (07 CANONICAL_METHOD; 08 simulates single only).
# Was the 07 robustness top-ranked 'average' scenario, which live does not trade (AFML Ch.16 walkthrough s.4P.2, fixed 2026-10-30).
DEFAULT_PORTFOLIO_RETURNS = (
    HRP_DIR / "hrp_portfolio_returns_raw_r_core_plus_watchlist_compound_velocity_single.csv"
)
PORTFOLIO_TRIAL_GLOB = "hrp_portfolio_returns_*.csv"
EDGE_TRIAL_RECORD = Path("artifacts") / "feature_importance_audit" / "ch14_dsr_all_trials.json"
ONC_SEED = 0


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def display_path(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(V2_ROOT))
    except ValueError:
        return str(path)


def load_daily_frame(path: Path, value_cols: list[str] | None = None) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing return stream: {path}")
    df = pd.read_csv(path, float_precision="round_trip")
    if "date" not in df.columns:
        raise ValueError(f"{path} must contain a date column")
    df["date"] = to_datetime_iso(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    if value_cols is not None:
        df = df[value_cols]
    return df


def calendar_grid(series: pd.Series) -> pd.Series:
    """Calendar-day grid, 0 on days without a return (the Gate C trial convention)."""
    s = series.groupby(series.index.normalize()).sum()
    idx = pd.date_range(s.index.min(), s.index.max(), freq="D", tz="UTC")
    return s.reindex(idx, fill_value=0.0)


def load_edge_trial_record(edge_id: str) -> dict[str, Any]:
    asset, edge = edge_id.split("/", 1)
    path = V2_ROOT / "assets" / asset / "edges" / edge / EDGE_TRIAL_RECORD
    if not path.exists():
        raise FileNotFoundError(
            f"{edge_id}: no all-trials DSR record {path.relative_to(V2_ROOT)}. Run "
            "python -m shared.research.dsr_all_trials --edge-root assets/<ASSET>/edges/<edge> --stamps <Gate C stamps> "
            f"--out {EDGE_TRIAL_RECORD.as_posix()} first (AFML third law: report every trial)."
        )
    rec = json.loads(path.read_text(encoding="utf-8"))
    for key in ("E_K_onc", "V_annualised_sr", "n_trials"):
        if key not in rec:
            raise ValueError(f"{edge_id}: all-trials record {path} lacks {key}; re-run shared/research/dsr_all_trials.py")
    return rec


def portfolio_trials() -> dict[str, Any]:
    """MLAM s.8.7 over every HRP scenario return series of Step 07 (the portfolio-level trials)."""
    files = sorted(HRP_DIR.glob(PORTFOLIO_TRIAL_GLOB))
    series = {}
    for f in files:
        df = load_daily_frame(f)
        col = "hrp_portfolio_r" if "hrp_portfolio_r" in df.columns else df.columns[0]
        series[f.stem.replace("hrp_portfolio_returns_", "")] = calendar_grid(df[col])
    perf = pd.DataFrame(series).fillna(0.0)
    perf = perf.loc[:, perf.std(ddof=1) > 0]
    if perf.shape[1] < 3:
        return {"n_trials": int(perf.shape[1]), "E_K": int(perf.shape[1]), "V_annualised_sr": float("nan"), "clusters": []}
    clusters, corr_diag = onc_trial_clusters(perf, seed=ONC_SEED)   # MLAM p.24/p.87 (D076)
    tv = cluster_trial_variance(perf, clusters)
    return {"n_trials": int(perf.shape[1]), "trial_corr": corr_diag, **tv}


def target_row(name: str, series: pd.Series, e_k: int, v_ann: float, n_trials: int, source: str) -> dict[str, Any]:
    s = calendar_grid(series)
    rep = dsr_report(s.to_numpy(), s.index, e_k, v_ann)
    return {
        "name": name, "trial_source": source, "n_trials": n_trials, "E_K": e_k, "V_annualised_sr": v_ann,
        "T_days": rep["T"], "sr_nonannualised": rep["sr"], "annualised_sr": rep["annualised_sr"],
        "skewness": rep["skew"], "kurtosis": rep["kurt"],
        "sr_star_annualised": rep["sr_star_annualised"], "psr_vs_zero": rep["psr_vs_zero"],
        "deflated_sharpe_ratio": rep["dsr"], "dsr_pass_0p95": rep["dsr_pass_0p95"],
        "fwer_alpha_K": rep["fwer"]["alpha_K"], "fwer_pass_0p05": rep["fwer"]["pass_0p05"],
        "type2_beta_K_at_annualised_sr_1": rep["type2"]["annualised_1p0"]["beta_K"],
        "passes_dsr_and_fwer": bool(rep["dsr_pass_0p95"] and rep["fwer"]["pass_0p05"]),
    }


def build(
    *,
    write: bool = True,
    edge_returns_path: Path = INPUT_EDGE_RETURNS,
    portfolio_returns_path: Path = DEFAULT_PORTFOLIO_RETURNS,
) -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    edge_returns = load_daily_frame(edge_returns_path)
    rows = []
    for edge_id in edge_returns.columns:
        rec = load_edge_trial_record(edge_id)
        rows.append(target_row(edge_id, edge_returns[edge_id], int(rec["E_K_onc"]), float(rec["V_annualised_sr"]),
                               int(rec["n_trials"]), f"Gate C run {','.join(rec.get('stamps', []))}"))
    pt = portfolio_trials()
    port = load_daily_frame(portfolio_returns_path, value_cols=["hrp_portfolio_r"])["hrp_portfolio_r"]
    rows.append(target_row(f"PORTFOLIO({portfolio_returns_path.stem})", port, int(pt["E_K"]), float(pt["V_annualised_sr"]),
                           int(pt["n_trials"]), "Step 07 HRP scenarios"))
    targets_df = pd.DataFrame(rows).sort_values("deflated_sharpe_ratio", ascending=False, na_position="last")
    clusters_df = pd.DataFrame([{k: v for k, v in c.items() if k != "members"} | {"members": ",".join(c.get("members", []))}
                                for c in pt.get("clusters", [])])
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "status": "PASS",
        "input_edge_returns": display_path(edge_returns_path),
        "input_portfolio_returns": display_path(portfolio_returns_path),
        "edge_trial_record": EDGE_TRIAL_RECORD.as_posix(),
        "portfolio_trials": {"n_trials": pt["n_trials"], "E_K": pt["E_K"], "V_annualised_sr": pt["V_annualised_sr"],
                             "trial_corr": pt.get("trial_corr")},
        "cluster_weighting": CLUSTER_WEIGHTING,
        "dsr_pass_mark": DSR_PASS,
        "fwer_pass_mark": FWER_PASS,
        "n_targets_evaluated": int(len(targets_df)),
        "n_targets_pass_dsr_and_fwer": int(targets_df["passes_dsr_and_fwer"].sum()),
        "methodology": "see module docstring (AFML s.14.7, MLAM s.8.7-8.8, Lopez de Prado & Lewis 2018 Snippet 4)",
        "gate_enforcement": "reported only; the owner decides when the DSR gate removes edges (walkthrough s.4N)",
        "outputs": {
            "targets": str(OUTPUT_DIR / "dsr_psr_targets.csv"),
            "clusters": str(OUTPUT_DIR / "dsr_psr_trial_clusters.csv"),
            "manifest": str(OUTPUT_DIR / "dsr_psr_validation_manifest.json"),
        },
    }
    if write:
        targets_df.to_csv(OUTPUT_DIR / "dsr_psr_targets.csv", index=False)
        clusters_df.to_csv(OUTPUT_DIR / "dsr_psr_trial_clusters.csv", index=False)
        write_json(OUTPUT_DIR / "dsr_psr_validation_manifest.json", manifest)
    return manifest | {"_targets": targets_df}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V2 Phase B2.07B DSR/PSR validation (all trials).")
    parser.add_argument("--edge-returns", default=str(INPUT_EDGE_RETURNS))
    parser.add_argument("--portfolio-returns", default=str(DEFAULT_PORTFOLIO_RETURNS))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    manifest = build(
        write=not args.dry_run,
        edge_returns_path=Path(args.edge_returns),
        portfolio_returns_path=Path(args.portfolio_returns),
    )
    t = manifest.pop("_targets")
    print("V2 Phase B2.07B DSR/PSR validation (all trials)")
    print(f"Portfolio trials: {manifest['portfolio_trials']}")
    print(t[["name", "n_trials", "E_K", "psr_vs_zero", "deflated_sharpe_ratio", "fwer_alpha_K", "passes_dsr_and_fwer"]].to_string(index=False))
    print(f"Pass DSR > {DSR_PASS} and FWER < {FWER_PASS}: {manifest['n_targets_pass_dsr_and_fwer']} / {manifest['n_targets_evaluated']}")


if __name__ == "__main__":
    main()
