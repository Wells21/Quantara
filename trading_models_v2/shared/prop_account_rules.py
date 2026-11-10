from __future__ import annotations

"""Generic, hashless prop-account rule loader used by allocator and live execution.

The active account is intentionally represented by one overwriteable JSON file:
``portfolio_allocator/config/active_account_rules.json``.  Scripts never branch on
prop-firm names.  Changing phase or firm means editing that file and rerunning the
normal allocator/live handoff.
"""

import copy
import json
import math
from pathlib import Path
from typing import Any

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
DEFAULT_RULES_PATH = (
    V2_ROOT / "portfolio_allocator" / "config" / "active_account_rules.json"
)

SUPPORTED_OVERALL_DRAWDOWN_TYPES = {
    "static",
    "trailing_fixed_initial_amount",
    "trailing_pct_high_watermark",
}
SUPPORTED_OVERALL_BREACH_BASES = {
    "equity",
    "balance",
    "lower_of_balance_or_equity",
}
SUPPORTED_DAILY_MODES = {
    "fixed_initial_amount_from_day_start_balance",
    "pct_of_day_start_balance",
    "pct_of_higher_day_start_balance_or_equity",
}
SUPPORTED_DAILY_BREACH_BASES = SUPPORTED_OVERALL_BREACH_BASES
SUPPORTED_VALID_DAY_MODES = {"minimum_profit", "any_trade"}
SUPPORTED_FLOATING_GUARD_ACTIONS = {"block_new_trades", "force_close_all"}


class AccountRulesError(ValueError):
    pass


def _as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AccountRulesError(f"{label} must be a JSON object")
    return dict(value)


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise AccountRulesError(f"Expected boolean value, got {value!r}")


def _float(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    allow_none: bool = False,
) -> float | None:
    if value is None and allow_none:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise AccountRulesError(f"{label} must be numeric, got {value!r}") from exc
    if not math.isfinite(out):
        raise AccountRulesError(f"{label} must be finite, got {value!r}")
    if minimum is not None and out < minimum:
        raise AccountRulesError(f"{label} must be >= {minimum}, got {out}")
    if maximum is not None and out > maximum:
        raise AccountRulesError(f"{label} must be <= {maximum}, got {out}")
    return out


def _int(value: Any, label: str, *, minimum: int = 0) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise AccountRulesError(f"{label} must be an integer, got {value!r}") from exc
    if out < minimum:
        raise AccountRulesError(f"{label} must be >= {minimum}, got {out}")
    return out


