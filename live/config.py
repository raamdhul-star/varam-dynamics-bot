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
# Real-money execution requires this True in reviewed code, ON TOP OF every
# environment gate.
#
# Set back to False 2026-08-24: the user chose to prove the plumbing on TESTNET
# first. Real money stays hard-off until that passes.
#
# WHAT TESTNET IS FOR: every test so far runs against a fake client we wrote
# ourselves. The shape of Hyperliquid's REAL responses — how it reports that an
# order filled and that a stop attached — has never been observed. Wrong
# parsing fails two ways: a trade closed for no reason, or a position left with
# NO STOP while the code believes it is protected. Testnet is the only way to
# see a real response without risking money.
#
# Flip to True only AFTER a testnet round trip has been verified on the
# exchange: position opened, stop resting against it, stop moved on a later
# run, position closed, local state matching.
#
# To stop all live trading, ANY ONE of these is sufficient on its own:
#   * this stays/returns False
#   * LIVE_KILL_SWITCH=1
#   * remove LIVE_CONFIRM
MAINNET_ENABLED = False
CONFIRM_PHRASE  = "I_UNDERSTAND"
VALID_MODES     = ("dryrun", "testnet", "mainnet")

# Testnet and mainnet arm SEPARATELY and never share credentials. Proving the
# plumbing on testnet must not be able to place a single real order.
TESTNET_ARM_VAR = "LIVE_TESTNET_ARMED"     # must equal "1"
MAINNET_ARM_VAR = "LIVE_CONFIRM"           # must equal CONFIRM_PHRASE

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

# How far back the bot looks for the high-water mark when moving a trailing
# stop. Must comfortably exceed the gap between runs -- measured GitHub drift
# is median +13 min with gaps up to 2h26m, so 180 min covers the worst observed
# case. Looking too far back is harmless: the stop can only ever tighten.
PEAK_LOOKBACK_MIN = 180

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
HL_BASE_URL   = {"mainnet": "https://api.hyperliquid.xyz",
                 "testnet": "https://api.hyperliquid-testnet.xyz"}
HTTP_TIMEOUT  = 20

# How far the market may have drifted from the signal price before we refuse
# the trade. Beyond this the setup is not the one that was scored: the stop is
# a different distance away, so the risk and the leverage are both wrong.
MAX_ENTRY_DRIFT_PCT = 1.0


def min_tradable_equity(margin_frac: float = MARGIN_FRAC,
                        lev_cap: int = LEV_CAP,
                        min_notional: float = MIN_NOTIONAL) -> float:
    """Equity below which no legal order can be built at these settings."""
    if margin_frac <= 0 or lev_cap <= 0:
        return float("inf")
    return min_notional / lev_cap / margin_frac


def account_address(mode: str = "") -> str:
    """Public wallet address to READ, for the given mode. Never a private key.

    Mode matters: reading mainnet balances while trading testnet would size
    testnet orders off real money and reconcile testnet positions against real
    ones. Each network reads its OWN address.
    """
    m = (mode or "").strip().lower()
    if m in ("mainnet", "testnet"):
        _, addr = credentials(m)
        if addr:
            return addr
    return (os.environ.get("HL_ACCOUNT_ADDRESS", "") or "").strip()


def info_url(mode: str = "") -> str:
    """Read endpoint for the given mode. A live mode must read its OWN network;
    dryrun reads mainnet, since that is the real market we are simulating."""
    m = (mode or "").strip().lower()
    if m in HL_BASE_URL:
        return HL_BASE_URL[m] + "/info"
    return HL_INFO_URL


def mode() -> str:
    """Requested mode, defaulting to the safe one. Mainnet is downgraded to
    dryrun unless the code gate AND the confirm phrase both pass."""
    m = (os.environ.get("LIVE_MODE", "dryrun") or "dryrun").strip().lower()
    if m not in VALID_MODES:
        return "dryrun"
    if m == "mainnet":
        if not MAINNET_ENABLED:
            return "dryrun"
        if (os.environ.get(MAINNET_ARM_VAR, "") or "").strip() != CONFIRM_PHRASE:
            return "dryrun"
    if m == "testnet":
        # Testnet still has to be armed deliberately; an unset flag means
        # dry-run, never "probably fine, it is only fake money".
        if (os.environ.get(TESTNET_ARM_VAR, "") or "").strip() != "1":
            return "dryrun"
    return m


def credentials(mode: str) -> tuple:
    """(private_key, account_address) for a live mode, or (None, None).

    Read straight from the environment at the moment of use and never stored,
    cached, logged or returned anywhere else. Testnet and mainnet use different
    variables so one can never be used against the other.
    """
    if mode == "mainnet":
        return (os.environ.get("HL_MAINNET_PRIVATE_KEY", "").strip() or None,
                os.environ.get("HL_MAINNET_ACCOUNT_ADDRESS", "").strip() or None)
    if mode == "testnet":
        return (os.environ.get("HL_TESTNET_PRIVATE_KEY", "").strip() or None,
                os.environ.get("HL_TESTNET_ACCOUNT_ADDRESS", "").strip() or None)
    return (None, None)


def credentials_present(mode: str) -> bool:
    if mode == "dryrun":
        return True
    k, a = credentials(mode)
    return bool(k and a)


def kill_switch_on() -> bool:
    """True (halt) on any affirmative value. Unreadable env -> halt."""
    try:
        return (os.environ.get("LIVE_KILL_SWITCH", "") or "").strip().lower() \
            in ("1", "true", "yes", "on")
    except Exception:
        return True
