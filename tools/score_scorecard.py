"""
tools/score_scorecard.py — read-only live scorecard for high-conviction calls
=============================================================================
Reads the paper-trade history the scanner ALREADY records hourly and reports
how the high-score (>= SCORE_MIN) calls are performing, overall and per week.
This is the D-analysis "track the next month live" tool — no new infra needed:
the scanner appends every call (with its score + trailing outcome) to
results/paper_trades/trade_history.csv on each run, so this scorecard updates
itself as new calls close.

READ-ONLY: no network, no Telegram, no writes, no trades. Just prints a report.

Usage:
  python tools/score_scorecard.py            # >=8.0 calls, trailing exit
  python tools/score_scorecard.py 7.5        # custom score threshold
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import sys
from collections import defaultdict

HISTORY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "results", "paper_trades", "trade_history.csv")
EXIT_STYLE = "trailing"          # the only exit rule that was profitable in analysis


def _t(s: str):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def load(score_min: float, exit_style: str = EXIT_STYLE) -> list:
    if not os.path.exists(HISTORY):
        return []
    out = []
    for r in csv.DictReader(open(HISTORY)):
        if r.get("status") != "closed" or r.get("exit_style") != exit_style:
            continue
        if r.get("pnl_pct") in ("", "None", None):
            continue
        try:
            if float(r.get("score") or 0) < score_min:
                continue
            r["_pnl"] = float(r["pnl_pct"]); r["_ts"] = _t(r.get("open_time", ""))
        except (TypeError, ValueError):
            continue
        out.append(r)
    out.sort(key=lambda r: r["_ts"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    return out


def stats(pnls: list) -> dict:
    n = len(pnls)
    if not n:
        return {"n": 0}
    wins = [x for x in pnls if x > 0.05]
    losses = [x for x in pnls if x < -0.05]
    return {"n": n, "winrate": 100 * len(wins) / n, "avg": sum(pnls) / n,
            "sum": sum(pnls),
            "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
            "avg_loss": (sum(losses) / len(losses)) if losses else 0.0}


def report(score_min: float = 8.0) -> str:
    rows = load(score_min)
    L = [f"{'=' * 60}",
         f"HIGH-CONVICTION SCORECARD — score >= {score_min}, {EXIT_STYLE} exit",
         f"(read-only view of results/paper_trades/trade_history.csv)",
         f"{'=' * 60}"]
    if not rows:
        L.append("No closed high-score calls recorded yet.")
        return "\n".join(L)

    allp = [r["_pnl"] for r in rows]
    s = stats(allp)
    first, last = rows[0]["_ts"], rows[-1]["_ts"]
    L.append(f"Period: {first.date()} -> {last.date()}   calls: {s['n']}")
    L.append(f"Win rate: {s['winrate']:.0f}%   avg/call: {s['avg']:+.2f}%   "
             f"avg win {s['avg_win']:+.1f}% / avg loss {s['avg_loss']:+.1f}%")
    # simple equity (10% margin/trade, 2x) as a running feel
    eq = 1.0
    for p in allp:
        eq *= (1 + 0.10 * 2 * p / 100.0)
    L.append(f"Illustrative equity (10% margin, 2x, compounding): x{eq:.2f} "
             f"({(eq - 1) * 100:+.0f}%)  — idealized, pre-slippage")

    # last 7 days
    if last:
        cut = last - dt.timedelta(days=7)
        recent = [r["_pnl"] for r in rows if r["_ts"] and r["_ts"] >= cut]
        rs = stats(recent)
        if rs["n"]:
            L.append(f"\nLast 7 days: {rs['n']} calls  win {rs['winrate']:.0f}%  "
                     f"avg {rs['avg']:+.2f}%  sum {rs['sum']:+.1f}%")

    # per ISO week
    wk = defaultdict(list)
    for r in rows:
        if r["_ts"]:
            wk[r["_ts"].isocalendar()[:2]].append(r["_pnl"])
    L.append("\nBy week:")
    for k in sorted(wk):
        v = wk[k]; w = sum(1 for x in v if x > 0.05)
        flag = "＋" if sum(v) > 0 else "－"
        L.append(f"  {k[0]}-W{k[1]:02d}  n={len(v):<3} win={100 * w / len(v):3.0f}%  "
                 f"avg={sum(v) / len(v):+6.2f}%  sum={sum(v):+7.1f}%  {flag}")
    L.append("\nNote: paper-simulated (idealized fills, no fees/slippage/funding). "
             "Use as a signal-quality tracker, not a P&L statement.")
    return "\n".join(L)


if __name__ == "__main__":
    thr = 8.0
    if len(sys.argv) > 1:
        try:
            thr = float(sys.argv[1])
        except ValueError:
            pass
    print(report(thr))
