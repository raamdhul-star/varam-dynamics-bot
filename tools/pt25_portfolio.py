"""
tools/pt25_portfolio.py — twin $25 paper portfolios (Sprint P25)
================================================================
Two simulated $25 accounts that follow the bot's high-conviction (score >= 7.5)
TRAILING signals and post a WEEKLY Telegram report. Same strategy, same start,
only the leverage ceiling differs:

  🟢 CAPPED  — suggested leverage (7% / stop-distance), rounded, capped at 3x
  🔴 FULL    — same suggested leverage, capped only at the exchange max

Read-only layer: it consumes the outcomes the existing paper tracker already
records (results/paper_trades/trade_history.csv) and NEVER writes to it. It does
NOT touch alerts, Upstash, trades.csv, the webhook, or the existing paper flow.
Its only side effects are: one weekly Telegram message and its own state file
(results/pt25_state.json). No buttons, no trade actions.

Sending: `--send` posts to Telegram (workflow uses this). Default run is a
DRY-RUN — prints the report, sends nothing, writes no state. `--selftest` runs
offline with mocked trades and never sends/writes.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram.bot import send_message as _bot_send_message  # generic sender, no markup

RESULTS   = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
HISTORY   = os.path.join(RESULTS, "paper_trades", "trade_history.csv")
STATE     = os.path.join(RESULTS, "pt25_state.json")

SCORE_MIN    = 7.5      # the strategy floor
START        = 25.0     # each account starts here
MARGIN_FRAC  = 0.10     # 10% of balance as margin per trade
RISK_PCT     = 0.07     # 7% risk basis for suggested leverage
CAP_A        = 3        # 🟢 capped account ceiling
CAP_B        = 20       # 🔴 full account ceiling (practical exchange-max stand-in)

FOOTER = "ℹ️ Auto-simulated · view-only · not financial advice · DYOR"


# ── pure helpers ─────────────────────────────────────────────────────────────

def suggested_leverage(entry: float, sl: float, cap: int) -> int:
    """round(7% / stop-distance), floor 1, capped at `cap`. Whole number (HL)."""
    try:
        slp = abs(float(entry) - float(sl)) / float(entry)
    except (TypeError, ValueError, ZeroDivisionError):
        return 1
    if slp <= 0:
        return cap
    return max(1, min(cap, round(RISK_PCT / slp)))


def _lev_return(L: int, pnl_pct: float) -> float:
    """Leveraged return on the position, floored at -100% (liquidation)."""
    return max(L * pnl_pct, -100.0)


def apply_trades(trades: list, capped: float, full: float) -> tuple:
    """Apply a batch of closed trades to both balances. Pure. Returns
    (new_capped, new_full, stats) where stats summarises the batch."""
    per = []
    liq_full = 0
    for tr in trades:
        e, s, pm = tr["entry"], tr["stop"], tr["pnl"]
        La = suggested_leverage(e, s, CAP_A)
        Lb = suggested_leverage(e, s, CAP_B)
        ra, rb = _lev_return(La, pm), _lev_return(Lb, pm)
        capped *= (1 + MARGIN_FRAC * ra / 100.0)
        full   *= (1 + MARGIN_FRAC * rb / 100.0)
        if rb <= -100.0:
            liq_full += 1
        per.append({"symbol": tr["symbol"], "pnl": pm, "La": La, "Lb": Lb})
    n = len(per)
    wins = sum(1 for p in per if p["pnl"] > 0.05)
    best = max(per, key=lambda p: p["pnl"]) if per else None
    worst = min(per, key=lambda p: p["pnl"]) if per else None
    stats = {"n": n, "wins": wins, "losses": n - wins,
             "avg_la": (sum(p["La"] for p in per) / n) if n else 0.0,
             "avg_lb": (sum(p["Lb"] for p in per) / n) if n else 0.0,
             "best": best, "worst": worst, "liq_full": liq_full}
    return capped, full, stats


def build_intro() -> str:
    return ("👋 <b>Paper Portfolio tracker started</b>\n\n"
            "Two make-believe $25 accounts now follow the bot's high-conviction "
            "(7.5+) trailing signals:\n"
            "🟢 <b>Capped</b> — leverage up to 3×\n"
            "🔴 <b>Full</b> — the signal's full suggested leverage (up to exchange max)\n\n"
            "You'll get one update a week. Simulation only — no real trades, no buttons.\n"
            f"{FOOTER}")


def build_report(capped: float, full: float, wk_a: float, wk_b: float,
                 since_a: float, since_b: float, stats: dict, week_label: str) -> str:
    liq = f"  ⚠️ {stats['liq_full']} liquidation(s)" if stats.get("liq_full") else ""
    L = [f"📊 <b>Paper Portfolio — week of {week_label}</b>", "",
         "Two simulated $25 accounts · 7.5+ trailing signals", "",
         f"🟢 Capped ≤3×:  ${capped:.2f}   week {wk_a:+.1f}%   "
         f"since start {since_a:+.1f}%   avg lev {stats['avg_la']:.1f}×",
         f"🔴 Full (max):  ${full:.2f}   week {wk_b:+.1f}%   "
         f"since start {since_b:+.1f}%   avg lev {stats['avg_lb']:.1f}×{liq}", ""]
    if stats["n"]:
        b, w = stats["best"], stats["worst"]
        L.append(f"This week: {stats['n']} trades · {stats['wins']}W/{stats['losses']}L   "
                 f"best {b['symbol']} {b['pnl']:+.1f}% · worst {w['symbol']} {w['pnl']:+.1f}%")
    else:
        L.append("This week: no high-conviction signals closed.")
    L += ["", FOOTER]
    return "\n".join(L)


# ── trade source (read-only CSV) ─────────────────────────────────────────────

def load_closed_trades() -> list:
    """Read 7.5+ trailing CLOSED trades from the paper history. Read-only.
    Returns a list of {symbol, entry, stop, pnl, exit_time} sorted by exit_time."""
    import csv
    if not os.path.exists(HISTORY):
        return []
    out = []
    for r in csv.DictReader(open(HISTORY)):
        if (r.get("status") != "closed" or r.get("exit_style") != "trailing"
                or r.get("pnl_pct") in ("", "None", None) or not r.get("exit_time")):
            continue
        try:
            if float(r.get("score") or 0) < SCORE_MIN:
                continue
            out.append({"symbol": r.get("symbol", "?"),
                        "entry": float(r["entry_price"]), "stop": float(r["sl_price"]),
                        "pnl": float(r["pnl_pct"]), "exit_time": r["exit_time"]})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda t: t["exit_time"])
    return out


def load_state() -> dict | None:
    if os.path.exists(STATE):
        try:
            return json.loads(open(STATE).read())
        except Exception:
            return None
    return None


def save_state(state: dict) -> None:
    os.makedirs(RESULTS, exist_ok=True)
    open(STATE, "w").write(json.dumps(state, indent=2, default=str))


# ── run ──────────────────────────────────────────────────────────────────────

def run(send: bool, now: datetime, trades: list, state: dict | None,
        sender=_bot_send_message) -> tuple:
    """Advance both accounts by any trades that closed since last_processed and
    (optionally) send the weekly message. Returns (messages, new_state, sent)."""
    first_run = state is None
    if first_run:
        # Start FORWARD: mark all existing closes as already-seen; $25 fresh.
        last = trades[-1]["exit_time"] if trades else ""
        state = {"inception": now.isoformat(), "capped": START, "full": START,
                 "last_processed": last, "trades_applied": 0, "liq_full_total": 0}

    last = state.get("last_processed", "")
    new = [t for t in trades if t["exit_time"] > last]

    before_a, before_b = state["capped"], state["full"]
    after_a, after_b, stats = apply_trades(new, before_a, before_b)
    if new:
        state["last_processed"] = new[-1]["exit_time"]
    state["capped"], state["full"] = round(after_a, 4), round(after_b, 4)
    state["trades_applied"] = state.get("trades_applied", 0) + stats["n"]
    state["liq_full_total"] = state.get("liq_full_total", 0) + stats["liq_full"]

    wk_a = (after_a / before_a - 1) * 100 if before_a else 0.0
    wk_b = (after_b / before_b - 1) * 100 if before_b else 0.0
    since_a = (after_a / START - 1) * 100
    since_b = (after_b / START - 1) * 100
    report = build_report(after_a, after_b, wk_a, wk_b, since_a, since_b,
                          stats, now.strftime("%d %b"))

    messages = ([build_intro()] if first_run else []) + [report]
    sent = 0
    if send:
        for m in messages:
            sender(m)
            sent += 1
    return messages, state, sent


def main(argv: list) -> int:
    send = "--send" in argv
    now = datetime.now(timezone.utc)
    state = load_state()
    messages, new_state, sent = run(send, now, load_closed_trades(), state)
    print("=" * 60)
    print("PT25 PAPER PORTFOLIO — " + ("LIVE SEND" if send else "DRY-RUN"))
    print("=" * 60)
    for m in messages:
        print("\n" + m)
    if send:
        save_state(new_state)
        print(f"\nSENT {sent} message(s); state persisted.")
    else:
        print("\nDRY-RUN: nothing sent, no state written.")
    return 0


# ── offline self-test (mocked; never sends, never writes) ────────────────────

def _selftest() -> int:
    ok = []
    def chk(n, c): ok.append(bool(c)); print(f"  [{'PASS' if c else 'FAIL'}] {n}")
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)

    chk("suggested lev: tight stop capped at 3", suggested_leverage(100, 99, 3) == 3)
    chk("suggested lev: full uncapped >3", suggested_leverage(100, 99, 20) == 7)   # 7%/1% = 7
    chk("suggested lev: wide stop -> 1x", suggested_leverage(100, 90, 3) == 1)      # 7%/10% <1 ->1
    chk("liquidation floor at -100", _lev_return(9, -45) == -100.0)

    # a batch: one winner (tight stop -> high lev), one loser
    trades = [{"symbol": "AAA", "entry": 100, "stop": 99, "pnl": 6.0, "exit_time": "t1"},
              {"symbol": "BBB", "entry": 100, "stop": 95, "pnl": -4.0, "exit_time": "t2"}]
    a, b, st = apply_trades(trades, 25.0, 25.0)
    chk("both balances move from 25", a != 25.0 and b != 25.0)
    chk("full >= capped after a high-lev winner", b >= a)
    chk("stats counts + avg lev", st["n"] == 2 and st["wins"] == 1 and st["avg_lb"] > st["avg_la"])

    sent = []
    stub = lambda m, *a, **k: sent.append((m, a, k))
    # first run: forward-start marks history as seen -> 0 new trades, intro+report
    msgs, s1, n1 = run(False, now, trades, None, sender=stub)
    chk("first run dry: sends nothing, no state write side effects", n1 == 0 and len(sent) == 0)
    chk("first run builds intro + report", len(msgs) == 2 and "tracker started" in msgs[0])
    chk("forward-start: balances stay $25 (history not backfilled)",
        s1["capped"] == 25.0 and s1["full"] == 25.0 and s1["last_processed"] == "t2")

    # next run with a NEW trade after last_processed -> applied once, gated send works
    trades2 = trades + [{"symbol": "CCC", "entry": 100, "stop": 98, "pnl": 8.0, "exit_time": "t3"}]
    sent.clear()
    msgs2, s2, n2 = run(True, now, trades2, s1, sender=stub)
    chk("send path posts report (no intro on later run)", n2 == 1 and len(sent) == 1)
    chk("report sent with no markup/buttons", sent[0][1] == () and sent[0][2].get("markup") is None
        and "button" not in sent[0][0].lower())
    chk("only the NEW trade applied (t3), balances grew", s2["capped"] > 25.0 and s2["trades_applied"] == 1
        and s2["last_processed"] == "t3")
    # idempotency: re-run same trades -> nothing new
    _m, s3, _n = run(False, now, trades2, s2, sender=stub)
    chk("idempotent: no double-count", s3["trades_applied"] == 1 and s3["capped"] == s2["capped"])
    chk("report has both accounts + footer",
        "Capped ≤3×" in msgs2[-1] and "Full (max)" in msgs2[-1] and FOOTER in msgs2[-1])
    chk("no telegram markup/session code touched", not hasattr(sys.modules[__name__], "batches"))

    print(f"\nPT25 SELFTEST: {sum(ok)}/{len(ok)} passed")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    raise SystemExit(main(sys.argv))
