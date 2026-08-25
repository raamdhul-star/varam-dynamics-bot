"""
live/notify.py — Telegram messages for live trades. PURE builders + one sender.
==============================================================================
Design rules, from the noise problem we just fixed:

  * Only real events get a message: a trade opened, a trade closed, something
    needs a human. No heartbeats, no "nothing happened".
  * Spacious and aligned. These arrive among ~20 signal alerts a day, so a
    real-money event has to be recognisable in one glance.
  * Numbers in dollars, not just percentages. "-$17" lands; "-1.4%" does not.
  * Never send a key, an address, or anything a screenshot should not carry.

The builders are pure so the exact text is unit-tested. `send` is the only part
that touches the network, and it is gated by the caller.
"""
from __future__ import annotations

from .config import TRAIL_STEP, TRAIL_TRIGGER

DISCLAIMER = "⚠️ Live trade · educational only · not financial advice"


def fmt_px(v) -> str:
    """Price with sane precision across a 6-order-of-magnitude book.

    BTC at 79470 and PENGU at 0.009918 both have to read cleanly, so the
    decimals follow the magnitude instead of being fixed.
    """
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 10:
        return f"{v:,.2f}"
    if a >= 0.1:
        return f"{v:.4f}"
    return f"{v:.6f}".rstrip("0").rstrip(".")


def fmt_usd(v, signed: bool = False) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    s = f"{abs(v):,.2f}"
    if signed:
        return f"{'+' if v >= 0 else '-'}${s}"
    return f"${s}"


def _pct_from(entry, px, direction: str):
    try:
        entry, px = float(entry), float(px)
    except (TypeError, ValueError):
        return None
    if entry <= 0:
        return None
    return ((px - entry) / entry * 100 if direction == "long"
            else (entry - px) / entry * 100)


def build_opened(*, symbol: str, direction: str, leverage: int, entry: float,
                 stop: float, target=None, size=None, notional=None,
                 margin=None) -> str:
    """Sent when a live position is opened AND its stop is confirmed resting."""
    stop_pct = _pct_from(entry, stop, direction)
    risk = None
    if margin is not None and stop_pct is not None:
        # loss if stopped out, on the margin, at this leverage
        risk = abs(float(margin) * (stop_pct / 100.0) * int(leverage or 1))

    L = ["🟢 <b>TRADE OPENED</b>", "",
         f"<b>{symbol}</b> · {direction.upper()} · {int(leverage or 1)}×", "",
         f"Entry    {fmt_px(entry)}"]
    L.append(f"Stop     {fmt_px(stop)}"
             + (f"   ({stop_pct:+.1f}%)" if stop_pct is not None else ""))
    if target:
        tp_pct = _pct_from(entry, target, direction)
        L.append(f"Target   {fmt_px(target)}"
                 + (f"   ({tp_pct:+.1f}%)" if tp_pct is not None else ""))
    L.append("")
    if size is not None:
        L.append(f"Size     {size} {symbol}"
                 + (f"   ({fmt_usd(notional)})" if notional else ""))
    if margin is not None:
        L.append(f"Margin   {fmt_usd(margin)}")
    if risk is not None:
        L.append(f"Risk     {fmt_usd(risk)} if stopped out")
    L += ["",
          f"Stop starts trailing {TRAIL_STEP*100:.0f}% behind "
          f"once {TRAIL_TRIGGER*100:.0f}% ahead.",
          DISCLAIMER]
    return "\n".join(L)


