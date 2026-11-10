"""
V2 bar contract validator.

Blueprint purpose
-----------------
V2 production bars must use the standard quote-tick contract:

- Research_Bar_Mode == standard_quote_tick
- Threshold_Unit == ticks
- Proxy_Value == Ticks for backward compatibility only
- all required OHLC/bar metadata columns exist
- executable rows must have valid OHLC and positive tick counts

This validator checks an already-built bar table. It does not build bars yet.
The actual shared quote-tick bar builder will be added after this contract passes.

Run from ``trading_models_v2``:

    python ./shared/bars/bar_contract_validator.py

or from the repository root:

    python trading_models_v2/shared/bars/bar_contract_validator.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# Allow this file to be run directly from either:
#   1. the repository root, or
#   2. inside trading_models_v2
# without requiring PYTHONPATH changes.
_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.contracts import (  # noqa: E402
    AuditFinding,
    AuditReport,
    AuditSeverity,
    BAR_MODE_STANDARD_QUOTE_TICK,
    REQUIRED_BAR_COLUMNS,
    THRESHOLD_UNIT_TICKS,
)


OHLC_COLUMNS: tuple[str, ...] = ("Open", "High", "Low", "Close")
BOOLEAN_COLUMNS: tuple[str, ...] = ("Executable", "Gap_Healed")


@dataclass(frozen=True)
class BarValidationConfig:
    """Validation config for a standard_quote_tick bar table."""

    require_proxy_value_equals_ticks: bool = True
    require_all_rows_executable_or_gap_flagged: bool = False
    allow_empty: bool = False


class BarContractValidationError(RuntimeError):
    """Raised when bar contract validation must fail closed."""


def _is_bool_like(series: pd.Series) -> bool:
    if pd.api.types.is_bool_dtype(series):
        return True
    allowed = {True, False, 0, 1, "true", "false", "True", "False"}
    return set(series.dropna().unique()).issubset(allowed)


def validate_bar_dataframe(
    bars: pd.DataFrame,
    config: BarValidationConfig | None = None,
) -> AuditReport:
    """Validate a DataFrame against the V2 standard_quote_tick bar contract."""

    cfg = config or BarValidationConfig()
    findings: list[AuditFinding] = []

    if bars.empty:
        severity = AuditSeverity.WARN if cfg.allow_empty else AuditSeverity.FAIL
        findings.append(
            AuditFinding(
                severity,
                "BAR_TABLE_EMPTY",
                "Bar table has no rows. This is WARN only for pre-live/no-rows-yet contexts.",
            )
        )
        return AuditReport(name="bar_dataframe_contract", findings=tuple(findings))

    missing_columns = [col for col in REQUIRED_BAR_COLUMNS if col not in bars.columns]
    if missing_columns:
        findings.append(
            AuditFinding(
                AuditSeverity.FAIL,
                "BAR_REQUIRED_COLUMNS_MISSING",
                f"Missing required V2 bar columns: {missing_columns}",
            )
        )
        return AuditReport(name="bar_dataframe_contract", findings=tuple(findings))

    # Datetime fields must be parseable. UTC enforcement will be tightened in the builder stage.
    for col in ("Datetime", "Bar_Start"):
        parsed = pd.to_datetime(bars[col], errors="coerce", utc=True, format="ISO8601")
        if parsed.isna().any():
            bad_count = int(parsed.isna().sum())
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    f"{col.upper()}_NOT_PARSEABLE",
                    f"{bad_count} row(s) have non-parseable {col} values.",
                )
            )

    # Close timestamp must not precede start timestamp.
    dt = pd.to_datetime(bars["Datetime"], errors="coerce", utc=True, format="ISO8601")
    start = pd.to_datetime(bars["Bar_Start"], errors="coerce", utc=True, format="ISO8601")
    invalid_order = (dt < start).fillna(False)
    if invalid_order.any():
        findings.append(
            AuditFinding(
                AuditSeverity.FAIL,
                "BAR_DATETIME_BEFORE_START",
                f"{int(invalid_order.sum())} row(s) have Datetime earlier than Bar_Start.",
            )
        )

    # Required mode/unit values.
    invalid_mode = bars["Research_Bar_Mode"].astype(str) != BAR_MODE_STANDARD_QUOTE_TICK
    if invalid_mode.any():
        findings.append(
            AuditFinding(
                AuditSeverity.FAIL,
                "BAR_MODE_INVALID",
                f"{int(invalid_mode.sum())} row(s) do not use {BAR_MODE_STANDARD_QUOTE_TICK!r}.",
            )
        )

    invalid_unit = bars["Threshold_Unit"].astype(str) != THRESHOLD_UNIT_TICKS
    if invalid_unit.any():
        findings.append(
            AuditFinding(
                AuditSeverity.FAIL,
                "BAR_THRESHOLD_UNIT_INVALID",
                f"{int(invalid_unit.sum())} row(s) do not use threshold_unit={THRESHOLD_UNIT_TICKS!r}.",
            )
        )

    # Numeric columns.
    numeric_columns = (*OHLC_COLUMNS, "Proxy_Value", "Ticks", "Threshold")
    numeric: dict[str, pd.Series] = {}
    for col in numeric_columns:
        numeric[col] = pd.to_numeric(bars[col], errors="coerce")
        if numeric[col].isna().any():
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    f"{col.upper()}_NOT_NUMERIC",
                    f"{int(numeric[col].isna().sum())} row(s) have non-numeric {col} values.",
                )
            )

    if not any(f.severity == AuditSeverity.FAIL and f.code.endswith("NOT_NUMERIC") for f in findings):
        if (numeric["Ticks"] <= 0).any():
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "BAR_TICKS_NOT_POSITIVE",
                    f"{int((numeric['Ticks'] <= 0).sum())} row(s) have Ticks <= 0.",
                )
            )

        if (numeric["Threshold"] <= 0).any():
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "BAR_THRESHOLD_NOT_POSITIVE",
                    f"{int((numeric['Threshold'] <= 0).sum())} row(s) have Threshold <= 0.",
                )
            )

        if cfg.require_proxy_value_equals_ticks:
            mismatch = numeric["Proxy_Value"] != numeric["Ticks"]
            if mismatch.any():
                findings.append(
                    AuditFinding(
                        AuditSeverity.FAIL,
                        "PROXY_VALUE_NOT_TICK_COUNT",
                        f"{int(mismatch.sum())} row(s) have Proxy_Value != Ticks.",
                    )
                )

        ohlc = pd.DataFrame({col: numeric[col] for col in OHLC_COLUMNS})
        high_too_low = ohlc["High"] < ohlc[["Open", "Low", "Close"]].max(axis=1)
        low_too_high = ohlc["Low"] > ohlc[["Open", "High", "Close"]].min(axis=1)
        if high_too_low.any():
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "BAR_HIGH_INVALID",
                    f"{int(high_too_low.sum())} row(s) have High below Open/Low/Close.",
                )
            )
        if low_too_high.any():
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "BAR_LOW_INVALID",
                    f"{int(low_too_high.sum())} row(s) have Low above Open/High/Close.",
                )
            )

    for col in BOOLEAN_COLUMNS:
        if not _is_bool_like(bars[col]):
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    f"{col.upper()}_NOT_BOOLEAN_LIKE",
                    f"Column {col} must be boolean-like.",
                )
            )

    if cfg.require_all_rows_executable_or_gap_flagged:
        executable = bars["Executable"].astype(bool)
        gap_healed = bars["Gap_Healed"].astype(bool)
        invalid_live_rows = (~executable) & (~gap_healed)
        if invalid_live_rows.any():
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "BAR_NOT_EXECUTABLE_OR_GAP_FLAGGED",
                    f"{int(invalid_live_rows.sum())} row(s) are neither executable nor gap-healed.",
                )
            )

    if not any(f.severity == AuditSeverity.FAIL for f in findings):
        findings.append(
            AuditFinding(
                AuditSeverity.PASS,
                "BAR_DATAFRAME_CONTRACT_OK",
                "Bar DataFrame satisfies the V2 standard_quote_tick contract.",
            )
        )

    return AuditReport(name="bar_dataframe_contract", findings=tuple(findings))


def require_valid_bar_dataframe(
    bars: pd.DataFrame,
    config: BarValidationConfig | None = None,
) -> None:
    """Fail closed if the bar DataFrame violates the V2 bar contract."""

    report = validate_bar_dataframe(bars, config=config)
    if report.failed:
        details = "\n".join(f"- {msg}" for msg in report.fail_messages())
        raise BarContractValidationError(f"V2 bar contract validation failed:\n{details}")


def load_bar_table(path: str | Path) -> pd.DataFrame:
    """Load a CSV or Parquet bar table.

    This is kept small for now; research/live loaders can wrap it later.
    """

    table_path = Path(path)
    if not table_path.exists():
        raise BarContractValidationError(f"Bar table does not exist: {table_path}")
    if table_path.suffix.lower() == ".csv":
        return pd.read_csv(table_path)
    if table_path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(table_path)
    raise BarContractValidationError(f"Unsupported bar table format: {table_path.suffix}")


def validate_bar_table_file(path: str | Path, config: BarValidationConfig | None = None) -> AuditReport:
    """Load and validate a bar table file."""

    bars = load_bar_table(path)
    return validate_bar_dataframe(bars, config=config)


def _make_valid_example_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Datetime": ["2026-05-15T10:00:05Z", "2026-05-15T10:00:10Z"],
            "Bar_Start": ["2026-05-15T10:00:00Z", "2026-05-15T10:00:05Z"],
            "Open": [100.0, 100.5],
            "High": [101.0, 101.2],
            "Low": [99.8, 100.4],
            "Close": [100.5, 101.0],
            "Proxy_Value": [50, 50],
            "Ticks": [50, 50],
            "Threshold": [50, 50],
            "Threshold_Unit": ["ticks", "ticks"],
            "Research_Bar_Mode": ["standard_quote_tick", "standard_quote_tick"],
            "Executable": [True, True],
            "Gap_Healed": [False, False],
            "Source": ["self_test", "self_test"],
        }
    )


def _run_self_test() -> None:
    valid_bars = _make_valid_example_bars()
    report = validate_bar_dataframe(valid_bars)
    if report.failed:
        raise SystemExit("❌ Valid example bars failed:\n" + "\n".join(report.fail_messages()))

    invalid_bars = valid_bars.copy()
    invalid_bars.loc[0, "Proxy_Value"] = 49
    invalid_report = validate_bar_dataframe(invalid_bars)
    if invalid_report.passed:
        raise SystemExit("❌ Invalid Proxy_Value mismatch was not detected.")

    print("✅ shared.bars.bar_contract_validator self-test passed.")
    print(f"Rows checked: {len(valid_bars)}")
    print("Detected expected invalid case: Proxy_Value != Ticks")


if __name__ == "__main__":
    _run_self_test()
