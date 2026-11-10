from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for candidate in (V2_ROOT, REPO_ROOT):
    text = str(candidate)
    if text not in sys.path:
        sys.path.insert(0, text)

from shared.broker_risk import derive_implied_account_currency_per_price_unit  # noqa: E402

SCHEMA_VERSION = "v2.3"
DEFAULT_CONTRACT_SPEC_PATH = V2_ROOT / "portfolio_allocator" / "broker_contract_specs.json"
DEFAULT_LOCAL_SETTINGS_PATH = V2_ROOT / "portfolio_live" / "local_live_settings.json"
DEFAULT_OUTPUT_PATH = (
    V2_ROOT
    / "portfolio_allocator"
    / "config"
    / "broker_profiles"
    / "mt5_broker_profile.json"
)

DEFAULT_ASSETS = [
    "XAUUSD",
    "NASDAQ",
    "BTCUSD",
    "DAX",
    "GBPJPY",
    "JPN",
    "USOIL",
    "HK",
    "US500",
    "USDMXN",
]

SYMBOL_INFO_FIELDS = [
    "name",
    "path",
    "description",
    "currency_base",
    "currency_profit",
    "currency_margin",
    "digits",
    "point",
    "trade_calc_mode",
    "trade_mode",
    "trade_contract_size",
    "trade_tick_size",
    "trade_tick_value",
    "trade_tick_value_profit",
    "trade_tick_value_loss",
    "trade_stops_level",
    "trade_freeze_level",
    "filling_mode",
    "order_mode",
    "volume_min",
    "volume_step",
    "volume_max",
    "volume_limit",
    "swap_mode",
    "margin_initial",
    "margin_maintenance",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path, *, required: bool = False) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"Expected a JSON object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def safe_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed


def first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def parse_assets(values: list[str] | None) -> list[str]:
    if not values:
        return list(DEFAULT_ASSETS)
    out: list[str] = []
    for value in values:
        for part in str(value).split(","):
            asset = part.strip().upper()
            if asset and asset not in out:
                out.append(asset)
    return out


def contract_mapping(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    for key in ("contracts", "assets", "symbols", "broker_contract_specs"):
        value = payload.get(key)
        if isinstance(value, dict):
            return {
                str(asset).strip().upper(): dict(spec)
                for asset, spec in value.items()
                if isinstance(spec, dict)
            }
    if payload and all(isinstance(value, dict) for value in payload.values()):
        return {
            str(asset).strip().upper(): dict(spec)
            for asset, spec in payload.items()
            if isinstance(spec, dict)
        }
    return {}


def local_symbol_map(local_settings: dict[str, Any]) -> dict[str, str]:
    raw = local_settings.get("mt5_symbol_map") or local_settings.get("MT5_SYMBOL_MAP") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(asset).strip().upper(): str(symbol).strip()
        for asset, symbol in raw.items()
        if str(asset).strip() and str(symbol).strip()
    }


def resolve_symbol(
    asset: str,
    *,
    specs: dict[str, dict[str, Any]],
    symbol_map: dict[str, str],
) -> str:
    if asset in symbol_map:
        return symbol_map[asset]
    spec = specs.get(asset, {})
    symbol = str(spec.get("broker_symbol") or asset).strip()
    return symbol or asset


def conversion_hint(
    *,
    asset: str,
    profit_currency: str,
    account_currency: str,
) -> dict[str, Any]:
    profit = str(profit_currency or "").strip().upper()
    account = str(account_currency or "").strip().upper()
    if profit and account and profit == account:
        return {
            "mode": "identity",
            "symbol": "",
            "requires_historical_conversion": False,
            "reason": "profit_currency_equals_account_currency",
        }
    if account == "USD" and profit == "EUR":
        return {
            "mode": "direct_symbol",
            "symbol": "EURUSD",
            "requires_historical_conversion": True,
            "reason": "one_EUR_times_EURUSD_equals_USD",
        }
    if account == "USD" and profit == "JPY":
        return {
            "mode": "inverse_symbol",
            "symbol": "USDJPY",
            "requires_historical_conversion": True,
            "reason": "one_JPY_divided_by_USDJPY_equals_USD",
        }
    if account == "USD" and profit == "HKD":
        return {
            "mode": "inverse_symbol",
            "symbol": "USDHKD",
            "requires_historical_conversion": True,
            "reason": "one_HKD_divided_by_USDHKD_equals_USD",
        }
    if account == "USD" and profit == "MXN" and asset == "USDMXN":
        return {
            "mode": "instrument_inverse",
            "symbol": "USDMXN",
            "requires_historical_conversion": True,
            "reason": "one_MXN_divided_by_USDMXN_entry_price_equals_USD",
        }
    return {
        "mode": "manual_review",
        "symbol": "",
        "requires_historical_conversion": profit != account,
        "reason": "conversion_path_not_safely_inferred",
    }


