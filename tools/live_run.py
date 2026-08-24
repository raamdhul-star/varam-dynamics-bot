"""
tools/live_run.py — run one live pass (DRY-RUN ONLY in L2) + the test suite.
============================================================================
L2 has no order-sending code at all: `get_backend("mainnet")` raises, so there
is no path — buggy or otherwise — that reaches the exchange with an order.

  python tools/live_run.py --selftest   # offline, no network, no writes
  python tools/live_run.py              # dry-run pass against the real read API
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live import config as C
from live import gates, state
from live.backends import BackendError, DryRunBackend, FakeBackend, get_backend, settle_bracket
from live.orders import build_bracket, build_stop, bracket_is_complete, round_px, round_sz
from live.reconcile import (ADOPTED, CLOSED_AWAY, EXPIRED, FAIL_CLOSED, HEALTHY,
                            NO_STOP, PROCEED, STILL_PENDING, reconcile)
from live.runner import run_once
from live.sizing import plan_order
from live.trailing import next_stop, plan_stop_move
from tools.live_preflight import recent_calls


def main() -> int:
    print("=" * 72)
    print("LIVE RUN — DRY-RUN.  L2 contains no order-sending code.")
    print("=" * 72)
    mode = C.mode()
    print(f"mode: {mode}   mainnet gate: {C.MAINNET_ENABLED}   "
          f"kill switch: {'ENGAGED' if C.kill_switch_on() else 'off'}")
    if not C.account_address():
        print("\nHL_ACCOUNT_ADDRESS not set — set your PUBLIC address to run a pass.")
        return 0
    sigs = recent_calls()
    res = run_once(signals=sigs, mode=mode, backend=get_backend(mode))
    if res["halted"]:
        print(f"\nHALTED: {res['halted']}")
        return 1
    print(f"\nwould place {len(res['placed'])}:")
    for p in res["placed"]:
        print(f"   {p['symbol']:<9} {p['leverage']}x  size {p['size']}  "
              f"entry {p['entry']}  stop {p['stop']}  margin ${p['margin']}")
    print(f"stop moves: {len(res['stop_moves'])}")
    for m in res["stop_moves"]:
        print(f"   {m['symbol']:<9} {m['from']} -> {m['to']}  ({m['reason']})")
    if res["attention"]:
        print(f"\n** NEEDS ATTENTION: {', '.join(res['attention'])} **")
    counts = {}
    for s in res["skipped"]:
        counts[s["reason"]] = counts.get(s["reason"], 0) + 1
    if counts:
        print("skipped:")
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"   {v:>3}x {k}")
    print("\nDRY-RUN: nothing was sent.")
    return 0


def _selftest() -> int:
    ok = []
    def chk(n, c): ok.append(bool(c)); print(f"  [{'PASS' if c else 'FAIL'}] {n}")
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    tmp = tempfile.mkdtemp(prefix="l2test_")

    try:
        # ── no mainnet path exists ──────────────────────────────────────────
        try:
            get_backend("mainnet"); chk("mainnet backend refuses to exist", False)
        except BackendError:
            chk("mainnet backend refuses to exist (no order path in L2)", True)
        chk("dryrun backend declares it places nothing",
            get_backend("dryrun").places_orders is False)

        # ── trailing: the risky piece ───────────────────────────────────────
        chk("no move before the trigger",
            next_stop(entry=100, price=102, direction="long", current_stop=98) is None)
        # Matches paper/tracker.py: move to breakeven, then immediately apply the
        # trail and keep whichever is tighter. At +3% trigger / 2% trail the trail
        # always wins (1.03 x 0.98 = 1.0094), so the first move locks in a small
        # PROFIT rather than merely breakeven.
        be = next_stop(entry=100, price=103.5, direction="long", current_stop=98)
        chk("first move takes the tighter of breakeven and trail",
            abs(be - 103.5 * 0.98) < 1e-9)
        chk("first move locks in profit, never below entry", be > 100)
        tr = next_stop(entry=100, price=120, direction="long", current_stop=100,
                       moved_to_breakeven=True)
        chk("then trails 2% behind price", abs(tr - 117.6) < 1e-9)
        chk("NEVER loosens a stop (price fell back)",
            next_stop(entry=100, price=105, direction="long", current_stop=117.6,
                      moved_to_breakeven=True) is None)
        chk("short trails downward",
            abs(next_stop(entry=100, price=80, direction="short", current_stop=100,
                          moved_to_breakeven=True) - 81.6) < 1e-9)
        chk("short never loosens",
            next_stop(entry=100, price=95, direction="short", current_stop=81.6,
                      moved_to_breakeven=True) is None)
        chk("refuses a stop already through the price",
            next_stop(entry=100, price=100.5, direction="long", current_stop=99,
                      moved_to_breakeven=True) is None)
        chk("garbage input never produces a stop",
            next_stop(entry=0, price=10, direction="long", current_stop=1) is None
            and next_stop(entry=100, price=120, direction="sideways",
                          current_stop=100) is None)

        mv = plan_stop_move(symbol="X", entry=100, price=120, direction="long",
                            current_stop=100, old_order_id="oid1",
                            moved_to_breakeven=True)
        chk("stop move PLACES before it CANCELS",
            [s[0] for s in mv.steps()] == ["place_stop", "cancel_order"])

        # ── order payloads ──────────────────────────────────────────────────
        chk("size floors, never rounds up", round_sz(1.999, 0) == 1.0)
        chk("price obeys 5 significant figures", round_px(123456.789, 2) == 123460.0)
        chk("price obeys the decimal-place limit", round_px(1.23456789, 5) == 1.2)
        p = plan_order(symbol="ZEC", direction="long", entry=40.0, stop=39.2,
                       equity=25.0, used_margin=0.0, sz_decimals=2,
                       asset_max_leverage=10)
        b = build_bracket(p, 2)
        chk("bracket has an entry and a stop", b and len(b["orders"]) == 2)
        chk("entry buys for a long, stop sells", b["orders"][0]["is_buy"] is True
            and b["orders"][1]["is_buy"] is False)
        chk("stop is reduce-only (can never open a new position)",
            b["orders"][1]["reduce_only"] is True)
        chk("entry is NOT reduce-only", b["orders"][0]["reduce_only"] is False)
        chk("entry is IOC, so a thin book cannot fill us far away",
            b["orders"][0]["tif"] == "Ioc")
        chk("long stop sits below entry", b["orders"][1]["trigger_px"] < p.entry)
        sh = build_stop("X", "short", 1.0, 105.0, 2)
        chk("short stop buys to close and sits above entry", sh["is_buy"] is True)
        chk("unusable plan yields no half-built bracket",
            build_bracket(plan_order(symbol="X", direction="long", entry=100,
                                     stop=102, equity=25, used_margin=0,
                                     sz_decimals=2, asset_max_leverage=10), 2) is None)
        chk("bracket is incomplete without a stop id",
            not bracket_is_complete({"entry_ok": True, "stop_order_id": None}))

        # ── the naked-position recovery path ────────────────────────────────
        fb = FakeBackend(fail_stop=True)
        okb, status, _ = settle_bracket(fb, b, fb.place_bracket(b))
        chk("entry filled but stop missing -> FLATTEN immediately",
            not okb and status == "flattened_no_stop" and fb.flattened == ["ZEC"])
        fb2 = FakeBackend(fail_entry=True)
        okb2, st2, _ = settle_bracket(fb2, b, fb2.place_bracket(b))
        chk("rejected entry is not treated as a position",
            not okb2 and st2 == "entry_rejected" and fb2.flattened == [])

        # ── reconciliation ──────────────────────────────────────────────────
        R = lambda **kw: reconcile(symbol="X", **kw).outcome
        chk("flat both sides -> proceed",
            R(local=None, exch_position=None, has_resting_stop=False) == PROCEED)
        chk("untracked exchange position -> adopt and block",
            R(local=None, exch_position={"size": 1}, has_resting_stop=True) == ADOPTED)
        chk("position with NO stop -> needs attention",
            R(local={"status": "open"}, exch_position={"size": 1},
              has_resting_stop=False) == NO_STOP)
        chk("position with a stop -> healthy",
            R(local={"status": "open"}, exch_position={"size": 1},
              has_resting_stop=True) == HEALTHY)
        chk("our record but exchange flat -> closed while away",
            R(local={"status": "open"}, exch_position=None,
              has_resting_stop=False) == CLOSED_AWAY)
        chk("unfilled entry under the 2h limit -> still pending",
            R(local={"status": "pending"}, exch_position=None, has_resting_stop=False,
              pending_age_hours=1.9) == STILL_PENDING)
        chk("unfilled entry past the 2h limit -> cancel it and free the margin",
            R(local={"status": "pending"}, exch_position=None, has_resting_stop=False,
              pending_age_hours=2.1) == EXPIRED)
        chk("the pending timeout is 2h, not the runbook's 8h",
            C.PENDING_TIMEOUT_HOURS == 2.0)
        # Entries are IOC, so nothing can rest in the first place -- this path is
        # defensive only until we ever use a resting limit entry.
        chk("entries are IOC, so no entry can sit unfilled today",
            build_bracket(p, 2)["orders"][0]["tif"] == "Ioc")
        chk("FAILED READ clears nothing and blocks",
            R(local={"status": "open"}, exch_position=None, has_resting_stop=False,
              read_ok=False) == FAIL_CLOSED)
        chk("every blocking outcome really blocks",
            all(reconcile(symbol="X", local={"status": "open"},
                          exch_position={"size": 1}, has_resting_stop=s
                          ).blocks_new_entry for s in (True, False)))

        # ── state is scoped per network ─────────────────────────────────────
        state.save_positions("dryrun", {"AAA": {"status": "open"}}, tmp)
        state.save_positions("mainnet", {"BBB": {"status": "open"}}, tmp)
        chk("dryrun state cannot see or clear mainnet state",
            list(state.load_positions("dryrun", tmp)) == ["AAA"]
            and list(state.load_positions("mainnet", tmp)) == ["BBB"])
        chk("unknown mode is forced into the dryrun directory",
            state.base_dir("../../etc", tmp).endswith(os.path.join("live", "dryrun")))
        chk("unreadable state returns empty, never crashes",
            state.load_positions("testnet", tmp) == {})

        # ── run-level gates ─────────────────────────────────────────────────
        chk("equity below the floor halts the run",
            gates.all_clear(mode="dryrun", equity=10.0)[0] is False)
        chk("healthy equity passes", gates.all_clear(mode="dryrun", equity=25.0)[0])
        os.environ["LIVE_KILL_SWITCH"] = "1"
        chk("kill switch halts the run", gates.all_clear(mode="dryrun", equity=25)[1]
            == "kill_switch_engaged")
        os.environ.pop("LIVE_KILL_SWITCH")
        chk("dryrun is never allowed to send orders",
            gates.mode_allows_orders("dryrun") is False)
        chk("mainnet is not allowed to send orders while the code gate is False",
            gates.mode_allows_orders("mainnet") is False)

        # ── whole pass, offline ─────────────────────────────────────────────
        META = {"ZEC": {"sz_decimals": 2, "max_leverage": 10},
                "ETH": {"sz_decimals": 4, "max_leverage": 25}}
        def reader(eq=25.0, positions=(), stops=None):
            return (META, {"equity": eq, "margin_used": 0.0, "withdrawable": eq,
                           "positions": list(positions),
                           "resting_stops": stops or {}},
                    {"ZEC": 40.0, "ETH": 3000.0})
        sigs = [{"symbol": "ZEC", "direction": "long", "entry": 40.0, "sl": 39.2,
                 "score": 8.1, "interval": "1h", "bar_time": "t1"},
                {"symbol": "ETH", "direction": "short", "entry": 3000.0, "sl": 3060.0,
                 "score": 7.9, "interval": "1h", "bar_time": "t1"}]
        bk = DryRunBackend()
        r = run_once(signals=sigs, mode="dryrun", backend=bk, reader=reader,
                     now=now, root=tmp)
        chk("a clean pass plans both trades", len(r["placed"]) == 2 and not r["halted"])
        chk("nothing was actually sent (dry-run backend)",
            all(k == "bracket" for k, _ in bk.sent) and bk.places_orders is False)

        # Duplicate suppression: once those positions really exist on the
        # exchange, a second run must not stack on top of them.
        held = [{"symbol": "ZEC", "size": 0.3, "notional": 12.0},
                {"symbol": "ETH", "size": 0.004, "notional": 12.0}]
        bk2 = DryRunBackend()
        r2 = run_once(signals=sigs, mode="dryrun", backend=bk2,
                      reader=lambda: reader(25.0, held,
                                            {"ZEC": "s1", "ETH": "s2"}),
                      now=now, root=tmp)
        # Two independent gates block this: the same candle's fingerprint, and
        # the symbol already being live. The cheaper duplicate gate fires first.
        chk("same signals on a second run are not re-entered",
            len(r2["placed"]) == 0 and len(r2["skipped"]) == 2
            and all(x["reason"] in ("duplicate_signal", "symbol_already_live")
                    for x in r2["skipped"]))
        # and a DIFFERENT candle on a symbol we already hold is still blocked
        newbar = [dict(sigs[0], bar_time="t2")]
        r2c = run_once(signals=newbar, mode="dryrun", backend=DryRunBackend(),
                       reader=lambda: reader(25.0, held, {"ZEC": "s1", "ETH": "s2"}),
                       now=now, root=tmp)
        chk("a new candle on a symbol we already hold is still blocked",
            not r2c["placed"]
            and r2c["skipped"][0]["reason"] == "symbol_already_live")
        # And the documented dry-run behaviour: nothing was really placed, so the
        # exchange is flat and our stale records are correctly cleared. The
        # exchange is always the truth, even when that erases our own notes.
        bk2b = DryRunBackend()
        run_once(signals=[], mode="dryrun", backend=bk2b, reader=reader,
                 now=now, root=tmp)
        chk("dry-run records are cleared when the exchange shows flat",
            state.load_positions("dryrun", tmp) == {})

        # a failed exchange read must stop everything
        def bad_reader():
            from live.hl_info import HLReadError
            raise HLReadError("simulated outage")
        r3 = run_once(signals=sigs, mode="dryrun", backend=DryRunBackend(),
                      reader=bad_reader, now=now, root=tmp)
        chk("a failed exchange read halts the run and places nothing",
            r3["halted"] and not r3["placed"])

        # trailing inside a real pass: price up 20% -> stop moves, place-then-cancel
        shutil.rmtree(os.path.join(tmp, "live"), ignore_errors=True)
        state.save_positions("dryrun", {"ZEC": state.record(
            "ZEC", direction="long", size=0.3, entry=40.0, stop=39.2, leverage=3,
            stop_order_id="old1", fingerprint="ZEC|long|1h|t0", now=now)}, tmp)
        bk3 = DryRunBackend()
        r4 = run_once(signals=[], mode="dryrun", backend=bk3,
                      reader=lambda: (META, {"equity": 25.0, "margin_used": 4.0,
                                             "withdrawable": 21.0,
                                             "positions": [{"symbol": "ZEC", "size": 0.3,
                                                            "notional": 14.4}],
                                             "resting_stops": {"ZEC": "old1"}},
                                      {"ZEC": 48.0}),
                      now=now, root=tmp)
        chk("held position gets its stop moved up", len(r4["stop_moves"]) == 1
            and r4["stop_moves"][0]["to"] > 39.2)
        chk("stop move placed the new order BEFORE cancelling the old",
            [k for k, _ in bk3.sent] == ["place_stop", "cancel_order"])

        # if placing the new stop fails, the OLD one must survive
        bk4 = FakeBackend(fail_place_stop=True)
        r5 = run_once(signals=[], mode="dryrun", backend=bk4,
                      reader=lambda: (META, {"equity": 25.0, "margin_used": 4.0,
                                             "withdrawable": 21.0,
                                             "positions": [{"symbol": "ZEC", "size": 0.3,
                                                            "notional": 14.4}],
                                             "resting_stops": {"ZEC": "old1"}},
                                      {"ZEC": 60.0}),
                      now=now, root=tmp)
        chk("failed stop placement cancels NOTHING (old stop still protects)",
            not r5["stop_moves"]
            and not any(k == "cancel_order" for k, _ in bk4.sent))

        # an untracked exchange position must block that symbol
        shutil.rmtree(os.path.join(tmp, "live"), ignore_errors=True)
        r6 = run_once(signals=sigs, mode="dryrun", backend=DryRunBackend(),
                      reader=lambda: reader(25.0, [{"symbol": "ZEC", "size": 0.5,
                                                    "notional": 20.0}],
                                            {"ZEC": "x"}),
                      now=now, root=tmp)
        chk("untracked position blocks new entries on that symbol",
            not any(p["symbol"] == "ZEC" for p in r6["placed"])
            and "ZEC" in r6["attention"])

        # ── still no order-sending code anywhere in live/ ────────────────────
        import live
        live_dir = os.path.dirname(os.path.abspath(live.__file__))
        src = "".join(open(os.path.join(live_dir, f)).read()
                      for f in os.listdir(live_dir) if f.endswith(".py"))
        chk("live/ still references only the read endpoint",
            ("/" + "exchange") not in src and src.count("https://") == 1)
        chk("live/ still has no private-key variable", ("PRIVATE" + "_KEY") not in src)
        chk("live/ still imports no signing library",
            not any(x in src for x in ("eth_account", "hyperliquid.exchange")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nL2 SELFTEST: {sum(ok)}/{len(ok)} passed")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    raise SystemExit(main())