def normalize_account_rules(payload: dict[str, Any]) -> dict[str, Any]:
    raw = copy.deepcopy(_as_dict(payload, "active_account_rules"))

    profile_label = str(raw.get("profile_label") or "active_account").strip()
    if not profile_label:
        raise AccountRulesError("profile_label cannot be empty")
    account_stage = str(raw.get("account_stage") or "challenge").strip().lower()
    if not account_stage:
        raise AccountRulesError("account_stage cannot be empty")

    initial_balance = float(_float(raw.get("initial_balance"), "initial_balance", minimum=0.01))
    current_balance = float(
        _float(raw.get("current_balance", initial_balance), "current_balance", minimum=0.0)
    )
    current_high_watermark = float(
        _float(
            raw.get("current_high_watermark", max(initial_balance, current_balance)),
            "current_high_watermark",
            minimum=0.0,
        )
    )
    current_high_watermark = max(initial_balance, current_balance, current_high_watermark)

    profit = _as_dict(raw.get("profit_target", {}), "profit_target")
    profit_enabled = _bool(profit.get("enabled", True), True)
    profit_pct = _float(
        profit.get("pct"),
        "profit_target.pct",
        minimum=0.0,
        maximum=10.0,
        allow_none=not profit_enabled,
    )
    if not profit_enabled:
        profit_pct = None
    stop_immediately_on_target = _bool(
        profit.get("stop_immediately_when_eligible", True), True
    )

    overall = _as_dict(raw.get("overall_drawdown", {}), "overall_drawdown")
    overall_enabled = _bool(overall.get("enabled", True), True)
    overall_type = str(overall.get("type") or "static").strip().lower()
    if overall_type not in SUPPORTED_OVERALL_DRAWDOWN_TYPES:
        raise AccountRulesError(
            f"overall_drawdown.type must be one of {sorted(SUPPORTED_OVERALL_DRAWDOWN_TYPES)}, got {overall_type!r}"
        )
    overall_pct = _float(
        overall.get("pct"),
        "overall_drawdown.pct",
        minimum=0.0,
        maximum=1.0,
        allow_none=not overall_enabled,
    )
    if not overall_enabled:
        overall_pct = None
    overall_breach_basis = str(
        overall.get("breach_basis") or "lower_of_balance_or_equity"
    ).strip().lower()
    if overall_breach_basis not in SUPPORTED_OVERALL_BREACH_BASES:
        raise AccountRulesError(
            "overall_drawdown.breach_basis must be one of "
            f"{sorted(SUPPORTED_OVERALL_BREACH_BASES)}"
        )
    high_watermark_basis = str(
        overall.get("high_watermark_basis") or "balance"
    ).strip().lower()
    if high_watermark_basis not in {"balance", "equity", "higher_of_balance_or_equity"}:
        raise AccountRulesError(
            "overall_drawdown.high_watermark_basis must be balance, equity, or higher_of_balance_or_equity"
        )
    trailing_stops_at_initial_balance = _bool(
        overall.get("trailing_stops_at_initial_balance", False), False
    )

    daily = _as_dict(raw.get("daily_drawdown", {}), "daily_drawdown")
    daily_enabled = _bool(daily.get("enabled", True), True)
    daily_pct = _float(
        daily.get("pct"),
        "daily_drawdown.pct",
        minimum=0.0,
        maximum=1.0,
        allow_none=not daily_enabled,
    )
    if not daily_enabled:
        daily_pct = None
    daily_mode = str(
        daily.get("mode") or "fixed_initial_amount_from_day_start_balance"
    ).strip().lower()
    if daily_mode not in SUPPORTED_DAILY_MODES:
        raise AccountRulesError(
            f"daily_drawdown.mode must be one of {sorted(SUPPORTED_DAILY_MODES)}, got {daily_mode!r}"
        )
    daily_breach_basis = str(
        daily.get("breach_basis") or "lower_of_balance_or_equity"
    ).strip().lower()
    if daily_breach_basis not in SUPPORTED_DAILY_BREACH_BASES:
        raise AccountRulesError(
            f"daily_drawdown.breach_basis must be one of {sorted(SUPPORTED_DAILY_BREACH_BASES)}"
        )
    reset_hour_utc = _int(daily.get("reset_hour_utc", 0), "daily_drawdown.reset_hour_utc")
    if reset_hour_utc > 23:
        raise AccountRulesError("daily_drawdown.reset_hour_utc must be between 0 and 23")
    reset_timezone = str(daily.get("reset_timezone") or "UTC").strip() or "UTC"

    min_days = _as_dict(raw.get("minimum_trading_days", {}), "minimum_trading_days")
    min_days_enabled = _bool(min_days.get("enabled", False), False)
    required_days = _int(
        min_days.get("required_days", 0),
        "minimum_trading_days.required_days",
        minimum=0,
    )
    valid_day_mode = str(min_days.get("day_count_mode") or "minimum_profit").strip().lower()
    if valid_day_mode not in SUPPORTED_VALID_DAY_MODES:
        raise AccountRulesError(
            f"minimum_trading_days.day_count_mode must be one of {sorted(SUPPORTED_VALID_DAY_MODES)}"
        )
    min_day_profit_pct = _float(
        min_days.get("minimum_day_profit_pct", 0.0),
        "minimum_trading_days.minimum_day_profit_pct",
        minimum=0.0,
        maximum=1.0,
    )
    if not min_days_enabled:
        required_days = 0

    max_trade = _as_dict(raw.get("maximum_risk_per_trade", {}), "maximum_risk_per_trade")
    max_trade_enabled = _bool(max_trade.get("enabled", False), False)
    max_trade_pct = _float(
        max_trade.get("pct"),
        "maximum_risk_per_trade.pct",
        minimum=0.0,
        maximum=1.0,
        allow_none=not max_trade_enabled,
    )
    if not max_trade_enabled:
        max_trade_pct = None

    symbol = _as_dict(raw.get("daily_symbol_risk", {}), "daily_symbol_risk")
    symbol_enabled = _bool(symbol.get("enabled", False), False)
    symbol_max_pct = _float(
        symbol.get("max_pct"),
        "daily_symbol_risk.max_pct",
        minimum=0.0,
        maximum=1.0,
        allow_none=not symbol_enabled,
    )
    if not symbol_enabled:
        symbol_max_pct = None
    symbol_internal_pct = _float(
        symbol.get("internal_operating_pct", symbol_max_pct),
        "daily_symbol_risk.internal_operating_pct",
        minimum=0.0,
        maximum=1.0,
        allow_none=not symbol_enabled,
    )
    if not symbol_enabled:
        symbol_internal_pct = None
    if (
        symbol_enabled
        and symbol_max_pct is not None
        and symbol_internal_pct is not None
        and symbol_internal_pct > symbol_max_pct + 1e-12
    ):
        raise AccountRulesError(
            "daily_symbol_risk.internal_operating_pct cannot exceed max_pct"
        )

    floating = _as_dict(raw.get("floating_loss_guard", {}), "floating_loss_guard")
    floating_enabled = _bool(floating.get("enabled", False), False)
    floating_pct = _float(
        floating.get("pct"),
        "floating_loss_guard.pct",
        minimum=0.0,
        maximum=1.0,
        allow_none=not floating_enabled,
    )
    if not floating_enabled:
        floating_pct = None
    floating_internal_pct = _float(
        floating.get("internal_operating_pct", floating_pct),
        "floating_loss_guard.internal_operating_pct",
        minimum=0.0,
        maximum=1.0,
        allow_none=not floating_enabled,
    )
    if not floating_enabled:
        floating_internal_pct = None
    if (
        floating_enabled
        and floating_pct is not None
        and floating_internal_pct is not None
        and floating_internal_pct > floating_pct + 1e-12
    ):
        raise AccountRulesError(
            "floating_loss_guard.internal_operating_pct cannot exceed pct"
        )
    floating_action = str(
        floating.get("action") or "block_new_trades"
    ).strip().lower()
    if floating_action not in SUPPORTED_FLOATING_GUARD_ACTIONS:
        raise AccountRulesError(
            "floating_loss_guard.action must be one of "
            f"{sorted(SUPPORTED_FLOATING_GUARD_ACTIONS)}"
        )

    permissions = _as_dict(raw.get("operational_permissions", {}), "operational_permissions")

    normalized: dict[str, Any] = {
        "schema_version": str(raw.get("schema_version") or "v1"),
        "artifact_policy": "hashless_structural_account_rules",
        "profile_label": profile_label,
        "description": str(raw.get("description") or "").strip(),
        "account_stage": account_stage,
        "initial_balance": initial_balance,
        "current_balance": current_balance,
        "current_high_watermark": current_high_watermark,
        "assume_fresh_daily_reset_at_simulation_start": _bool(
            raw.get("assume_fresh_daily_reset_at_simulation_start", True), True
        ),
        "profit_target": {
            "enabled": profit_enabled,
            "pct": profit_pct,
            "target_balance": (
                initial_balance * (1.0 + float(profit_pct))
                if profit_enabled and profit_pct is not None
                else None
            ),
            "stop_immediately_when_eligible": stop_immediately_on_target,
        },
        "overall_drawdown": {
            "enabled": overall_enabled,
            "type": overall_type,
            "pct": overall_pct,
            "amount": initial_balance * float(overall_pct or 0.0),
            "breach_basis": overall_breach_basis,
            "high_watermark_basis": high_watermark_basis,
            "trailing_stops_at_initial_balance": trailing_stops_at_initial_balance,
        },
        "daily_drawdown": {
            "enabled": daily_enabled,
            "pct": daily_pct,
            "mode": daily_mode,
            "breach_basis": daily_breach_basis,
            "reset_timezone": reset_timezone,
            "reset_hour_utc": reset_hour_utc,
            # Existing simulations use floor((timestamp + offset)/24h).  A reset
            # at H:00 UTC therefore requires an offset of (24-H) mod 24 hours.
            "bucket_offset_hours": float((24 - reset_hour_utc) % 24),
        },
        "minimum_trading_days": {
            "enabled": min_days_enabled,
            "required_days": required_days,
            "day_count_mode": valid_day_mode,
            "minimum_day_profit_pct": float(min_day_profit_pct or 0.0),
            "minimum_day_profit_amount": initial_balance * float(min_day_profit_pct or 0.0),
        },
        "maximum_risk_per_trade": {
            "enabled": max_trade_enabled,
            "pct": max_trade_pct,
            "amount": initial_balance * float(max_trade_pct or 0.0) if max_trade_enabled else None,
        },
        "daily_symbol_risk": {
            "enabled": symbol_enabled,
            "max_pct": symbol_max_pct,
            "max_amount": initial_balance * float(symbol_max_pct or 0.0) if symbol_enabled else None,
            "internal_operating_pct": symbol_internal_pct,
            "internal_operating_amount": (
                initial_balance * float(symbol_internal_pct or 0.0)
                if symbol_enabled
                else None
            ),
            "enforcement_mode": "enforced" if symbol_enabled else "disabled",
        },
        "floating_loss_guard": {
            "enabled": floating_enabled,
            "pct": floating_pct,
            "amount": initial_balance * float(floating_pct or 0.0) if floating_enabled else None,
            "internal_operating_pct": floating_internal_pct,
            "internal_operating_amount": (
                initial_balance * float(floating_internal_pct or 0.0)
                if floating_enabled
                else None
            ),
            "action": floating_action,
        },
        "operational_permissions": {
            "news_trading_allowed": _bool(
                permissions.get("news_trading_allowed", True), True
            ),
            "weekend_holding_allowed": _bool(
                permissions.get("weekend_holding_allowed", True), True
            ),
            "vps_allowed": _bool(permissions.get("vps_allowed", True), True),
            "automation_allowed": _bool(
                permissions.get("automation_allowed", True), True
            ),
        },
    }

    if normalized["profit_target"]["enabled"] and normalized["profit_target"]["target_balance"] is not None:
        if current_balance >= float(normalized["profit_target"]["target_balance"]):
            # Valid: a partially completed account may already be above target but
            # still need minimum valid days.  Do not reject it here.
            pass

    floor = overall_drawdown_floor(normalized, current_high_watermark)
    if overall_enabled and overall_breach_value(
        normalized,
        balance=current_balance,
        equity=current_balance,
    ) < floor - 1e-9:
        raise AccountRulesError(
            f"current_balance={current_balance} is below the configured overall floor={floor}"
        )

    return normalized