def mt5_initialize_kwargs(args: argparse.Namespace, local: dict[str, Any]) -> dict[str, Any]:
    login = first_present(
        args.login,
        os.getenv("MT5_LOGIN"),
        local.get("mt5_login"),
        local.get("MT5_LOGIN"),
    )
    password = first_present(
        args.password,
        os.getenv("MT5_PASSWORD"),
        local.get("mt5_password"),
        local.get("MT5_PASSWORD"),
    )
    server = first_present(
        args.server,
        os.getenv("MT5_SERVER"),
        local.get("mt5_server"),
        local.get("MT5_SERVER"),
    )
    terminal_path = first_present(
        args.terminal_path,
        os.getenv("MT5_PATH"),
        local.get("mt5_path"),
        local.get("MT5_PATH"),
    )
    timeout = first_present(
        args.timeout_ms,
        os.getenv("MT5_TIMEOUT_MS"),
        local.get("mt5_timeout_ms"),
        local.get("MT5_TIMEOUT_MS"),
        60000,
    )

    kwargs: dict[str, Any] = {"timeout": int(timeout)}
    if terminal_path:
        kwargs["path"] = str(terminal_path)
    if login not in (None, ""):
        kwargs["login"] = int(login)
    if password not in (None, ""):
        kwargs["password"] = str(password)
    if server not in (None, ""):
        kwargs["server"] = str(server)
    return kwargs


def capture_symbol_info(info: Any) -> dict[str, Any]:
    return {
        field: safe_value(getattr(info, field, None))
        for field in SYMBOL_INFO_FIELDS
    }


def choose_probe_distance(info: Any, reference_price: float) -> float:
    point = abs(safe_float(getattr(info, "point", None), 0.0) or 0.0)
    tick_size = abs(safe_float(getattr(info, "trade_tick_size", None), 0.0) or 0.0)
    relative = abs(reference_price) * 0.0005
    candidates = [value for value in (point * 100.0, tick_size * 20.0, relative) if value > 0]
    return max(candidates) if candidates else max(abs(reference_price) * 0.001, 1e-8)


def order_calc_profit_probe(mt5: Any, symbol: str, info: Any, tick: Any) -> dict[str, Any]:
    bid = safe_float(getattr(tick, "bid", None))
    ask = safe_float(getattr(tick, "ask", None))
    contract_size = safe_float(getattr(info, "trade_contract_size", None))
    if not bid or not ask or bid <= 0 or ask <= 0:
        return {"status": "missing_positive_bid_or_ask"}

    midpoint = (bid + ask) / 2.0
    distance = choose_probe_distance(info, midpoint)
    buy_open = ask
    buy_close = max(buy_open - distance, max(midpoint * 1e-9, 1e-12))
    sell_open = bid
    sell_close = sell_open + distance

    buy_profit = mt5.order_calc_profit(
        mt5.ORDER_TYPE_BUY,
        symbol,
        1.0,
        buy_open,
        buy_close,
    )
    sell_profit = mt5.order_calc_profit(
        mt5.ORDER_TYPE_SELL,
        symbol,
        1.0,
        sell_open,
        sell_close,
    )

    result: dict[str, Any] = {
        "status": "ok",
        "lots": 1.0,
        "probe_distance_price": distance,
        "buy": {
            "open_price": buy_open,
            "close_price": buy_close,
            "order_calc_profit": safe_float(buy_profit),
        },
        "sell": {
            "open_price": sell_open,
            "close_price": sell_close,
            "order_calc_profit": safe_float(sell_profit),
        },
    }

    implied_values: list[float] = []
    for side in ("buy", "sell"):
        profit = safe_float(result[side]["order_calc_profit"])
        if profit is None or profit == 0:
            result[side]["implied_account_currency_per_price_unit_per_lot"] = None
            continue
        implied = derive_implied_account_currency_per_price_unit(
            absolute_profit=abs(profit),
            price_distance=distance,
            lots=1.0,
        )
        result[side]["implied_account_currency_per_price_unit_per_lot"] = implied
        implied_values.append(implied)

    if implied_values:
        mean_implied = sum(implied_values) / len(implied_values)
        result["mean_implied_account_currency_per_price_unit_per_lot"] = mean_implied
        if contract_size and contract_size > 0:
            result["mean_implied_profit_to_account_conversion_rate"] = (
                mean_implied / contract_size
            )
    else:
        result["status"] = "order_calc_profit_unavailable"
    return result


