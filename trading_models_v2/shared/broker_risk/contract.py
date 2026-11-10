from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


SUPPORTED_CONVERSION_MODES = {
    "identity",
    "explicit_rate",
    "direct_symbol",
    "inverse_symbol",
    "instrument_inverse",
}


class BrokerRiskError(ValueError):
    """Raised when broker-risk inputs are incomplete or internally inconsistent."""


def _positive_float(value: Any, *, field: str, allow_zero: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BrokerRiskError(f"{field} must be numeric; received {value!r}") from exc
    if allow_zero:
        if parsed < 0:
            raise BrokerRiskError(f"{field} must be >= 0; received {parsed}")
    elif parsed <= 0:
        raise BrokerRiskError(f"{field} must be > 0; received {parsed}")
    return parsed


def _currency(value: Any) -> str:
    return str(value or "").strip().upper()


@dataclass(frozen=True)
class ConversionContract:
    """Describes how instrument profit currency is converted to account currency.

    This object deliberately contains no file hashes. It is a runtime contract only.
    Historical conversion values are supplied explicitly by the caller for each trade.
    """

    mode: str
    profit_currency: str
    account_currency: str
    symbol: str = ""
    fixed_rate: float | None = None
    timestamp_policy: str = "latest_at_or_before_entry"
    max_staleness_seconds: int | None = None

    def __post_init__(self) -> None:
        mode = str(self.mode).strip().lower()
        if mode not in SUPPORTED_CONVERSION_MODES:
            raise BrokerRiskError(
                f"Unsupported conversion mode {self.mode!r}; "
                f"supported={sorted(SUPPORTED_CONVERSION_MODES)}"
            )
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "profit_currency", _currency(self.profit_currency))
        object.__setattr__(self, "account_currency", _currency(self.account_currency))
        object.__setattr__(self, "symbol", str(self.symbol or "").strip().upper())
        if not self.profit_currency:
            raise BrokerRiskError("profit_currency is required")
        if not self.account_currency:
            raise BrokerRiskError("account_currency is required")
        if mode == "identity" and self.profit_currency != self.account_currency:
            raise BrokerRiskError(
                "identity conversion is valid only when profit_currency equals account_currency"
            )
        if mode == "explicit_rate":
            object.__setattr__(
                self,
                "fixed_rate",
                _positive_float(self.fixed_rate, field="fixed_rate"),
            )
        if mode in {"direct_symbol", "inverse_symbol"} and not self.symbol:
            raise BrokerRiskError(f"symbol is required for conversion mode {mode}")
        if self.max_staleness_seconds is not None and int(self.max_staleness_seconds) < 0:
            raise BrokerRiskError("max_staleness_seconds must be >= 0")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ConversionContract":
        return cls(
            mode=str(payload.get("mode", "")),
            profit_currency=str(payload.get("profit_currency", "")),
            account_currency=str(payload.get("account_currency", "")),
            symbol=str(payload.get("symbol", "")),
            fixed_rate=payload.get("fixed_rate"),
            timestamp_policy=str(
                payload.get("timestamp_policy", "latest_at_or_before_entry")
            ),
            max_staleness_seconds=(
                None
                if payload.get("max_staleness_seconds") is None
                else int(payload["max_staleness_seconds"])
            ),
        )

    def resolve_rate(
        self,
        *,
        conversion_price: float | None = None,
        instrument_price: float | None = None,
    ) -> float:
        """Return account-currency units per one profit-currency unit."""

        if self.mode == "identity":
            return 1.0
        if self.mode == "explicit_rate":
            return _positive_float(self.fixed_rate, field="fixed_rate")
        if self.mode == "direct_symbol":
            return _positive_float(conversion_price, field="conversion_price")
        if self.mode == "inverse_symbol":
            price = _positive_float(conversion_price, field="conversion_price")
            return 1.0 / price
        if self.mode == "instrument_inverse":
            price = _positive_float(instrument_price, field="instrument_price")
            return 1.0 / price
        raise BrokerRiskError(f"Unhandled conversion mode: {self.mode}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "profit_currency": self.profit_currency,
            "account_currency": self.account_currency,
            "symbol": self.symbol,
            "fixed_rate": self.fixed_rate,
            "timestamp_policy": self.timestamp_policy,
            "max_staleness_seconds": self.max_staleness_seconds,
        }


