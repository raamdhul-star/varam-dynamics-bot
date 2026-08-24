"""
live/config.py — live-execution settings. FAIL-CLOSED by design.
====================================================================
Anything unset, unreadable or invalid degrades to the safest behaviour:
dry-run, trade nothing. Nothing here places an order; L1 is read-only.

Sizing was chosen from measurement, not preference (1-minute replay of 393
closed calls, real fees, real bot cadence):

  * Hyperliquid rejects any order under $10 of notional. On a $25 account at
    10% margin EVERY order is unplaceable, so the paper sizing cannot be used.
  * Taking any call at 50% margin clears the floor but holds 1-2 positions, so
    WHICH calls you catch is luck: outcomes spread 32x ($153 .. $4920).
  * Taking ALL calls at a flat 25% margin was measured best: $129 vs $55 for a
    tight-stop-only filter, with the luck spread only 2x ($94..$207). Its WORST
    case beats the tight filter's BEST case, at 2 points more drawdown.
  * Topping wide-stop calls up to the $10 minimum ("use the idle capital")
    earns more on average but is rejected: drawdown -32%, spread 5x. Dollar
    risk on a stop-out is margin x 7% REGARDLESS of leverage (the leverage
    formula cancels the stop distance), so forcing a 1x call to be legal at
    $10 of margin risks 3x what a 3x call at $3.33 risks. Flat margin keeps
    risk per trade equal; wide-stop calls become affordable on their own as
    the account grows past ~$40.

MARGIN_FRAC 0.25 x LEV_CAP 3 means no legal order exists below about $13.33 of
equity (see min_tradable_equity) -- the bot stops opening trades deep in a
drawdown rather than shrinking into invalid orders.
"""
from __future__ import annotations

import os

# ── HARD CODE GATE ───────────────────────────────────────────────────────────
# Real-money execution requires this to be flipped True in reviewed code, on
# top of every environment gate. L1 has no order-placement path at all, so it
# stays False and MUST stay False until the executor is built and tested.
MAINNET_ENABLED = False
CONFIRM_PHRASE  = "I_UNDERSTAND"
VALID_MODES     = ("dryrun", "mainnet")

# ── sizing (measured; see module docstring) ──────────────────────────────────
MARGIN_FRAC   = 0.25    # margin per trade as a fraction of equity
LEV_CAP       = 3       # our ceiling; the asset's own max can be lower
MAX_EXPOSURE  = 0.90    # total posted margin never exceeds 90% of equity
MIN_NOTIONAL  = 10.0    # Hyperliquid minimum order value (USD)
MAX_CONCURRENT = 4      # 3 full + 1 partial fills MAX_EXPOSURE; the
                        # exposure ceiling is the real constraint, this is a backstop
# How long an UNFILLED entry order may sit before we cancel it and free the
# margin. Entries are IOC today (fill at once or die), so no entry can rest and
# this never fires -- it is here for the day we use a resting limit entry, and
# it is kept short so capital is never parked in an order that is not working.
PENDING_TIMEOUT_HOURS = 2.0

# ── strategy filter ──────────────────────────────────────────────────────────
SCORE_MIN     = 7.5     # same floor the alerts use
TIGHT_ONLY    = False   # take every qualifying call; the $10 floor and the
                        # flat margin decide affordability (measured: see above)
RISK_PCT      = 0.07    # 7% risk basis behind the leverage suggestion

# ── trailing stop (live only; paper deliberately stays at 0.05) ──────────────
# +3% chosen over +5% because it replicated out-of-sample: profit factor
# 2.55 -> 2.91 on the first half of history and 2.87 -> 3.14 on the second.
# Trail WIDTH was tested too (1.5%..8%) and did NOT replicate, so it stays 2%.
TRAIL_TRIGGER = 0.03
TRAIL_STEP    = 0.02

HL_INFO_URL   = "https://api.hyperliquid.xyz/info"
HTTP_TIMEOUT  = 20


def min_tradable_equity(margin_frac: float = MARGIN_FRAC,
                        lev_cap: int = LEV_CAP,
                        min_notional: float = MIN_NOTIONAL) -> float:
    """Equity below which no legal order can be built at these settings."""
    if margin_frac <= 0 or lev_cap <= 0:
        return float("inf")
    return min_notional / lev_cap / margin_frac


def account_address() -> str:
    """Public wallet address to READ. Never a private key."""
    return (os.environ.get("HL_ACCOUNT_ADDRESS", "") or "").strip()


def mode() -> str:
    """Requested mode, defaulting to the safe one. Mainnet is downgraded to
    dryrun unless the code gate AND the confirm phrase both pass."""
    m = (os.environ.get("LIVE_MODE", "dryrun") or "dryrun").strip().lower()
    if m not in VALID_MODES:
        return "dryrun"
    if m == "mainnet":
        if not MAINNET_ENABLED:
            return "dryrun"
        if (os.environ.get("LIVE_CONFIRM", "") or "").strip() != CONFIRM_PHRASE:
            return "dryrun"
    return m


def kill_switch_on() -> bool:
    """True (halt) on any affirmative value. Unreadable env -> halt."""
    try:
        return (os.environ.get("LIVE_KILL_SWITCH", "") or "").strip().lower() \
            in ("1", "true", "yes", "on")
    except Exception:
        return True