def build_closed(*, symbol: str, direction: str, leverage: int, entry: float,
                 exit_px: float, pnl_usd=None, reason: str = "",
                 margin=None, account_value=None) -> str:
    """Sent when a live position is gone. `pnl_usd` comes from the exchange's
    own realised figure where available — never a re-derived guess."""
    move = _pct_from(entry, exit_px, direction)
    win = None
    if pnl_usd is not None:
        try:
            win = float(pnl_usd) >= 0
        except (TypeError, ValueError):
            win = None
    elif move is not None:
        win = move >= 0

    head = ("🟢 <b>TRADE CLOSED — PROFIT</b>" if win
            else "🔴 <b>TRADE CLOSED — LOSS</b>" if win is False
            else "⚪ <b>TRADE CLOSED</b>")
    L = [head, "",
         f"<b>{symbol}</b> · {direction.upper()} · {int(leverage or 1)}×", "",
         f"Entry    {fmt_px(entry)}",
         f"Exit     {fmt_px(exit_px)}"
         + (f"   ({move:+.1f}%)" if move is not None else "")]
    if reason:
        L.append(f"Closed   {reason}")
    L.append("")
    if pnl_usd is not None:
        on_margin = ""
        if margin:
            try:
                on_margin = f"   ({float(pnl_usd)/float(margin)*100:+.1f}% on margin)"
            except (TypeError, ValueError, ZeroDivisionError):
                on_margin = ""
        L.append(f"P&amp;L      {fmt_usd(pnl_usd, signed=True)}{on_margin}")
    if account_value is not None:
        L.append(f"Account  {fmt_usd(account_value)}")
    L += ["", DISCLAIMER]
    return "\n".join(L)


def build_attention(*, symbol: str, reason: str, detail: str = "") -> str:
    """Something a human must look at. Deliberately loud — these are rare, so
    they are allowed to shout."""
    L = ["🚨 <b>NEEDS ATTENTION</b>", "",
         f"<b>{symbol}</b> — {reason}"]
    if detail:
        L += ["", detail]
    L += ["", "Check the exchange. New entries on this symbol are blocked "
          "until it is resolved.", DISCLAIMER]
    return "\n".join(L)


def build_weekly(*, account_value, week_start_value=None, closed: list = (),
                 holding: list = (), runs: int = 0, expected_runs: int = 0,
                 label: str = "") -> str:
    """One weekly message. Carries the LIVENESS line, because retiring the
    hourly no-signal message means silence no longer proves the bot is alive."""
    wins = [c for c in closed if (c.get("pnl_usd") or 0) > 0]
    losses = [c for c in closed if (c.get("pnl_usd") or 0) <= 0]
    pnl = sum(float(c.get("pnl_usd") or 0) for c in closed)

    L = [f"📊 <b>LIVE WEEKLY</b>{' — ' + label if label else ''}", "",
         f"Account   {fmt_usd(account_value)}"]
    if week_start_value:
        try:
            chg = (float(account_value) / float(week_start_value) - 1) * 100
            L[-1] += f"   ({chg:+.1f}% this week)"
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    L += ["", f"Closed    {len(closed)} trades"
          + (f" · {len(wins)}W / {len(losses)}L" if closed else "")]
    if closed:
        L.append(f"P&amp;L       {fmt_usd(pnl, signed=True)}")
    L.append("")
    if holding:
        L.append(f"Open      {len(holding)} position"
                 + ("s" if len(holding) != 1 else ""))
        for h in holding:
            p = h.get("pnl_pct")
            shield = "" if h.get("protected", True) else "  ⚠️ NO STOP"
            L.append(f"   {h.get('symbol')} {str(h.get('direction','')).lower()}"
                     + (f"   {p:+.1f}%" if p is not None else "") + shield)
    else:
        L.append("Open      nothing")
    L.append("")
    if expected_runs:
        ok = runs >= expected_runs * 0.9
        L.append(f"{'✅' if ok else '⚠️'} Bot ran {runs} of ~{expected_runs} "
                 f"hourly checks this week.")
    L.append(DISCLAIMER)
    return "\n".join(L)


def send(messages, sender=None) -> int:
    """Send plain messages — no buttons, no markup, no trade flow. Import of the
    Telegram sender is lazy so this module stays testable with no network and
    no bot token present."""
    if not messages:
        return 0
    if sender is None:
        from telegram.bot import send_message as sender   # noqa: WPS433
    n = 0
    for m in messages:
        if not m:
            continue
        try:
            sender(m)
            n += 1
        except Exception:                                  # noqa: BLE001
            # A failed notification must never break a trading run: the trade
            # and its stop matter, the message does not.
            continue
    return n
