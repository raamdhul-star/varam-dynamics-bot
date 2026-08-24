"""
live/gates.py — safety predicates. PURE, tiny, and independently sufficient.
============================================================================
Each gate alone is enough to stop a trade. They are kept small and side-effect
free so every one can be tested on its own, and the runner enforces their order
and writes the reason to the audit log.

Precedence in the runner is cheapest-and-most-certain first:
  kill switch -> mode -> equity read -> min equity -> reconciliation
  -> per signal: score -> duplicate -> symbol already busy -> capacity -> sizing
"""
from __future__ import annotations

from . import config as C


def halted() -> bool:
    """Kill switch. Unreadable environment counts as halted."""
    return C.kill_switch_on()


def mode_allows_orders(mode: str) -> bool:
    """Only a fully-gated mainnet may send orders. Anything else is dry-run,
    and dry-run must never reach the sending layer."""
    return mode == "mainnet" and C.MAINNET_ENABLED


def equity_sufficient(equity: float) -> bool:
    """Below this, no order can clear the $10 exchange minimum, so entering is
    arithmetically impossible rather than merely unwise."""
    try:
        return float(equity) >= C.min_tradable_equity()
    except (TypeError, ValueError):
        return False


def score_ok(score) -> bool:
    try:
        return float(score) >= C.SCORE_MIN
    except (TypeError, ValueError):
        return False


def is_duplicate(fingerprint: str, seen) -> bool:
    """Same signal on the same candle — never enter it twice."""
    return bool(fingerprint) and fingerprint in (seen or ())


def symbol_busy(symbol: str, blocked) -> bool:
    """Reconciliation says this symbol already has something live. Never stack."""
    return symbol in (blocked or ())


def capacity_left(open_count: int, exposure_used: float, equity: float) -> bool:
    """Both a position count backstop and the real constraint, total exposure."""
    try:
        if int(open_count) >= C.MAX_CONCURRENT:
            return False
        return float(exposure_used) < float(equity) * C.MAX_EXPOSURE
    except (TypeError, ValueError):
        return False


def all_clear(*, mode: str, equity: float) -> tuple:
    """Run-level gates. Returns (ok, reason). Fails closed on anything odd."""
    if halted():
        return False, "kill_switch_engaged"
    if mode not in C.VALID_MODES:
        return False, "invalid_mode"
    try:
        eq = float(equity)
    except (TypeError, ValueError):
        return False, "unreadable_equity"
    if eq <= 0:
        return False, "no_equity"
    if not equity_sufficient(eq):
        return False, f"equity_below_${C.min_tradable_equity():.2f}_minimum"
    return True, "ok"