def load_active_account_rules(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path).expanduser().resolve() if path else DEFAULT_RULES_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"Missing active account rules JSON: {target}. "
            "Create/edit portfolio_allocator/config/active_account_rules.json."
        )
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AccountRulesError(f"Expected JSON object: {target}")
    normalized = normalize_account_rules(payload)
    normalized["source_path"] = target.resolve().relative_to(V2_ROOT.resolve()).as_posix()
    return normalized


def overall_drawdown_floor(rules: dict[str, Any], high_watermark: float) -> float:
    cfg = rules["overall_drawdown"]
    initial = float(rules["initial_balance"])
    if not cfg.get("enabled", True):
        return float("-inf")
    dd_type = str(cfg["type"])
    pct = float(cfg.get("pct") or 0.0)
    amount = float(cfg.get("amount") or initial * pct)
    high = max(initial, float(high_watermark))
    if dd_type == "static":
        return initial - amount
    if dd_type == "trailing_fixed_initial_amount":
        floor = high - amount
    elif dd_type == "trailing_pct_high_watermark":
        floor = high * (1.0 - pct)
    else:  # normalized validation should make this unreachable
        raise AccountRulesError(f"Unsupported overall drawdown type: {dd_type}")
    if cfg.get("trailing_stops_at_initial_balance"):
        floor = min(initial, floor)
    return float(floor)


