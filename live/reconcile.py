"""
live/reconcile.py — local record vs the exchange. PURE: no IO, no network.
==========================================================================
The exchange is the only source of truth. Local state drifts: a stop fills
while the bot sleeps, an order is cancelled by hand, a run dies halfway. So
before deciding anything, every run asks the exchange what is actually true and
resolves the difference here.

Adapted from the btc-paper-bot runbook, including the two rules it learned the
expensive way:

  * A position on the exchange that we have NO record of must be ADOPTED and
    must BLOCK new entries on that symbol. Never stack a duplicate on top of
    something you are not tracking.
  * If the exchange read FAILED, do not clear anything and do not act. An
    unread exchange is not an empty exchange. That mistake once wiped the
    record of a live position and left the bot blind to its own trade.

A position with no resting stop is the single most dangerous state in the
system, so it gets its own outcome and blocks rather than being auto-fixed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import PENDING_TIMEOUT_HOURS

# outcomes
PROCEED       = "proceed"          # nothing here, free to open
HEALTHY       = "healthy"          # position + resting stop, all good
NO_STOP       = "needs_attention"  # position with NO stop — block, tell the user
ADOPTED       = "adopted_untracked" # exchange has a position we did not record
CLOSED_AWAY   = "closed_externally" # our record is stale; the position is gone
STILL_PENDING = "pending"          # entry order resting, not filled yet
EXPIRED       = "pending_expired"  # resting entry too old — cancel it
FAIL_CLOSED   = "fail_closed"      # could not read; do nothing at all

BLOCKING = (HEALTHY, NO_STOP, ADOPTED, STILL_PENDING, FAIL_CLOSED)


@dataclass
class Resolution:
    symbol: str
    outcome: str
    detail: str = ""

    @property
    def blocks_new_entry(self) -> bool:
        return self.outcome in BLOCKING

    @property
    def needs_attention(self) -> bool:
        return self.outcome in (NO_STOP, ADOPTED)


def reconcile(*, symbol: str, local: Optional[dict], exch_position: Optional[dict],
              has_resting_stop: bool, read_ok: bool = True,
              pending_age_hours: float = 0.0,
              pending_timeout_hours: float = PENDING_TIMEOUT_HOURS) -> Resolution:
    """Resolve one symbol. `local` is our record (or None); `exch_position` is
    what the exchange reports (or None). `read_ok=False` means the read failed
    and NOTHING may be concluded from it."""
    if not read_ok:
        return Resolution(symbol, FAIL_CLOSED,
                          "exchange read failed — blocking, nothing cleared")

    have_local = bool(local) and (local or {}).get("status") in ("open", "pending")
    on_exchange = bool(exch_position) and abs(float((exch_position or {}).get("size") or 0)) > 0

    if not have_local and not on_exchange:
        return Resolution(symbol, PROCEED, "flat here and on the exchange")

    if not have_local and on_exchange:
        return Resolution(symbol, ADOPTED,
                          "position on the exchange we have no record of — "
                          "adopting and blocking new entries on this symbol")

    status = (local or {}).get("status")

    if have_local and not on_exchange:
        if status == "pending":
            if pending_age_hours >= pending_timeout_hours:
                return Resolution(symbol, EXPIRED,
                                  f"entry order unfilled after {pending_age_hours:.1f}h "
                                  f"— cancel it")
            return Resolution(symbol, STILL_PENDING, "entry order resting, not filled")
        return Resolution(symbol, CLOSED_AWAY,
                          "we recorded a position but the exchange is flat — "
                          "it closed while we were away; clearing our record")

    # local says open AND the exchange agrees
    if not has_resting_stop:
        return Resolution(symbol, NO_STOP,
                          "POSITION WITH NO STOP on the exchange — blocking; "
                          "needs attention")
    return Resolution(symbol, HEALTHY, "position and stop both live")


def summarize(resolutions: list) -> dict:
    """Counts by outcome plus the symbols needing a human."""
    out = {}
    attention = []
    for r in resolutions:
        out[r.outcome] = out.get(r.outcome, 0) + 1
        if r.needs_attention:
            attention.append(r.symbol)
    return {"counts": out, "attention": attention,
            "blocked": [r.symbol for r in resolutions if r.blocks_new_entry]}