def capture_profile(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise RuntimeError(
            "MetaTrader5 is not installed in this Python environment. "
            "Install it with: pip install MetaTrader5"
        ) from exc

    spec_path = Path(args.contract_specs).expanduser().resolve()
    local_path = Path(args.local_settings).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    specs = contract_mapping(load_json(spec_path, required=True))
    local = load_json(local_path, required=False)
    symbols = local_symbol_map(local)
    assets = parse_assets(args.asset)
    init_kwargs = mt5_initialize_kwargs(args, local)

    initialized = False
    try:
        initialized = bool(mt5.initialize(**init_kwargs))
        if not initialized:
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

        account = mt5.account_info()
        terminal = mt5.terminal_info()
        version = mt5.version()
        if account is None:
            raise RuntimeError(f"MT5 account_info unavailable: {mt5.last_error()}")

        account_currency = str(getattr(account, "currency", "") or "").strip().upper()
        captured: dict[str, Any] = {}
        unavailable: dict[str, Any] = {}
        missing: list[str] = []

        for asset in assets:
            symbol = resolve_symbol(asset, specs=specs, symbol_map=symbols)
            selected = bool(mt5.symbol_select(symbol, True))
            info = mt5.symbol_info(symbol)
            if not selected or info is None:
                missing.append(asset)
                unavailable[asset] = {
                    "asset": asset,
                    "broker_symbol": symbol,
                    "status": "symbol_unavailable",
                    "symbol_selected": selected,
                    "mt5_error": safe_value(mt5.last_error()),
                }
                continue

            tick = mt5.symbol_info_tick(symbol)
            fields = capture_symbol_info(info)
            profit_currency = str(fields.get("currency_profit") or "").strip().upper()
            captured[asset] = {
                "asset": asset,
                "broker_symbol": symbol,
                "status": "captured",
                "symbol_selected": selected,
                "account_currency": account_currency,
                "profit_currency": profit_currency,
                "contract": {
                    "min_lot": fields.get("volume_min"),
                    "lot_step": fields.get("volume_step"),
                    "max_lot": fields.get("volume_max"),
                    "contract_size": fields.get("trade_contract_size"),
                    "trade_calc_mode": fields.get("trade_calc_mode"),
                    "tick_size": fields.get("trade_tick_size"),
                    "tick_value": fields.get("trade_tick_value"),
                    "tick_value_profit": fields.get("trade_tick_value_profit"),
                    "tick_value_loss": fields.get("trade_tick_value_loss"),
                },
                "conversion_hint": conversion_hint(
                    asset=asset,
                    profit_currency=profit_currency,
                    account_currency=account_currency,
                ),
                "current_tick": (
                    {
                        "time": safe_value(getattr(tick, "time", None)),
                        "time_msc": safe_value(getattr(tick, "time_msc", None)),
                        "bid": safe_value(getattr(tick, "bid", None)),
                        "ask": safe_value(getattr(tick, "ask", None)),
                        "last": safe_value(getattr(tick, "last", None)),
                    }
                    if tick is not None
                    else None
                ),
                "order_calc_profit_probe": (
                    order_calc_profit_probe(mt5, symbol, info, tick)
                    if tick is not None
                    else {"status": "missing_tick"}
                ),
                "symbol_info": fields,
            }

        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "created_at": now_utc(),
            "artifact_policy": "hashless_overwriteable_snapshot",
            "purpose": (
                "Read-only MT5 broker contract capture. This file does not arm trading "
                "and is not consumed by the allocator until the integration step."
            ),
            "source": "MetaTrader5 Python API",
            "account": {
                "currency": account_currency,
                "server": safe_value(getattr(account, "server", None)),
                "company": safe_value(getattr(account, "company", None)),
                "trade_mode": safe_value(getattr(account, "trade_mode", None)),
                "margin_mode": safe_value(getattr(account, "margin_mode", None)),
            },
            "terminal": {
                "name": safe_value(getattr(terminal, "name", None)) if terminal else None,
                "company": safe_value(getattr(terminal, "company", None)) if terminal else None,
                "build": safe_value(version[1]) if version and len(version) > 1 else None,
                "connected": safe_value(getattr(terminal, "connected", None)) if terminal else None,
                "trade_allowed": safe_value(getattr(terminal, "trade_allowed", None)) if terminal else None,
            },
            "inputs": {
                "asset_count": len(assets),
                "assets": assets,
                "contract_specs_path": str(spec_path),
                "local_settings_loaded": local_path.exists(),
                "credentials_written_to_output": False,
            },
            "summary": {
                "requested_asset_count": len(assets),
                "captured_asset_count": len(captured),
                "available_assets": sorted(captured),
                "missing_asset_count": len(missing),
                "missing_assets": sorted(missing),
                "availability_status": "FULL" if not missing else "PARTIAL",
                "status": "PASS" if captured else "FAIL",
            },
            # Only captured/usable contracts are exposed to the broker-risk loader.
            # Missing symbols are valid portfolio exclusions, not malformed contracts.
            "contracts": captured,
            "unavailable_contracts": unavailable,
            "next_step": (
                "Run 00b_build_historical_conversion_artifacts.py. Portfolio Step 01 "
                "will automatically exclude edges whose assets are listed in missing_assets."
            ),
        }
        write_json(output_path, payload)
        return payload
    finally:
        if initialized:
            mt5.shutdown()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture authoritative broker/account contract metadata from a local MT5 "
            "terminal. This script is read-only and never sends orders."
        )
    )
    parser.add_argument(
        "--asset",
        action="append",
        help="Asset key to capture. Repeat or use comma-separated values. Defaults to all 10 assets.",
    )
    parser.add_argument("--login", type=int, help="MT5 login. Prefer local settings or MT5_LOGIN.")
    parser.add_argument("--password", help="MT5 password. Prefer local settings or MT5_PASSWORD.")
    parser.add_argument("--server", help="MT5 server. Prefer local settings or MT5_SERVER.")
    parser.add_argument("--terminal-path", help="Path to terminal64.exe. Prefer local settings or MT5_PATH.")
    parser.add_argument("--timeout-ms", type=int, default=None)
    parser.add_argument(
        "--contract-specs",
        default=str(DEFAULT_CONTRACT_SPEC_PATH),
        help="Existing allocator contract file used only for asset-to-symbol seed mapping.",
    )
    parser.add_argument(
        "--local-settings",
        default=str(DEFAULT_LOCAL_SETTINGS_PATH),
        help="Optional local_live_settings.json containing credentials and mt5_symbol_map.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Hashless JSON snapshot path. Existing file is overwritten.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero unless all requested assets are captured.",
    )
    parser.add_argument("--print-json", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = capture_profile(args)
    summary = payload["summary"]
    print("V2 Portfolio Allocator 00A — MT5 broker profile capture")
    print(f"Status: {summary['status']}")
    print(
        f"Captured: {summary['captured_asset_count']}/{summary['requested_asset_count']}"
    )
    print(f"Account currency: {payload['account']['currency']}")
    print(f"Output: {Path(args.output).expanduser().resolve()}")
    if summary["missing_assets"]:
        print("Missing assets: " + ", ".join(summary["missing_assets"]))
    if args.print_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    if args.strict and summary.get("availability_status") != "FULL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
