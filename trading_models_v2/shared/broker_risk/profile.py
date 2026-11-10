from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import BrokerContract, BrokerRiskError, ConversionContract


@dataclass(frozen=True)
class BrokerProfileBundle:
    account_currency: str
    contracts: dict[str, BrokerContract]
    conversions: dict[str, ConversionContract]
    source_path: str
    artifact_policy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_currency": self.account_currency,
            "contracts": {
                asset: contract.to_dict()
                for asset, contract in sorted(self.contracts.items())
            },
            "conversions": {
                asset: conversion.to_dict()
                for asset, conversion in sorted(self.conversions.items())
            },
            "source_path": self.source_path,
            "artifact_policy": self.artifact_policy,
        }


def load_broker_profile_payload(path: str | Path) -> dict[str, Any]:
    profile_path = Path(path)
    if not profile_path.exists() or not profile_path.is_file():
        raise FileNotFoundError(profile_path)
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"broker profile must be a JSON object: {profile_path}")
    return payload


def _profile_contract_mapping(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    contracts = payload.get("contracts")
    if not isinstance(contracts, dict) or not contracts:
        raise BrokerRiskError("broker profile contains no contracts mapping")
    return {
        str(asset).strip().upper(): entry
        for asset, entry in contracts.items()
        if str(asset).strip() and isinstance(entry, dict)
    }


def load_broker_profile(
    path: str | Path,
    *,
    max_staleness_seconds: int = 259200,
    require_status_pass: bool = True,
) -> BrokerProfileBundle:
    """Load the local hashless MT5 broker snapshot into runtime contracts."""

    profile_path = Path(path)
    payload = load_broker_profile_payload(profile_path)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if require_status_pass and str(summary.get("status", "")).upper() != "PASS":
        raise BrokerRiskError(
            f"broker profile summary status must be PASS: {summary.get('status')!r}"
        )

    account_payload = (
        payload.get("account") if isinstance(payload.get("account"), dict) else {}
    )
    account_currency = str(account_payload.get("currency") or "").strip().upper()
    if not account_currency:
        raise BrokerRiskError("broker profile account currency is missing")

    contracts: dict[str, BrokerContract] = {}
    conversions: dict[str, ConversionContract] = {}
    for asset, entry in _profile_contract_mapping(payload).items():
        status = str(entry.get("status") or "").strip().lower()
        if status != "captured":
            if require_status_pass:
                raise BrokerRiskError(
                    f"{asset}: broker profile entry status is not captured: {status!r}"
                )
            continue

        nested_contract = (
            entry.get("contract") if isinstance(entry.get("contract"), dict) else {}
        )
        contract_payload = {
            **nested_contract,
            "asset": asset,
            "broker_symbol": entry.get("broker_symbol") or asset,
            "account_currency": entry.get("account_currency") or account_currency,
            "profit_currency": entry.get("profit_currency")
            or entry.get("currency_profit"),
        }
        contract = BrokerContract.from_mapping(contract_payload)
        if contract.account_currency != account_currency:
            raise BrokerRiskError(
                f"{asset}: contract account currency {contract.account_currency} "
                f"does not match profile account currency {account_currency}"
            )

        hint = (
            entry.get("conversion_hint")
            if isinstance(entry.get("conversion_hint"), dict)
            else {}
        )
        mode = str(hint.get("mode") or "").strip().lower()
        conversion_payload = {
            "mode": mode,
            "profit_currency": contract.profit_currency,
            "account_currency": contract.account_currency,
            "symbol": hint.get("symbol") or "",
            "timestamp_policy": "latest_at_or_before_entry",
            "max_staleness_seconds": (
                None
                if mode in {"identity", "explicit_rate", "instrument_inverse"}
                else int(max_staleness_seconds)
            ),
        }
        conversion = ConversionContract.from_mapping(conversion_payload)
        contracts[asset] = contract
        conversions[asset] = conversion

    if not contracts:
        raise BrokerRiskError("broker profile produced no valid broker contracts")

    return BrokerProfileBundle(
        account_currency=account_currency,
        contracts=contracts,
        conversions=conversions,
        source_path=str(profile_path),
        artifact_policy=str(
            payload.get("artifact_policy") or "hashless_overwriteable_snapshot"
        ),
    )
