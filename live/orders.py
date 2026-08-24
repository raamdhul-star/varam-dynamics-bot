"""
live/orders.py — build the exact order payloads. PURE: no IO, no network.
=========================================================================
Turns an OrderPlan into the request dicts Hyperliquid expects. Building them is
separated from sending them on purpose: everything risky (sides, reduce-only
flags, trigger prices, tick rounding) is decided here where it can be unit
tested, and the sending layer stays a thin, reviewable wrapper.

NOTHING IN THIS MODULE SENDS ANYTHING. There is no HTTP here.

Bracket shape for our strategy: an entry plus a resting STOP. There is no
take-profit — the measured edge comes from the trailing stop, and a fixed
target would cut the winners that carry the whole result.

Hyperliquid price rules (perps): at most 5 significant figures AND at most
(6 - szDecimals) decimal places. Sizes are FLOORED to szDecimals. The exchange
rejects unrounded values outright, so both are applied here.
"""
from __future__ import annotations

import math
from typing import Optional

from .sizing import OrderPlan, floor_to

MAX_DECIMALS_PERP = 6
SIG_FIGS = 5


def round_px(px: float, sz_decimals: int) -> float:
    """5 significant figures, then clamp the decimal places."""
    try:
        px = float(px)
    except (TypeError, ValueError):
        return 0.0
    if px <= 0:
        return 0.0
    max_dec = max(0, MAX_DECIMALS_PERP - int(sz_decimals))
    sig = float(f"{px:.{SIG_FIGS}g}")
    return round(sig, max_dec)


def round_sz(sz: float, sz_decimals: int) -> float:
    """Floor, never round up — rounding up could exceed the free margin."""
    return floor_to(sz, int(sz_decimals))


def _opposite(direction: str) -> bool:
    """is_buy for the CLOSING side of a position."""
    return direction != "long"          # long closes with a sell -> is_buy False


def build_entry(plan: OrderPlan, sz_decimals: int, slippage: float = 0.01) -> dict:
    """Aggressive limit (IOC) rather than a bare market order, so a thin book
    cannot fill us arbitrarily far away. The limit is set `slippage` beyond the
    entry in our direction; anything worse simply does not fill."""
    long_ = plan.direction == "long"
    limit = plan.entry * (1 + slippage) if long_ else plan.entry * (1 - slippage)
    return {"action": "entry", "symbol": plan.symbol, "is_buy": long_,
            "size": round_sz(plan.size, sz_decimals),
            "limit_px": round_px(limit, sz_decimals),
            "reduce_only": False, "tif": "Ioc", "leverage": plan.leverage}


def build_stop(symbol: str, direction: str, size: float, stop_px: float,
               sz_decimals: int) -> dict:
    """Reduce-only stop trigger on the closing side. Reduce-only matters: if the
    position is already gone this can only ever be a no-op, never a new
    position in the opposite direction."""
    return {"action": "stop", "symbol": symbol, "is_buy": _opposite(direction),
            "size": round_sz(size, sz_decimals),
            "trigger_px": round_px(stop_px, sz_decimals),
            "reduce_only": True, "is_market": True, "tpsl": "sl"}


def build_bracket(plan: OrderPlan, sz_decimals: int,
                  slippage: float = 0.01) -> Optional[dict]:
    """Entry + resting stop, with the ordering rules the caller must obey.

    Returns None for an unusable plan rather than a half-built order.
    """
    if not plan.ok or plan.size <= 0:
        return None
    entry = build_entry(plan, sz_decimals, slippage)
    stop = build_stop(plan.symbol, plan.direction, plan.size, plan.stop, sz_decimals)
    if entry["size"] <= 0 or stop["size"] <= 0:
        return None
    if entry["limit_px"] <= 0 or stop["trigger_px"] <= 0:
        return None
    # the stop must still be on the correct side after rounding
    if plan.direction == "long" and stop["trigger_px"] >= plan.entry:
        return None
    if plan.direction == "short" and stop["trigger_px"] <= plan.entry:
        return None
    return {"symbol": plan.symbol, "direction": plan.direction,
            "grouping": "normalTpsl", "orders": [entry, stop],
            # If the stop does not come back confirmed, the position is naked.
            # That is the most dangerous state in the system, so the caller
            # must FLATTEN rather than hope the next run fixes it.
            "on_stop_missing": "flatten"}


def bracket_is_complete(resp: dict) -> bool:
    """True only when BOTH legs are confirmed.

    A stop can be LIVE without an order id: Hyperliquid answers a new trigger
    order with the bare string "waitingForTrigger". Requiring an id here made
    the code flatten three healthy positions on testnet, so acceptance is what
    counts. `stop_live` is the authoritative answer when the backend provides
    it; the id-based check remains for backends that do not.
    """
    if not isinstance(resp, dict):
        return False
    if not resp.get("entry_ok"):
        return False
    if "stop_live" in resp:
        return bool(resp["stop_live"])
    return bool(resp.get("stop_order_id"))
