"""
live/sizing.py — pure order maths. No IO, no network, no state.
===============================================================
Turns a signal plus an account snapshot into either a concrete, exchange-legal
order or an explicit skip reason. Every rejection is named so the Telegram note
and the audit log can say exactly why a call was not taken.

Order of checks matters and is deliberately cheapest-and-most-certain first:
geometry -> tight-stop filter -> free margin -> $10 floor -> size rounding ->
$10 floor again after rounding (rounding can only ever reduce the size).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Optional

from .config import (MARGIN_FRAC, LEV_CAP, MAX_EXPOSURE, MAX_ENTRY_DRIFT_PCT,
                     MIN_NOTIONAL, RISK_PCT, TIGHT_ONLY)


@dataclass
class OrderPlan:
    symbol: str
    direction: str
    entry: float
    stop: float
    mark: float = 0.0
    drift_pct: float = 0.0
    leverage: int = 0
    margin: float = 0.0
    notional: float = 0.0
    size: float = 0.0
    stop_distance_pct: float = 0.0
    skip_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.skip_reason is None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


def floor_to(x: float, decimals: int) -> float:
    """Floor to the exchange's size decimals. NEVER round up: rounding up could
    push the order above the intended notional (or past free margin)."""
    if x <= 0:
        return 0.0
    if decimals <= 0:
        return float(math.floor(x))
    f = 10 ** decimals
    return math.floor(x * f) / f


def suggested_leverage(entry: float, sl: float, cap: int = LEV_CAP) -> int:
    """round(7% / stop-distance), floor 1, capped. Whole numbers only —
    Hyperliquid does not accept fractional leverage."""
    try:
        slp = abs(float(entry) - float(sl)) / float(entry)
    except (TypeError, ValueError, ZeroDivisionError):
        return 1
    if slp <= 0:
        return cap
    return max(1, min(cap, round(RISK_PCT / slp)))


def is_tight_stop(entry: float, sl: float, cap: int = LEV_CAP) -> bool:
    """The live filter: only calls whose suggested leverage reaches the cap.
    These need the least margin to clear the $10 floor, so several fit at once
    — which is what makes the outcome stop depending on which calls we catch."""
    return suggested_leverage(entry, sl, cap) >= cap


def geometry_ok(direction: str, entry: float, stop: float) -> bool:
    """A long's stop must sit below entry; a short's above. Anything else is a
    malformed signal and must never reach the exchange."""
    try:
        entry, stop = float(entry), float(stop)
    except (TypeError, ValueError):
        return False
    if entry <= 0 or stop <= 0:
        return False
    return stop < entry if direction == "long" else stop > entry


def plan_order(*, symbol: str, direction: str, entry: float, stop: float,
               equity: float, used_margin: float, sz_decimals: int,
               asset_max_leverage: int, mark_price: float | None = None,
               margin_frac: float = MARGIN_FRAC,
               lev_cap: int = LEV_CAP, min_notional: float = MIN_NOTIONAL,
               max_exposure: float = MAX_EXPOSURE,
               tight_only: bool = TIGHT_ONLY,
               max_drift_pct: float = MAX_ENTRY_DRIFT_PCT) -> OrderPlan:
    """Build a legal order, or explain precisely why we cannot.

    `mark_price` is what the market is RIGHT NOW. The signal's `entry` can be
    an hour old, and sizing off a stale price gets the risk wrong: fill 0.9%
    worse than planned and a 2.5% stop is really a 3.4% stop, a third more risk
    than intended. So everything below is measured from the mark when we have
    it, and a mark that has drifted too far means the setup is no longer the
    one that was scored, so we decline it.
    """
    p = OrderPlan(symbol=symbol, direction=direction,
                  entry=float(entry or 0), stop=float(stop or 0))

    if direction not in ("long", "short"):
        p.skip_reason = "bad_direction"; return p
    # A MISSING stop is a wiring fault, not bad market data. Reported
    # separately so a schema mismatch is loud instead of hiding behind
    # "bad_geometry" — that exact confusion once made every call skip silently.
    if entry in (None, "") or stop in (None, ""):
        p.skip_reason = "missing_price_or_stop"; return p
    if not geometry_ok(direction, entry, stop):
        p.skip_reason = "bad_geometry"; return p
    if equity is None or equity <= 0:
        p.skip_reason = "no_equity"; return p

    # price everything off the mark when we have one
    p.mark = p.entry
    if mark_price is not None:
        try:
            mk = float(mark_price)
        except (TypeError, ValueError):
            p.skip_reason = "bad_mark_price"; return p
        if mk <= 0:
            p.skip_reason = "bad_mark_price"; return p
        p.mark = mk
        p.drift_pct = (mk - p.entry) / p.entry * 100
        if abs(p.drift_pct) > max_drift_pct:
            p.skip_reason = "price_moved_away"; return p
        # the stop must still be on the correct side of where we will fill
        if not geometry_ok(direction, p.mark, p.stop):
            p.skip_reason = "bad_geometry"; return p

    p.stop_distance_pct = abs(p.mark - p.stop) / p.mark * 100

    if tight_only and not is_tight_stop(p.mark, p.stop, lev_cap):
        p.skip_reason = "not_tight_stop"; return p

    try:
        amax = int(asset_max_leverage)
    except (TypeError, ValueError):
        p.skip_reason = "unknown_asset_max_leverage"; return p
    if amax < 1:
        p.skip_reason = "unknown_asset_max_leverage"; return p

    # our cap AND the asset's own ceiling; the exchange rejects anything above
    p.leverage = max(1, min(suggested_leverage(p.mark, p.stop, lev_cap), amax))

    free = equity * max_exposure - max(0.0, used_margin or 0.0)
    p.margin = min(margin_frac * equity, free)
    if p.margin <= 0:
        p.margin = 0.0
        p.skip_reason = "no_free_margin"; return p

    p.notional = p.margin * p.leverage
    if p.notional < min_notional:
        p.skip_reason = "below_min_order"; return p

    try:
        decs = int(sz_decimals)
    except (TypeError, ValueError):
        p.skip_reason = "unknown_size_decimals"; return p

    p.size = floor_to(p.notional / p.mark, decs)
    if p.size <= 0:
        p.skip_reason = "size_rounds_to_zero"; return p

    # flooring only ever shrinks the order, so re-check the floor afterwards
    p.notional = p.size * p.mark
    if p.notional < min_notional:
        p.skip_reason = "below_min_after_rounding"; return p
    p.margin = p.notional / p.leverage
    return p
