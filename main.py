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
from scanner.fetcher  import fetch_all, SCAN_INTERVALS, LOWER_TF_INTERVALS
from scanner.cpr_engine import attach_cpr, detect_signals, get_latest_signal
from scanner.scorer   import score_signal
from paper.tracker    import (open_position, update_positions,
                               get_open_positions, get_performance_summary)
import telegram.bot as tg

ACCOUNT_SIZE = float(os.environ.get("ACCOUNT_SIZE", "200"))


def ist_now():
    return (datetime.now(timezone.utc) + timedelta(hours=5,minutes=30))\
           .strftime("%d %b %Y, %I:%M %p IST")


# ── Friendly labels for the auto paper-trade update message (display only) ──
EXIT_STYLE_LABELS = {
    "fixed_pct":  "Fixed % Exit",
    "cpr_target": "CPR Target Exit",
    "trailing":   "Trailing Stop Exit",
}
EXIT_STYLE_MEANING = {
    "fixed_pct":  "this paper trade used the fixed +10%/-7% rule, not the trailing stop.",
    "cpr_target": "this paper trade aimed for the CPR-based target line (R1/S1).",
    "trailing":   "this paper trade trailed the stop to protect profit as price moved our way.",
}


def _format_close(c: dict) -> str:
    """Build a layman-friendly Telegram line for one closed paper trade.
    Display only — uses values already computed by the tracker."""
    style   = c.get("exit_style", "")
    label   = EXIT_STYLE_LABELS.get(style, style)
    meaning = EXIT_STYLE_MEANING.get(style, "")
    won     = c.get("result") == "win"
    emoji   = "✅" if won else "❌"
    verdict = "Win" if won else "Loss"
    lines = [
        f"{emoji} <b>{c.get('symbol','')} {str(c.get('direction','')).upper()}</b> — {verdict}",
        f"   Exit method: {label}",
        f"   Result: {c.get('pnl_pct',0.0):+.2f}%",
    ]
    entry, exitp = c.get("entry_price"), c.get("exit_price")
    if entry is not None and exitp is not None:
        lines.append(f"   Entry {entry:.6g} → Exit {exitp:.6g}")
    if meaning:
        lines.append(f"   Meaning: {meaning}")
    return "\n".join(lines)


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
    top = all_sigs[:5]
    print(f"\nSending top {len(top)} signals to Telegram...")

    tg.send_alert(top, ist_now(), ACCOUNT_SIZE)
    for sb in top:
        open_position(sb)

    if cur_px:
        closed = update_positions(cur_px)
        if closed:
            msgs = ["📋 <b>Paper Trade Update</b> (auto-simulated by the bot)\n"]
            for c in closed:
                msgs.append(_format_close(c))
            tg.send_message("\n\n".join(msgs))

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

    # Process any pending Telegram button taps
    tg.process_callbacks(ACCOUNT_SIZE)
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
        "I'll send CPR breakout alerts every 2 hours.\n"
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