def overall_breach_value(
    rules: dict[str, Any], *, balance: float, equity: float
) -> float:
    basis = rules["overall_drawdown"]["breach_basis"]
    if basis == "balance":
        return float(balance)
    if basis == "equity":
        return float(equity)
    return float(min(balance, equity))


def overall_high_watermark_value(
    rules: dict[str, Any], *, balance: float, equity: float
) -> float:
    basis = rules["overall_drawdown"]["high_watermark_basis"]
    if basis == "balance":
        return float(balance)
    if basis == "equity":
        return float(equity)
    return float(max(balance, equity))


def drawdown_stats(
    rules: dict[str, Any],
    *,
    balance: float,
    open_risk_total: float = 0.0,
    high_watermark: float | None = None,
) -> dict[str, Any]:
    equity_proxy = float(balance) - max(0.0, float(open_risk_total))
    observed = overall_high_watermark_value(
        rules, balance=float(balance), equity=equity_proxy
    )
    high = max(
        float(rules["initial_balance"]),
        observed,
        float(high_watermark) if high_watermark is not None else observed,
    )
    floor = overall_drawdown_floor(rules, high)
    breach_value = overall_breach_value(
        rules, balance=float(balance), equity=equity_proxy
    )
    enabled = bool(rules["overall_drawdown"].get("enabled", True))
    return {
        "drawdown_type": rules["overall_drawdown"]["type"],
        "drawdown_floor": floor,
        "initial_drawdown_floor": overall_drawdown_floor(
            rules, float(rules["initial_balance"])
        ),
        "trailing_high_watermark": high,
        "trailing_floor_locked_at_initial_balance": bool(
            rules["overall_drawdown"].get("trailing_stops_at_initial_balance")
            and floor >= float(rules["initial_balance"]) - 1e-12
        ),
        "equity_proxy": equity_proxy,
        "breach_value": breach_value,
        "remaining_dd_buffer": (
            breach_value - floor if enabled else float("inf")
        ),
        "floor_breached": bool(enabled and breach_value < floor - 1e-12),
        "challenge_dd_used": max(0.0, high - breach_value),
    }


