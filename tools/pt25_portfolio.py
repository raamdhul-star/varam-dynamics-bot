"""
tools/pt25_portfolio.py — twin $25 paper portfolios (Sprint P25)
================================================================
Two simulated $25 accounts that follow the bot's high-conviction (score >= 7.5)
TRAILING signals and post a WEEKLY Telegram report. Same strategy, same start,
only the leverage ceiling differs:

  🟢 CAPPED  — suggested leverage (7% / stop-distance), rounded, capped at 3x
  🔴 FULL    — same suggested leverage, capped only at the exchange max

CAPITAL MODEL (concurrent, not sequential)
------------------------------------------
The bot fires ~20 qualifying calls a day and they run AT THE SAME TIME. An
account therefore cannot roll its whole balance into trade #1 and then its whole
grown balance into trade #2 — trade #1 has not closed yet. So this simulates a
real margin account with an event loop:

  * a trade OPENS  -> post margin = MARGIN_FRAC x equity, taken from FREE cash
  * total posted margin can never exceed the equity (MAX_EXPOSURE)
  * if there is no free margin left, the call is SKIPPED (missed, not taken)
  * a trade CLOSES -> realise margin x leveraged-return, release the margin
  * a position can lose at most its own margin (-100% leg floor = liquidation)

Balances are replayed from inception on every run, so the result is fully
deterministic and idempotent (no double counting, no drifting state).

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

SCORE_MIN     = 7.5     # the strategy floor
START         = 25.0    # each account starts here
MARGIN_FRAC   = 0.10    # margin per trade = 10% of equity AT THE TIME IT OPENS
MAX_EXPOSURE  = 1.00    # total posted margin never exceeds 100% of equity
MIN_MARGIN    = 0.02    # need at least 2% of equity free, else the call is skipped
RISK_PCT      = 0.07    # 7% risk basis for suggested leverage
CAP_A         = 3       # 🟢 capped account ceiling
CAP_B         = 20      # 🔴 full account ceiling (practical exchange-max stand-in)

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


def simulate(trades: list, cap: int, start: float = START, cutoff: str | None = None) -> dict:
    """Replay `trades` through a margin account with CONCURRENT positions.

    Trades need open_time / exit_time / entry / stop / pnl. Events are strictly
    ordered by time; a close settles before an open at the same instant so the
    freed margin is available again. `cutoff` stops the clock: events after it
    are ignored, so a position open at the cutoff keeps its margin tied up and
    contributes no P&L yet. Pure — no I/O.
    """
    events = []
    for i, t in enumerate(trades):
        events.append((t["open_time"], 1, i))   # 1 = open  (sorts after close)
        events.append((t["exit_time"], 0, i))   # 0 = close (settles first)
    events.sort()

    equity, used = float(start), 0.0
    peak, max_dd = float(start), 0.0
    margins: dict[int, float] = {}
    taken = skipped = liq = 0
    closed: list[dict] = []
    max_open = 0

    for ts, kind, i in events:
        if cutoff is not None and ts > cutoff:
            continue
        t = trades[i]
        if kind == 1:                                    # open
            free = equity * MAX_EXPOSURE - used
            m = min(MARGIN_FRAC * equity, free)
            if m <= 0 or m < MIN_MARGIN * equity:
                skipped += 1
                continue
            margins[i] = m
            used += m
            taken += 1
            max_open = max(max_open, len(margins))
        else:                                            # close
            m = margins.pop(i, None)
            if m is None:                                # was skipped, never opened
                continue
            L = suggested_leverage(t["entry"], t["stop"], cap)
            r = _lev_return(L, t["pnl"])
            if r <= -100.0:
                liq += 1
            equity += m * r / 100.0
            used = max(0.0, used - m)
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100.0)
            closed.append({"symbol": t["symbol"], "pnl": t["pnl"], "lev": L,
                           "ret": r, "usd": m * r / 100.0, "exit_time": t["exit_time"]})

    return {"equity": equity, "open_margin": used, "still_open": len(margins),
            "taken": taken, "skipped": skipped, "liq": liq, "max_dd": max_dd,
            "max_open": max_open, "closed": closed}


def week_stats(sim_a: dict, sim_b: dict, since: str) -> dict:
    """Summarise the legs that CLOSED after `since` (the reporting window)."""
    ca = [c for c in sim_a["closed"] if c["exit_time"] > since]
    cb = [c for c in sim_b["closed"] if c["exit_time"] > since]
    n = len(ca)
    wins = sum(1 for c in ca if c["pnl"] > 0.05)
    best = max(ca, key=lambda c: c["pnl"]) if ca else None
    worst = min(ca, key=lambda c: c["pnl"]) if ca else None
    liq = sum(1 for c in cb if c["ret"] <= -100.0)
    return {"n": n, "wins": wins, "losses": n - wins,
            "avg_la": (sum(c["lev"] for c in ca) / n) if n else 0.0,
            "avg_lb": (sum(c["lev"] for c in cb) / len(cb)) if cb else 0.0,
            "best": best, "worst": worst, "liq_full": liq}


def build_intro() -> str:
    return ("👋 <b>Paper Portfolio tracker started</b>\n\n"
            "Two make-believe $25 accounts now follow the bot's high-conviction "
            "(7.5+) trailing signals:\n"
            "🟢 <b>Capped</b> — leverage up to 3×\n"
            "🔴 <b>Full</b> — the signal's full suggested leverage (up to exchange max)\n\n"
            "Each trade uses 10% of the account as margin and several run at once — "
            "when the account is fully committed the next call is skipped.\n\n"
            "You'll get one update a week. Simulation only — no real trades, no buttons.\n"
            f"{FOOTER}")


def build_report(capped: float, full: float, wk_a: float, wk_b: float,
                 since_a: float, since_b: float, stats: dict, week_label: str,
                 sim_a: dict | None = None, sim_b: dict | None = None) -> str:
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
    if sim_a and sim_b and stats["n"]:
        L.append(f"Capital: max {sim_a['max_open']}🟢/{sim_b['max_open']}🔴 positions at once · "
                 f"{sim_a['skipped']}🟢/{sim_b['skipped']}🔴 calls skipped (no free margin)")
        L.append(f"Max drawdown: 🟢 -{sim_a['max_dd']:.1f}%  ·  🔴 -{sim_b['max_dd']:.1f}%")
    L += ["", FOOTER]
    return "\n".join(L)


# ── trade source (read-only CSV) ─────────────────────────────────────────────

def load_closed_trades() -> list:
    """Read 7.5+ trailing CLOSED trades from the paper history. Read-only.
    Returns [{symbol, entry, stop, pnl, open_time, exit_time}] sorted by exit_time."""
    import csv
    if not os.path.exists(HISTORY):
        return []
    out = []
    for r in csv.DictReader(open(HISTORY)):
        if (r.get("status") != "closed" or r.get("exit_style") != "trailing"
                or r.get("pnl_pct") in ("", "None", None)
                or not r.get("exit_time") or not r.get("open_time")):
            continue
        try:
            if float(r.get("score") or 0) < SCORE_MIN:
                continue
            out.append({"symbol": r.get("symbol", "?"),
                        "entry": float(r["entry_price"]), "stop": float(r["sl_price"]),
                        "pnl": float(r["pnl_pct"]),
                        "open_time": r["open_time"], "exit_time": r["exit_time"]})
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
    """Replay both accounts from inception through the concurrent-capital model
    and (optionally) send the weekly message. Returns (messages, new_state, sent)."""
    first_run = state is None
    if first_run:
        # Start FORWARD: only calls OPENED from now on ever enter the accounts.
        state = {"inception": now.isoformat(), "capped": START, "full": START,
                 "last_processed": now.isoformat(), "trades_applied": 0,
                 "liq_full_total": 0}

    inception = state.get("inception") or now.isoformat()
    last = state.get("last_processed") or inception

    # Forward-only universe: calls opened at/after inception.
    elig = [t for t in trades if t["open_time"] >= inception]

    sim_a = simulate(elig, CAP_A)
    sim_b = simulate(elig, CAP_B)
    prev_a = simulate(elig, CAP_A, cutoff=last)["equity"]
    prev_b = simulate(elig, CAP_B, cutoff=last)["equity"]

    after_a, after_b = sim_a["equity"], sim_b["equity"]
    stats = week_stats(sim_a, sim_b, last)

    all_closed = sim_a["closed"] + sim_b["closed"]
    if all_closed:
        state["last_processed"] = max([c["exit_time"] for c in all_closed] + [last])
    state["capped"], state["full"] = round(after_a, 4), round(after_b, 4)
    state["trades_applied"] = len(sim_a["closed"])
    state["liq_full_total"] = sim_b["liq"]

    wk_a = (after_a / prev_a - 1) * 100 if prev_a else 0.0
    wk_b = (after_b / prev_b - 1) * 100 if prev_b else 0.0
    since_a = (after_a / START - 1) * 100
    since_b = (after_b / START - 1) * 100
    report = build_report(after_a, after_b, wk_a, wk_b, since_a, since_b,
                          stats, now.strftime("%d %b"), sim_a, sim_b)

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

def _tr(sym, e, s, pnl, o, x):
    return {"symbol": sym, "entry": e, "stop": s, "pnl": pnl,
            "open_time": o, "exit_time": x}


def _selftest() -> int:
    ok = []
    def chk(n, c): ok.append(bool(c)); print(f"  [{'PASS' if c else 'FAIL'}] {n}")
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)

    chk("suggested lev: tight stop capped at 3", suggested_leverage(100, 99, 3) == 3)
    chk("suggested lev: full uncapped >3", suggested_leverage(100, 99, 20) == 7)
    chk("suggested lev: wide stop -> 1x", suggested_leverage(100, 90, 3) == 1)
    chk("liquidation floor at -100", _lev_return(9, -45) == -100.0)

    # ── concurrency model ────────────────────────────────────────────────────
    # one trade: 10% margin x 3x x +6% = +1.8% of equity -> 25 -> 25.45
    one = [_tr("AAA", 100, 99, 6.0, "2026-08-20T00:00", "2026-08-20T01:00")]
    s = simulate(one, 3)
    chk("single trade sizes 10% margin", abs(s["equity"] - 25 * (1 + 0.10 * 18 / 100)) < 1e-9)
    chk("margin released on close", s["open_margin"] == 0 and s["still_open"] == 0)

    # 12 IDENTICAL overlapping trades: only 10 fit (10 x 10% margin = 100%)
    over = [_tr(f"S{i}", 100, 99, 6.0, "2026-08-20T00:00", "2026-08-20T05:00")
            for i in range(12)]
    so = simulate(over, 3)
    chk("concurrency cap: 10 taken, 2 skipped", so["taken"] == 10 and so["skipped"] == 2)
    chk("max concurrent positions tracked", so["max_open"] == 10)

    # the same 12 trades run one after another -> all taken, and compound
    seq = [_tr(f"S{i}", 100, 99, 6.0, f"2026-08-20T{i:02d}:00", f"2026-08-20T{i:02d}:30")
           for i in range(12)]
    ss = simulate(seq, 3)
    chk("sequential trades all taken (no capital clash)", ss["taken"] == 12 and ss["skipped"] == 0)
    chk("BUGFIX: concurrent result < sequential result", so["equity"] < ss["equity"])

    # a losing leg can only cost its own margin
    lose = [_tr("BAD", 100, 99, -50.0, "2026-08-20T00:00", "2026-08-20T01:00")]
    sl_ = simulate(lose, 20)
    chk("loss capped at the position's margin", abs(sl_["equity"] - 25 * 0.90) < 1e-9)
    chk("liquidation counted", sl_["liq"] == 1)
    chk("equity can never go negative", sl_["equity"] > 0)

    # cutoff: a position open at the cutoff contributes no P&L yet
    sc = simulate(one, 3, cutoff="2026-08-20T00:30")
    chk("cutoff holds P&L of a still-open leg", sc["equity"] == 25.0 and sc["still_open"] == 1)

    # capped vs full on a winner
    mix = [_tr("AAA", 100, 99, 6.0, "2026-08-20T00:00", "2026-08-20T01:00"),
           _tr("BBB", 100, 95, -4.0, "2026-08-20T02:00", "2026-08-20T03:00")]
    a, b = simulate(mix, CAP_A), simulate(mix, CAP_B)
    chk("full >= capped after a high-lev winner", b["equity"] >= a["equity"])
    chk("both accounts move off 25", a["equity"] != 25.0 and b["equity"] != 25.0)
    st = week_stats(a, b, "")
    chk("week stats counts + avg lev", st["n"] == 2 and st["wins"] == 1 and st["avg_lb"] > st["avg_la"])

    # ── run() plumbing ───────────────────────────────────────────────────────
    sent = []
    stub = lambda m, *a, **k: sent.append((m, a, k))
    old = [_tr("OLD", 100, 99, 6.0, "2026-08-10T00:00", "2026-08-10T01:00")]
    msgs, s1, n1 = run(False, now, old, None, sender=stub)
    chk("first run dry: sends nothing", n1 == 0 and len(sent) == 0)
    chk("first run builds intro + report", len(msgs) == 2 and "tracker started" in msgs[0])
    chk("forward-start: history before inception not backfilled",
        s1["capped"] == 25.0 and s1["full"] == 25.0)

    # a state whose inception precedes the trades -> they DO count
    st2 = {"inception": "2026-08-19T00:00", "capped": 25.0, "full": 25.0,
           "last_processed": "2026-08-19T00:00", "trades_applied": 0, "liq_full_total": 0}
    sent.clear()
    msgs2, s2, n2 = run(True, now, mix, dict(st2), sender=stub)
    chk("send path posts report (no intro on later run)", n2 == 1 and len(sent) == 1)
    chk("report sent with no markup/buttons", sent[0][1] == () and sent[0][2].get("markup") is None
        and "button" not in sent[0][0].lower())
    chk("trades applied once, balance moved", s2["trades_applied"] == 2 and s2["capped"] != 25.0)

    # idempotency: replay is deterministic, re-running never double-counts
    _m, s3, _n = run(False, now, mix, dict(st2), sender=stub)
    chk("idempotent: full replay gives same balance",
        s3["capped"] == s2["capped"] and s3["full"] == s2["full"])
    _m4, s4, _n4 = run(False, now, mix, dict(s2), sender=stub)
    chk("re-run on advanced state does not double-count", s4["capped"] == s2["capped"])

    chk("report has both accounts + footer",
        "Capped ≤3×" in msgs2[-1] and "Full (max)" in msgs2[-1] and FOOTER in msgs2[-1])
    chk("report shows capital/drawdown lines",
        "positions at once" in msgs2[-1] and "Max drawdown" in msgs2[-1])
    chk("no telegram markup/session code touched", not hasattr(sys.modules[__name__], "batches"))

    print(f"\nPT25 SELFTEST: {sum(ok)}/{len(ok)} passed")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    raise SystemExit(main(sys.argv))
