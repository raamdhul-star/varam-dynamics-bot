"""
paper/tracker.py
================
Automatically paper trades every signal the scanner generates.
Tracks 3 parallel exit styles simultaneously:

  Style A: Fixed %    — exit when position P&L reaches +10%
  Style B: CPR Target — exit when price hits R1/S1 (green line)
  Style C: Trailing   — move SL to entry at +5%, trail by 2%

This runs every hour to update open positions with current price.
Results saved to results/paper_trades/ as CSV.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results" / "paper_trades"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE   = RESULTS_DIR / "open_positions.json"
HISTORY_FILE = RESULTS_DIR / "trade_history.csv"

EXIT_STYLES = ["fixed_pct", "cpr_target", "trailing"]
FIXED_EXIT_PCT  = 0.10    # exit at +10%
FIXED_STOP_PCT  = 0.07    # stop at -7%
TRAIL_TRIGGER   = 0.05    # start trailing after +5%
TRAIL_STEP      = 0.02    # trail by 2%


def _load_state() -> list[dict]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return []


def _save_state(positions: list[dict]) -> None:
    STATE_FILE.write_text(json.dumps(positions, indent=2, default=str))


def _append_history(trade: dict) -> None:
    row = pd.DataFrame([trade])
    if HISTORY_FILE.exists():
        existing = pd.read_csv(HISTORY_FILE)
        pd.concat([existing, row], ignore_index=True).to_csv(HISTORY_FILE, index=False)
    else:
        row.to_csv(HISTORY_FILE, index=False)


def open_position(signal_score) -> None:
    """
    Open 3 parallel paper positions for a new signal.
    signal_score: ScoreBreakdown object from scorer.py
    """
    positions = _load_state()
    now = datetime.now(timezone.utc).isoformat()

    for style in EXIT_STYLES:
        pos = {
            "id":          f"{signal_score.symbol}_{signal_score.interval}_{style}_{now[:10]}",
            "symbol":      signal_score.symbol,
            "interval":    signal_score.interval,
            "direction":   signal_score.direction,
            "exit_style":  style,
            "entry_price": signal_score.entry_price,
            "sl_price":    signal_score.sl_price,
            "tp_price":    signal_score.tp_price,
            "current_sl":  signal_score.sl_price,   # tracks trailing
            "score":       signal_score.total_score,
            "risk_label":  signal_score.risk_label,
            "open_time":   now,
            "status":      "open",
            "peak_pnl_pct": 0.0,
        }
        positions.append(pos)

    _save_state(positions)
    print(f"[tracker] Opened 3 paper positions for {signal_score.symbol} "
          f"{signal_score.direction} @ {signal_score.entry_price}")


def update_positions(current_prices: dict[str, float]) -> list[dict]:
    """
    Update all open positions with current prices.
    Check exit conditions for each style.
    Returns list of newly closed positions.

    current_prices: {symbol: current_price}
    """
    positions = _load_state()
    closed = []
    still_open = []

    for pos in positions:
        if pos["status"] != "open":
            still_open.append(pos)
            continue

        sym   = pos["symbol"]
        price = current_prices.get(sym)
        if price is None:
            still_open.append(pos)
            continue

        entry     = pos["entry_price"]
        direction = pos["direction"]
        style     = pos["exit_style"]
        current_sl = pos["current_sl"]

        # P&L calculation
        if direction == "long":
            pnl_pct = (price - entry) / entry
        else:
            pnl_pct = (entry - price) / entry

        pos["peak_pnl_pct"] = max(pos.get("peak_pnl_pct", 0), pnl_pct)

        exit_reason = None
        exit_price  = None

        # ── Style A: Fixed % ──────────────────────────────────────────
        if style == "fixed_pct":
            if pnl_pct >= FIXED_EXIT_PCT:
                exit_reason = f"target_hit (+{FIXED_EXIT_PCT*100:.0f}%)"
                exit_price  = price
            elif pnl_pct <= -FIXED_STOP_PCT:
                exit_reason = f"stop_hit (-{FIXED_STOP_PCT*100:.0f}%)"
                exit_price  = price
            # SL hit
            elif direction == "long" and price <= current_sl:
                exit_reason = "sl_hit"
                exit_price  = current_sl
            elif direction == "short" and price >= current_sl:
                exit_reason = "sl_hit"
                exit_price  = current_sl

        # ── Style B: CPR Target ───────────────────────────────────────
        elif style == "cpr_target":
            tp = pos["tp_price"]
            if direction == "long":
                if price >= tp:
                    exit_reason = "tp_r1_hit"
                    exit_price  = tp
                elif price <= current_sl:
                    exit_reason = "sl_hit"
                    exit_price  = current_sl
            else:
                if price <= tp:
                    exit_reason = "tp_s1_hit"
                    exit_price  = tp
                elif price >= current_sl:
                    exit_reason = "sl_hit"
                    exit_price  = current_sl

        # ── Style C: Trailing Stop ────────────────────────────────────
        elif style == "trailing":
            # Move SL to entry (BE) after +5%
            if pnl_pct >= TRAIL_TRIGGER and current_sl == pos["sl_price"]:
                new_sl = entry
                pos["current_sl"] = new_sl
                current_sl = new_sl

            # Trail: keep SL at (peak_price - trail_step%)
            if pnl_pct >= TRAIL_TRIGGER:
                if direction == "long":
                    trail_sl = price * (1 - TRAIL_STEP)
                    if trail_sl > pos["current_sl"]:
                        pos["current_sl"] = trail_sl
                        current_sl = trail_sl
                else:
                    trail_sl = price * (1 + TRAIL_STEP)
                    if trail_sl < pos["current_sl"]:
                        pos["current_sl"] = trail_sl
                        current_sl = trail_sl

            # Check SL hit
            if direction == "long" and price <= current_sl:
                exit_reason = "trailing_sl"
                exit_price  = current_sl
            elif direction == "short" and price >= current_sl:
                exit_reason = "trailing_sl"
                exit_price  = current_sl

        # ── Process exit ─────────────────────────────────────────────
        if exit_reason:
            if direction == "long":
                actual_pnl = (exit_price - entry) / entry
            else:
                actual_pnl = (entry - exit_price) / entry

            closed_pos = {**pos,
                "status":      "closed",
                "exit_price":  exit_price,
                "exit_time":   datetime.now(timezone.utc).isoformat(),
                "exit_reason": exit_reason,
                "pnl_pct":     round(actual_pnl * 100, 3),
                "result":      "win" if actual_pnl > 0 else "loss",
            }
            _append_history(closed_pos)
            closed.append(closed_pos)
            print(f"[tracker] CLOSED {sym} {direction} {style}: "
                  f"{exit_reason}  P&L={actual_pnl*100:+.2f}%")
        else:
            pos["current_price"] = price
            pos["current_pnl_pct"] = round(pnl_pct * 100, 3)
            still_open.append(pos)

    _save_state(still_open)
    return closed


def get_open_positions() -> list[dict]:
    return [p for p in _load_state() if p.get("status") == "open"]


def get_performance_summary() -> dict:
    """Summarise paper trade history by exit style."""
    if not HISTORY_FILE.exists():
        return {}

    df = pd.read_csv(HISTORY_FILE)
    if df.empty:
        return {}

    summary = {}
    for style in EXIT_STYLES:
        s = df[df["exit_style"] == style]
        if s.empty:
            continue
        wins = s[s["result"] == "win"]
        summary[style] = {
            "trades":   len(s),
            "wins":     len(wins),
            "losses":   len(s) - len(wins),
            "win_rate": round(len(wins) / len(s) * 100, 1),
            "avg_pnl":  round(s["pnl_pct"].mean(), 2),
            "best":     round(s["pnl_pct"].max(), 2),
            "worst":    round(s["pnl_pct"].min(), 2),
        }

    # Score accuracy: do higher scores win more?
    df["score_bucket"] = pd.cut(df["score"],
                                bins=[0, 5, 6, 7, 8, 10],
                                labels=["<5", "5-6", "6-7", "7-8", "8-10"])
    score_acc = df.groupby("score_bucket").apply(
        lambda x: round((x["result"] == "win").mean() * 100, 1)
    ).to_dict()
    summary["score_accuracy"] = score_acc

    return summary


if __name__ == "__main__":
    print("Paper tracker — state file:", STATE_FILE)
    print("History file :", HISTORY_FILE)
    positions = get_open_positions()
    print(f"Open positions: {len(positions)}")
    summary = get_performance_summary()
    if summary:
        print("\nPerformance by exit style:")
        for style, stats in summary.items():
            if style != "score_accuracy":
                print(f"  {style}: {stats}")
    else:
        print("No trade history yet.")