def daily_drawdown_floor(
    rules: dict[str, Any], *, day_start_balance: float, day_start_equity: float
) -> float:
    cfg = rules["daily_drawdown"]
    if not cfg.get("enabled", True):
        return float("-inf")
    pct = float(cfg.get("pct") or 0.0)
    mode = str(cfg["mode"])
    if mode == "fixed_initial_amount_from_day_start_balance":
        reference = float(day_start_balance)
        loss_amount = float(rules["initial_balance"]) * pct
    elif mode == "pct_of_day_start_balance":
        reference = float(day_start_balance)
        loss_amount = reference * pct
    elif mode == "pct_of_higher_day_start_balance_or_equity":
        reference = max(float(day_start_balance), float(day_start_equity))
        loss_amount = reference * pct
    else:
        raise AccountRulesError(f"Unsupported daily drawdown mode: {mode}")
    return float(reference - loss_amount)


def daily_breach_value(
    rules: dict[str, Any], *, balance: float, equity: float
) -> float:
    basis = rules["daily_drawdown"]["breach_basis"]
    if basis == "balance":
        return float(balance)
    if basis == "equity":
        return float(equity)
    return float(min(balance, equity))


def daily_drawdown_amount(
    rules: dict[str, Any], *, day_start_balance: float, day_start_equity: float
) -> float:
    cfg = rules["daily_drawdown"]
    if not cfg.get("enabled", True):
        return 0.0
    mode = str(cfg["mode"])
    pct = float(cfg.get("pct") or 0.0)
    if mode == "fixed_initial_amount_from_day_start_balance":
        return float(rules["initial_balance"]) * pct
    if mode == "pct_of_day_start_balance":
        return float(day_start_balance) * pct
    return max(float(day_start_balance), float(day_start_equity)) * pct


def minimum_days_satisfied(rules: dict[str, Any], qualifying_day_count: int) -> bool:
    cfg = rules["minimum_trading_days"]
    return (not cfg.get("enabled", False)) or int(qualifying_day_count) >= int(
        cfg.get("required_days", 0)
    )


def target_satisfied(
    rules: dict[str, Any], *, balance: float, qualifying_day_count: int = 0
) -> bool:
    target = rules["profit_target"]
    if not target.get("enabled", True):
        return False
    target_balance = target.get("target_balance")
    return (
        target_balance is not None
        and float(balance) >= float(target_balance) - 1e-12
        and minimum_days_satisfied(rules, qualifying_day_count)
    )


