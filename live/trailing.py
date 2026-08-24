"""
live/trailing.py — the trailing stop. PURE: no IO, no network, no state.
========================================================================
This is the riskiest idea in the whole live system, so it is isolated here and
tested hardest.

How it differs from paper:
  paper  — the stop lives inside the bot. If the bot is asleep, there is no
           stop at all. We measured gaps of up to 2h26m between runs.
  live   — the stop is a resting order ON the exchange. It watches every tick
           whether the bot is awake or not. The bot's only job is to MOVE it.

Two rules keep this safe, and both are enforced here rather than left to the
caller:

  1. A stop may only ever move in the SAFER direction (up for a long, down for
     a short). A bug that computes a looser stop must never widen your risk, so
     `next_stop` returns None instead.
  2. The replacement is PLACE-THEN-CANCEL, never cancel-then-place. If the
     placement fails, the OLD stop is still resting and you are still covered.
     Briefly holding two reduce-only stops is harmless: whichever triggers
     first flattens the position and the other becomes a no-op.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import TRAIL_STEP, TRAIL_TRIGGER


def pnl_pct(entry: float, price: float, direction: str) -> float:
    """Unleveraged move in our favour, as a fraction. Leverage does not belong
    here — it multiplies the outcome, not the price move."""
    try:
        entry, price = float(entry), float(price)
    except (TypeError, ValueError):
        return 0.0
    if entry <= 0:
        return 0.0
    return (price - entry) / entry if direction == "long" else (entry - price) / entry


def next_stop(*, entry: float, price: float, direction: str, current_stop: float,
              moved_to_breakeven: bool = False,
              trigger: float = TRAIL_TRIGGER,
              step: float = TRAIL_STEP) -> Optional[float]:
    """The stop this position should have now, or None to leave it alone.

    Never returns a looser stop than `current_stop`. Never returns a stop on
    the wrong side of the current price (which the exchange would trigger
    instantly, closing the position at market for no reason).
    """
    if direction not in ("long", "short"):
        return None
    try:
        entry, price, current_stop = float(entry), float(price), float(current_stop)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or price <= 0 or current_stop <= 0:
        return None

    if pnl_pct(entry, price, direction) < trigger:
        return None                      # not far enough in profit yet

    long_ = direction == "long"
    # candidate 1: breakeven (only the first time we cross the trigger)
    # candidate 2: `step` behind the current price
    trail = price * (1 - step) if long_ else price * (1 + step)
    cands = [trail] if moved_to_breakeven else [entry, trail]
    best = max(cands) if long_ else min(cands)

    # rule 1: only ever tighter
    if long_ and best <= current_stop:
        return None
    if not long_ and best >= current_stop:
        return None
    # a stop already through the price would fire immediately — refuse
    if long_ and best >= price:
        return None
    if not long_ and best <= price:
        return None
    return best


@dataclass
class StopMove:
    """One safe stop replacement, in the order the steps must happen."""
    symbol: str
    old_stop: float
    new_stop: float
    old_order_id: Optional[str]
    reason: str = "trail"

    def steps(self) -> list:
        """PLACE first, CANCEL second. Never the other way round."""
        s = [("place_stop", {"symbol": self.symbol, "trigger_px": self.new_stop})]
        if self.old_order_id:
            s.append(("cancel_order", {"symbol": self.symbol,
                                       "order_id": self.old_order_id}))
        return s


def plan_stop_move(*, symbol: str, entry: float, price: float, direction: str,
                   current_stop: float, old_order_id: Optional[str] = None,
                   moved_to_breakeven: bool = False) -> Optional[StopMove]:
    """A StopMove to execute, or None if the stop should stay where it is."""
    nxt = next_stop(entry=entry, price=price, direction=direction,
                    current_stop=current_stop,
                    moved_to_breakeven=moved_to_breakeven)
    if nxt is None:
        return None
    reason = "breakeven" if (not moved_to_breakeven and
                             abs(nxt - entry) < 1e-12) else "trail"
    return StopMove(symbol=symbol, old_stop=float(current_stop), new_stop=nxt,
                    old_order_id=old_order_id, reason=reason)
