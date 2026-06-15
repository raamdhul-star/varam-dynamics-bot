"""
main.py — Varam-Dynamics orchestrator
======================================
Commands:
  scan     — fetch all HL assets, find CPR signals, send Telegram alert
  monitor  — update paper positions + process Telegram callbacks
  status   — print current paper trade status
  setup    — test Telegram connection
"""
from __future__ import annotations
import argparse, os, sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from scanner.assets   import get_assets, calc_leverage, liquidity_score
from scanner.asset_classes import classify
from scanner.fetcher  import fetch_all, SCAN_INTERVALS, LOWER_TF_INTERVALS
from scanner.cpr_engine import attach_cpr, detect_signals, get_latest_signal
from scanner.scorer   import score_signal
from paper.tracker    import (open_position, update_positions,
                               get_open_positions, get_performance_summary)
import telegram.bot as tg

ACCOUNT_SIZE     = float(os.environ.get("ACCOUNT_SIZE", "200"))
MAX_PER_CATEGORY = 10    # Sprint C: per-category alert ceiling (NOT a quota — no padding)

# Sprint H1: one informational message when a scan runs but sends no alert
# (no qualified signals, or all signals A2-suppressed, or all uncategorized).
# Plain text only — no buttons/markup/batches/trade flow.
HL_NO_ALERT_MSG = (
    "🔍 <b>Hyperliquid Signal Check</b>\n\n"
    "No qualified Hyperliquid signals this run.\n\n"
    "ℹ️ Hyperliquid scan completed · no trade alert generated\n"
    "⚠️ Educational only · not financial advice · high-risk · DYOR")


def _hl_no_alert_due(sent_any: bool, had_data: bool) -> bool:
    """Send the no-alert message only when the scan had data but no real alert
    went out. A total fetch outage (had_data False) is NOT 'no signal'."""
    return had_data and not sent_any


def _split_by_category(ranked_sigs, cap: int = MAX_PER_CATEGORY):
    """Split score-RANKED signals into (crypto_top, rwa_top, uncategorized_syms).
    Each category is capped at `cap`; input order (score) is preserved; nothing
    is padded. Uncategorized symbols are collected for reporting, never bucketed."""
    crypto_top, rwa_top, uncategorized = [], [], []
    for sig in ranked_sigs:
        cls = classify(sig.symbol)
        if cls == "crypto":
            if len(crypto_top) < cap:
                crypto_top.append(sig)
        elif cls == "rwa_perp":
            if len(rwa_top) < cap:
                rwa_top.append(sig)
        else:
            uncategorized.append(sig.symbol)
    return crypto_top, rwa_top, uncategorized


def ist_now():
    return (datetime.now(timezone.utc) + timedelta(hours=5,minutes=30))\
           .strftime("%d %b %Y, %I:%M %p IST")


# ── Friendly labels for the auto paper-trade update message (display only) ──
EXIT_STYLE_LABELS = {
    "fixed_pct":  "Fixed % Exit",
    "cpr_target": "Target Exit",
    "trailing":   "Trailing Stop Exit",
}
EXIT_STYLE_MEANING = {
    "fixed_pct":  "this paper trade used the fixed +10%/-7% rule, not the trailing stop.",
    "cpr_target": "this paper trade aimed for the target zone.",
    "trailing":   "this paper trade trailed the stop to protect profit as price moved our way.",
}


# Short, generic method labels for the grouped paper-trade update (display only).
EXIT_STYLE_SHORT = {"fixed_pct": "Fixed %", "cpr_target": "Target", "trailing": "Trailing"}
_STYLE_ORDER = ["fixed_pct", "cpr_target", "trailing"]   # stable display order

def _verdict(result: str) -> tuple[str, str]:
    if result == "win":
        return "✅", "Win"
    if result == "loss":
        return "❌", "Loss"
    return "↔️", "Breakeven"


