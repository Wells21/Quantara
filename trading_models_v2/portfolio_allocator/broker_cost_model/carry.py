"""Swap spec + commission for the broker cost model (RUNBOOK v5.1).
Swap is charged in Gate C / 00d per rollover actually crossed (weekday server-midnights, Sat/Sun 0, triple day x3 --
calibrated on 409 real Moneta positions); the spec values here are the firm's CURRENT rates.
Commission = median round-trip bps of notional over the firm's real closed positions per asset (0 if none)."""
from __future__ import annotations

from typing import Any

import pandas as pd


def swap_spec(sym_spec: dict[str, Any], ref_price: float) -> dict[str, Any]:
    return {"mode": int(sym_spec["swap_mode"]), "long": float(sym_spec["swap_long"]), "short": float(sym_spec["swap_short"]),
            "triple_day": int(sym_spec["swap_rollover3days"]), "point": float(sym_spec["point"]),
            "contract_size": float(sym_spec["trade_contract_size"]), "ref_price": float(ref_price),
            "rollover_rule": "weekday server-midnights, Sat/Sun 0, triple day x3"}


def commission_bps_by_asset(deals: pd.DataFrame | None, specs: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    if deals is None or deals.empty:
        return out
    sym2a = {v["name"]: a for a, v in specs["symbols"].items() if v.get("name")}
    d = deals[deals.symbol.isin(sym2a)]
    rows = []
    for _, g in d.groupby("position_id"):
        i, o = g[g.entry == 0], g[g.entry == 1]
        if i.empty or o.empty:
            continue
        i = i.iloc[0]; a = sym2a[i.symbol]; sp = specs["symbols"][a]
        notional = float(i.volume) * float(i.price) * float(sp["trade_tick_value"]) / float(sp["trade_tick_size"])
        if notional > 0:
            rows.append((a, -float(g.commission.sum()) / notional * 1e4))
    if rows:
        r = pd.DataFrame(rows, columns=["asset", "bps"])
        out = {a: round(float(g.bps.median()), 4) for a, g in r.groupby("asset")}
    return out