def daily_drawdown_config(rules: dict[str, Any]) -> dict[str, Any]:
    """Return the legacy-compatible scenario row consumed by Steps 08-11."""
    daily = rules["daily_drawdown"]
    symbol = rules["daily_symbol_risk"]
    initial = float(rules["initial_balance"])
    return {
        "name": str(rules["profile_label"]),
        "account_stage": str(rules["account_stage"]),
        "enabled": bool(daily["enabled"]),
        "pct": float(daily.get("pct") or 0.0),
        "amount": initial * float(daily.get("pct") or 0.0),
        "mode": str(daily["mode"]),
        "breach_basis": str(daily["breach_basis"]),
        "max_overall_loss_pct": float(rules["overall_drawdown"].get("pct") or 0.0),
        "trailing_drawdown": str(rules["overall_drawdown"]["type"]).startswith("trailing"),
        "trailing_stops_at_initial_balance": bool(
            rules["overall_drawdown"].get("trailing_stops_at_initial_balance")
        ),
        "daily_symbol_risk_rule_enabled": bool(symbol["enabled"]),
        "max_daily_risk_per_symbol_pct": symbol.get("max_pct"),
        "max_daily_risk_per_symbol_amount": symbol.get("max_amount"),
        "internal_operating_risk_per_symbol_pct": symbol.get("internal_operating_pct"),
        "internal_operating_risk_per_symbol_amount": symbol.get("internal_operating_amount"),
        "symbol_risk_safety_reserve_amount": (
            float(symbol["max_amount"]) - float(symbol["internal_operating_amount"])
            if symbol.get("enabled")
            and symbol.get("max_amount") is not None
            and symbol.get("internal_operating_amount") is not None
            else None
        ),
        "symbol_risk_mode": str(symbol["enforcement_mode"]),
        "daily_symbol_risk_enforcement_mode": str(symbol["enforcement_mode"]),
        "symbol_risk_admission_policy": (
            "enforced_from_active_account_rules" if symbol["enabled"] else "disabled"
        ),
        "account_rule_profile": str(rules["profile_label"]),
        "account_rule_profile_description": str(rules.get("description") or ""),
    }


def active_rule_contract(rules: dict[str, Any]) -> dict[str, Any]:
    """Compact JSON-safe contract copied through Steps 08-11 and live manifest."""
    keys = [
        "schema_version",
        "artifact_policy",
        "profile_label",
        "description",
        "account_stage",
        "initial_balance",
        "current_balance",
        "current_high_watermark",
        "assume_fresh_daily_reset_at_simulation_start",
        "profit_target",
        "overall_drawdown",
        "daily_drawdown",
        "minimum_trading_days",
        "maximum_risk_per_trade",
        "daily_symbol_risk",
        "floating_loss_guard",
        "operational_permissions",
        "source_path",
    ]
    return copy.deepcopy({key: rules.get(key) for key in keys})


def structurally_equal(left: Any, right: Any) -> bool:
    """Hashless deterministic equality for JSON-compatible contracts."""
    return json.dumps(left, sort_keys=True, separators=(",", ":"), default=str) == json.dumps(
        right, sort_keys=True, separators=(",", ":"), default=str
    )


def _print_active_rules_summary() -> None:
    rules = load_active_account_rules()
    print("Active prop-account rules: PASS")
    print(f"Profile label: {rules['profile_label']}")
    print(f"Account stage: {rules['account_stage']}")
    print(f"Initial balance: {rules['initial_balance']:.2f}")
    target = rules['profit_target']
    print(
        "Profit target: "
        + (f"{float(target['pct']) * 100:.4f}% -> {float(target['target_balance']):.2f}" if target['enabled'] else "disabled")
    )
    overall = rules['overall_drawdown']
    print(
        f"Overall drawdown: enabled={overall['enabled']} type={overall['type']} "
        f"pct={float(overall.get('pct') or 0.0) * 100:.4f}% "
        f"initial_floor={overall_drawdown_floor(rules, rules['initial_balance']):.2f}"
    )
    daily = rules['daily_drawdown']
    print(
        f"Daily drawdown: enabled={daily['enabled']} mode={daily['mode']} "
        f"pct={float(daily.get('pct') or 0.0) * 100:.4f}% "
        f"reset_hour_utc={daily['reset_hour_utc']}"
    )
    minimum = rules['minimum_trading_days']
    print(
        f"Minimum trading days: enabled={minimum['enabled']} "
        f"required={minimum['required_days']} mode={minimum['day_count_mode']}"
    )
    print(f"Source: {rules['source_path']}")


if __name__ == "__main__":
    _print_active_rules_summary()