def _group_closed(closed: list[dict]) -> list[str]:
    """Group closed paper records by (symbol, direction, entry_price) and render
    one clean block each. Display only — reads the records, never mutates them."""
    groups: dict = {}
    order: list = []
    for c in closed:
        key = (c.get("symbol", ""), str(c.get("direction", "")), c.get("entry_price"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(c)

    blocks = []
    for key in order:
        sym, direction, entry = key
        recs = groups[key]
        # stable method order
        recs = sorted(recs, key=lambda r: _STYLE_ORDER.index(r.get("exit_style"))
                      if r.get("exit_style") in _STYLE_ORDER else 99)
        head = f"<b>{sym} {direction.upper()}</b>"
        entry_s = f"{entry:.6g}" if entry is not None else "?"

        if len(recs) == 1:                                   # single method
            r = recs[0]
            emoji, verdict = _verdict(r.get("result"))
            label = EXIT_STYLE_SHORT.get(r.get("exit_style"), r.get("exit_style", ""))
            lines = [f"{emoji} {head} — {verdict}"]
            exitp = r.get("exit_price")
            lines.append(f"   Entry {entry_s} → Exit {exitp:.6g}" if exitp is not None
                         else f"   Entry {entry_s}")
            lines.append(f"   Result: {r.get('pnl_pct', 0.0):+.2f}%")
            lines.append(f"   Method: {label}")
            blocks.append("\n".join(lines))
            continue

        pnls = [r.get("pnl_pct", 0.0) for r in recs]
        if len(set(pnls)) == 1:                              # uniform: all pnl identical
            r0 = recs[0]
            emoji, verdict = _verdict(r0.get("result"))
            methods = ", ".join(EXIT_STYLE_SHORT.get(r.get("exit_style"), "?") for r in recs)
            lines = [f"{emoji} {head} — {verdict}"]
            exitp = r0.get("exit_price")
            lines.append(f"   Entry {entry_s} → Exit {exitp:.6g}" if exitp is not None
                         else f"   Entry {entry_s}")
            lines.append(f"   Result: {pnls[0]:+.2f}%")
            lines.append(f"   Methods checked: {methods}")
            blocks.append("\n".join(lines))
        else:                                                # mixed: pnl differ
            lines = [f"⚖️ {head} — Mixed result", f"   Entry {entry_s}"]
            for r in recs:
                label = EXIT_STYLE_SHORT.get(r.get("exit_style"), "?")
                lines.append(f"   {label}: {r.get('pnl_pct', 0.0):+.2f}%")
            blocks.append("\n".join(lines))
    return blocks


def _paper_update_message(closed: list[dict]) -> str:
    """Full grouped Paper Trade Update text (header + blocks + footer)."""
    return ("📋 <b>Paper Trade Update</b> (auto-simulated)\n\n"
            + "\n\n".join(_group_closed(closed))
            + "\n\nPaper updates are auto-simulated tracking results, not manual trades.")


def _prepare(df):
    if df is None or len(df) < 35: return None
    df = attach_cpr(df)
    df = detect_signals(df)
    return df


def lower_tf_ok(symbol, direction, data):
    for tf in ["15m","30m"]:
        df = data.get(symbol,{}).get(tf)
        if df is None: continue
        df = _prepare(df)
        if df is None: continue
        last = df.iloc[-2]
        if direction=="long"  and last["close"] > last["sma9"]: return True
        if direction=="short" and last["close"] < last["sma9"]: return True
    return False


def run_scan():
    print(f"\n{'='*60}")
    print(f"VARAM-DYNAMICS SCAN — {ist_now()}")
    print(f"{'='*60}")

    # Dynamic asset list (all HL assets filtered by $1M volume)
    assets    = get_assets(force_refresh=True)
    symbols   = [a["symbol"] for a in assets]
    liq_map   = {a["symbol"]: a["liquidity_score"] for a in assets}
    print(f"Scanning {len(symbols)} assets...")

    all_ivs  = SCAN_INTERVALS + LOWER_TF_INTERVALS
    data     = fetch_all(symbols, all_ivs, delay_ms=60)
    all_sigs = []
    cur_px   = {}

    for sym in symbols:
        sym_data = data.get(sym, {})
        if not sym_data: continue
        for tf in ["1h","4h"]:
            df = sym_data.get(tf)
            if df is not None and len(df)>0:
                cur_px[sym] = float(df["close"].iloc[-1]); break

        tf_sigs = {}
        for tf in SCAN_INTERVALS:
            df = sym_data.get(tf)
            p  = _prepare(df)
            tf_sigs[tf] = get_latest_signal(p) if p is not None else None

        fresh = {tf:s for tf,s in tf_sigs.items() if s}
        if not fresh: continue

        for tf, sig in fresh.items():
            direction = sig["signal"]
            ltf_ok    = lower_tf_ok(sym, direction, data)
            sb = score_signal(
                symbol=sym, interval=tf, direction=direction,
                signal_data=sig, all_tf_signals=tf_sigs,
                liquidity_score=liq_map.get(sym, 0.5),
                lower_tf_ok=ltf_ok,
            )
            if sb.should_alert:
                all_sigs.append(sb)
                print(f"  ✅ {sym:<8} {tf:>4}  {direction:<6}  {sb.total_score:.1f}/10  {sb.risk_emoji}")
            else:
                print(f"  ➖ {sym:<8} {tf:>4}  {direction:<6}  {sb.total_score:.1f}/10")

    # Deduplicate: keep only the highest scoring signal per asset
    # (prevents DOGE 1h and DOGE 4h both alerting simultaneously)
    seen_assets = {}
    for sig in sorted(all_sigs, key=lambda x: x.total_score, reverse=True):
        if sig.symbol not in seen_assets:
            seen_assets[sig.symbol] = sig
    all_sigs = list(seen_assets.values())
    all_sigs.sort(key=lambda x: x.total_score, reverse=True)

    # ── Sprint C: split the score-ranked signals by reviewed asset class and
    # cap each category independently (ceiling, not quota — no padding).
    # Uncategorized symbols are EXCLUDED from alerts and reported for review. ──
    crypto_top, rwa_top, uncategorized = _split_by_category(all_sigs)
    if uncategorized:
        print(f"[scan] UNCATEGORIZED qualified symbols (excluded from alerts — "
              f"please classify in scanner/asset_classes.py): "
              f"{', '.join(sorted(set(uncategorized)))}")

    # Two INDEPENDENT Telegram messages: each gets its own message_id → its own
    # batches[mid] → independent Select/Skip/Confirm. An empty category sends
    # nothing. send_alert returns the message_id on a real send, or None when
    # nothing went out (no fresh signals OR all A2-suppressed).
    sent_any = False
    if crypto_top:
        sent_any |= tg.send_alert(crypto_top, ist_now(), ACCOUNT_SIZE,
                                  category="crypto") is not None
    if rwa_top:
        sent_any |= tg.send_alert(rwa_top, ist_now(), ACCOUNT_SIZE,
                                  category="rwa_perp") is not None

    # Sprint H1: one Hyperliquid no-alert message when the scan ran (had data)
    # but no real alert was sent — covers no-qualified, all-A2-suppressed, and
    # all-uncategorized. Skipped on a fetch outage (had_data False ≠ no signal).
    had_data = any(data.get(s) for s in symbols)
    if _hl_no_alert_due(sent_any, had_data):
        tg.send_message(HL_NO_ALERT_MSG)

    selected = crypto_top + rwa_top
    print(f"\nSent {len(crypto_top)} crypto + {len(rwa_top)} rwa/perp signals "
          f"to Telegram ({len(set(uncategorized))} uncategorized excluded).")
    for sb in selected:
        open_position(sb)

    if cur_px:
        closed = update_positions(cur_px)
        if closed:
            tg.send_message(_paper_update_message(closed))

    print(f"\nScan complete — {ist_now()}")


def run_monitor():
    """Update paper positions + process Telegram button taps."""
    positions = get_open_positions()
    symbols   = list({p["symbol"] for p in positions})

    if symbols:
        from scanner.fetcher import fetch_candles
        cur_px = {}
        for sym in symbols:
            df = fetch_candles(sym, "1h", lookback_bars=5)
            if df is not None and len(df)>0:
                cur_px[sym] = float(df["close"].iloc[-1])
        update_positions(cur_px)

        # Send hourly check-in on open manual trades
        from telegram.bot import _ld, send_checkin
        st = _ld()
        manual = st.get("trades", {})
        if manual:
            # Fetch prices for manual trades too
            for sym in manual:
                if sym not in cur_px:
                    df = fetch_candles(sym, "1h", lookback_bars=5)
                    if df is not None and len(df)>0:
                        cur_px[sym] = float(df["close"].iloc[-1])
            send_checkin(manual, cur_px)

    # Process any pending Telegram button taps — ONLY in polling mode.
    # In webhook mode the Vercel endpoint handles taps; calling getUpdates
    # here would 409-conflict with the active webhook. Default is polling,
    # so behaviour is unchanged when the env var is absent.
    callback_mode = (os.environ.get("TELEGRAM_CALLBACK_MODE") or "polling").strip().lower()
    if callback_mode == "polling":
        tg.process_callbacks(ACCOUNT_SIZE)
    else:
        print(f"[monitor] callback mode={callback_mode}: skipping polling (webhook handles taps)")
    print(f"[monitor] Done — {ist_now()}")


def run_status():
    positions = get_open_positions()
    print(f"\nOpen paper positions: {len(positions)}")
    for p in positions:
        print(f"  {p['symbol']:<8} {p['direction']:<6} {p['exit_style']:<13} "
              f"pnl={p.get('current_pnl_pct',0.0):+.2f}%")
    summary = get_performance_summary()
    if summary:
        print("\nPaper performance by exit style:")
        for style, s in summary.items():
            if style == "score_accuracy": continue
            print(f"  {style:<15}: {s['trades']} trades  "
                  f"win={s['win_rate']}%  avg={s['avg_pnl']:+.2f}%")


def run_setup():
    print("Testing Telegram...")
    ok = tg.send_message(
        "✅ <b>Varam-Dynamics</b> connected!\n\n"
        "I'll send Signal alerts.\n"
        "Tap the buttons to log your trades.\n\n"
        "Send /help to see all commands."
    )
    print("✅ Telegram working!" if ok else "❌ Failed — check secrets")


def main():
    p   = argparse.ArgumentParser(prog="varam-dynamics")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    sub.add_parser("monitor")
    sub.add_parser("status")
    sub.add_parser("setup")
    args = p.parse_args()
    {"scan": run_scan, "monitor": run_monitor,
     "status": run_status, "setup": run_setup}[args.cmd]()


if __name__ == "__main__":
    main()