@dataclass(frozen=True)
class BrokerContract:
    asset: str
    broker_symbol: str
    account_currency: str
    profit_currency: str
    min_lot: float
    lot_step: float
    max_lot: float
    contract_size: float
    trade_calc_mode: int | None = None
    tick_size: float | None = None
    tick_value: float | None = None
    tick_value_profit: float | None = None
    tick_value_loss: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset", str(self.asset).strip().upper())
        object.__setattr__(self, "broker_symbol", str(self.broker_symbol).strip())
        object.__setattr__(self, "account_currency", _currency(self.account_currency))
        object.__setattr__(self, "profit_currency", _currency(self.profit_currency))
        object.__setattr__(self, "min_lot", _positive_float(self.min_lot, field="min_lot"))
        object.__setattr__(self, "lot_step", _positive_float(self.lot_step, field="lot_step"))
        object.__setattr__(self, "max_lot", _positive_float(self.max_lot, field="max_lot"))
        object.__setattr__(
            self,
            "contract_size",
            _positive_float(self.contract_size, field="contract_size"),
        )
        if not self.asset:
            raise BrokerRiskError("asset is required")
        if not self.broker_symbol:
            raise BrokerRiskError("broker_symbol is required")
        if not self.account_currency:
            raise BrokerRiskError("account_currency is required")
        if not self.profit_currency:
            raise BrokerRiskError("profit_currency is required")
        if self.max_lot < self.min_lot:
            raise BrokerRiskError("max_lot must be >= min_lot")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BrokerContract":
        return cls(
            asset=str(payload.get("asset", "")),
            broker_symbol=str(payload.get("broker_symbol", "")),
            account_currency=str(payload.get("account_currency", "")),
            profit_currency=str(
                payload.get("profit_currency")
                or payload.get("currency_profit")
                or payload.get("quote_currency")
                or ""
            ),
            min_lot=payload.get("min_lot", payload.get("volume_min")),
            lot_step=payload.get("lot_step", payload.get("volume_step")),
            max_lot=payload.get("max_lot", payload.get("volume_max")),
            contract_size=payload.get(
                "contract_size", payload.get("trade_contract_size")
            ),
            trade_calc_mode=payload.get("trade_calc_mode"),
            tick_size=payload.get("tick_size", payload.get("trade_tick_size")),
            tick_value=payload.get("tick_value", payload.get("trade_tick_value")),
            tick_value_profit=payload.get(
                "tick_value_profit", payload.get("trade_tick_value_profit")
            ),
            tick_value_loss=payload.get(
                "tick_value_loss", payload.get("trade_tick_value_loss")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "broker_symbol": self.broker_symbol,
            "account_currency": self.account_currency,
            "profit_currency": self.profit_currency,
            "min_lot": self.min_lot,
            "lot_step": self.lot_step,
            "max_lot": self.max_lot,
            "contract_size": self.contract_size,
            "trade_calc_mode": self.trade_calc_mode,
            "tick_size": self.tick_size,
            "tick_value": self.tick_value,
            "tick_value_profit": self.tick_value_profit,
            "tick_value_loss": self.tick_value_loss,
        }


@dataclass(frozen=True)
class RiskResult:
    risk_amount: float
    risk_per_1_lot: float
    price_distance: float
    lots: float
    conversion_rate: float
    status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_amount": self.risk_amount,
            "risk_per_1_lot": self.risk_per_1_lot,
            "price_distance": self.price_distance,
            "lots": self.lots,
            "conversion_rate": self.conversion_rate,
            "status": self.status,
        }


def calculate_linear_price_distance_risk(
    *,
    contract: BrokerContract,
    conversion: ConversionContract,
    entry_price: float,
    stop_price: float,
    lots: float,
    conversion_price: float | None = None,
) -> RiskResult:
    """Calculate linear price-distance risk without changing allocator logic.

    Formula:
        abs(entry - stop) * contract_size * lots * conversion_rate

    This function is intentionally explicit and side-neutral. It is suitable for the
    current allocator's linear risk model. MT5 ``order_calc_profit`` remains the final
    broker authority and will be used by the parity validator added in the next step.
    """

    entry = _positive_float(entry_price, field="entry_price")
    stop = _positive_float(stop_price, field="stop_price")
    lot_count = _positive_float(lots, field="lots")
    distance = abs(entry - stop)
    if distance <= 0:
        raise BrokerRiskError("entry_price and stop_price must be different")
    rate = conversion.resolve_rate(
        conversion_price=conversion_price,
        instrument_price=entry,
    )
    risk_per_1_lot = distance * contract.contract_size * rate
    return RiskResult(
        risk_amount=risk_per_1_lot * lot_count,
        risk_per_1_lot=risk_per_1_lot,
        price_distance=distance,
        lots=lot_count,
        conversion_rate=rate,
    )


def derive_implied_account_currency_per_price_unit(
    *,
    absolute_profit: float,
    price_distance: float,
    lots: float = 1.0,
) -> float:
    """Infer MT5 account-currency P/L per one price unit and one lot."""

    profit = _positive_float(absolute_profit, field="absolute_profit")
    distance = _positive_float(price_distance, field="price_distance")
    lot_count = _positive_float(lots, field="lots")
    return profit / (distance * lot_count)
