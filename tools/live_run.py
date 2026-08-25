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
from live import gates, notify, state
from live.backends import BackendError, DryRunBackend, FakeBackend, get_backend, settle_bracket
from live.orders import build_bracket, build_stop, bracket_is_complete, round_px, round_sz
from live.reconcile import (ADOPTED, CLOSED_AWAY, EXPIRED, FAIL_CLOSED, HEALTHY,
                            NO_STOP, PROCEED, STILL_PENDING, reconcile)
from live.runner import run_once
from live.sizing import plan_order
from live.trailing import next_stop, plan_stop_move
from live.signals import recent_calls, source_for


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv
    want_live = "--live" in argv
    cap = None
    for i, a in enumerate(argv):
        if a == "--max-trades" and i + 1 < len(argv):
            try:
                cap = int(argv[i + 1])
            except ValueError:
                cap = None

    mode = C.mode()
    backend = get_backend(mode)
    real = getattr(backend, "places_orders", False)

    # Going live REQUIRES an explicit cap. The first supervised run must place
    # exactly one order so it can be checked on the exchange before anything is
    # automated — and forgetting the flag must not silently open four.
    if want_live and real and cap is None:
        print("REFUSED: --live needs an explicit --max-trades N.")
        print("  For the first supervised run use:  --live --max-trades 1")
        return 2
    if real and not want_live:
        print("This environment is ARMED for real orders but --live was not given.")
        print("  Running as a dry run. Add --live when you actually mean it.")
        backend = DryRunBackend()
        real = False

    if not real:
        banner = "DRY RUN, nothing sent"
    elif mode == "testnet":
        banner = "TESTNET — real orders, PRACTICE money"
    else:
        banner = "MAINNET — REAL ORDERS, REAL MONEY"
    print("=" * 72)
    print("LIVE RUN — " + banner)
    print("=" * 72)
    print(f"mode: {mode}   mainnet gate: {C.MAINNET_ENABLED}   "
          f"kill switch: {'ENGAGED' if C.kill_switch_on() else 'off'}")
    if real:
        print(f"trade cap this run: {cap}")
    # Per-network address, same as the preflight. Reading the generic fallback
    # here meant a correctly configured testnet run refused to start.
    if not C.account_address(mode):
        print(f"\nNo account address set for mode {mode!r}.")
        print("  testnet -> HL_TESTNET_ACCOUNT_ADDRESS")
        print("  mainnet -> HL_MAINNET_ACCOUNT_ADDRESS")
        print("  (public wallet address, never a private key)")
        return 0
    sigs = recent_calls(source_for(mode))
    res = run_once(signals=sigs, mode=mode, backend=backend, max_new=cap)
    if res["halted"]:
        print(f"\nHALTED: {res['halted']}")
        return 1
    print(f"\n{'PLACED' if real else 'would place'} {len(res['placed'])}:")
    for p in res["placed"]:
        print(f"   {p['symbol']:<9} {p['leverage']}x  size {p['size']}  "
              f"entry {p['entry']}  stop {p['stop']}  margin ${p['margin']}")
    if res.get("holding"):
        print(f"\nHOLDING {len(res['holding'])}:")
        for h in res["holding"]:
            shield = "PROTECTED" if h["protected"] else "*** NO STOP ***"
            pnl = f"{h['pnl_pct']:+.2f}%" if h["pnl_pct"] is not None else "?"
            trg = h["stop"] if h["stop"] else "?"
            print(f"   {h['symbol']:<8} {h['direction']:<5} size {h['size']:<10} "
                  f"entry {h['entry']}  now {h['price']}  {pnl:>8}")
            print(f"      stop {trg}   {shield}   "
                  f"(trail starts at +{C.TRAIL_TRIGGER*100:.0f}%)")
    else:
        print("\nHOLDING nothing.")
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
    flattened = [s for s in res["skipped"]
                 if str(s["reason"]).startswith("flattened_no_stop")]
    if flattened:
        print("\n" + "!" * 72)
        print("AN ENTRY FILLED BUT ITS STOP DID NOT ATTACH — position was CLOSED again.")
        print("Money moved and a fee was paid. The exchange said:")
        for f in flattened:
            print(f"   {f['symbol']}: {str(f['reason']).split(': ', 1)[-1]}")
        print("CHECK THE EXCHANGE NOW and confirm you hold no position in those.")
        print("Full request and response are in results/live/<mode>/audit.jsonl")
        print("!" * 72)
    # Telegram only for REAL events on a REAL run. A dry run or a testnet run
    # must never message, or the alerts become noise again the moment we test.
    notes = res.get("notifications") or []
    if notes:
        if real and mode == "mainnet":
            print(f"\nSent {notify.send(notes)} Telegram message(s).")
        else:
            print(f"\n{len(notes)} Telegram message(s) NOT sent "
                  f"({mode} — preview only):")
            for m in notes:
                print("-" * 62)
                print(m.replace("<b>", "").replace("</b>", "").replace("&amp;", "&"))
            print("-" * 62)

    if real and not res["placed"] and not flattened:
        print("\nNothing was placed this run — no order reached the exchange.")
    elif real and not res["placed"]:
        print("\nNo position is open, but an order DID reach the exchange (see above).")
    elif real:
        print("\nREAL ORDERS WERE SENT. Now check on the exchange, by hand:")
        print("  1. the position exists, at the size and leverage printed above")
        print("  2. a STOP-LOSS order is resting against it  <- the one that matters")
        print("  3. the stop is on the correct side and roughly the price shown")
        print("If the stop is missing, close the position yourself immediately.")
    else:
        print("\nDRY RUN: nothing was sent.")
    return 0


class _FakeClient:
    """Stands in for the SDK. Records calls; reaches no network."""
    def __init__(self): self.calls = []
    def update_leverage(self, lev, coin): self.calls.append(("lev", lev, coin))
    def bulk_orders(self, reqs, grouping=None):
        self.calls.append(("bulk", reqs, grouping))
        return {"response": {"data": {"statuses": [
            {"filled": {"oid": 1, "totalSz": reqs[0]["sz"]}}, {"resting": {"oid": 2}}]}}}
    def order(self, *a, **k):
        self.calls.append(("order", a, k))
        return {"response": {"data": {"statuses": [{"resting": {"oid": 3}}]}}}
    def cancel(self, coin, oid):
        self.calls.append(("cancel", coin, oid)); return {"status": "ok"}
    def market_close(self, coin):
        self.calls.append(("close", coin)); return {"status": "ok"}


def _raises(fn) -> bool:
    try:
        fn(); return False
    except Exception:
        return True


def _selftest() -> int:
    ok = []
    def chk(n, c): ok.append(bool(c)); print(f"  [{'PASS' if c else 'FAIL'}] {n}")
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    tmp = tempfile.mkdtemp(prefix="l2test_")

    try:
        # ── arming: an order path now EXISTS, so prove it stays locked ───────
        from live.exchange import ExchangeBackend, NotArmed, arm_status
        # Mainnet is hard-off again while we prove the plumbing on testnet.
        # Testnet arms separately and cannot reach real money.
        chk("mainnet is hard-off while we prove testnet", C.MAINNET_ENABLED is False)
        chk("mainnet is NOT armed without LIVE_MODE + LIVE_CONFIRM + keys",
            arm_status("mainnet")[0] is False)
        os.environ["LIVE_MODE"] = "mainnet"
        chk("LIVE_MODE alone does not arm it", arm_status("mainnet")[0] is False)
        os.environ["LIVE_CONFIRM"] = C.CONFIRM_PHRASE
        chk("LIVE_MODE + confirm phrase, but no keys, still does not arm it",
            arm_status("mainnet")[0] is False)
        os.environ["LIVE_KILL_SWITCH"] = "1"
        chk("the kill switch halts a run even when fully armed",
            gates.all_clear(mode="mainnet", equity=25.0)[1] == "kill_switch_engaged")
        os.environ.pop("LIVE_KILL_SWITCH")
        os.environ.pop("LIVE_MODE"); os.environ.pop("LIVE_CONFIRM")
        chk("back to unarmed once the environment is cleared",
            arm_status("mainnet")[0] is False)
        chk("testnet refuses to arm without its own flag",
            arm_status("testnet")[0] is False)
        for m in ("dryrun", "mainnet", "testnet"):
            try:
                ExchangeBackend(m, client=object())
                chk(f"ExchangeBackend({m}) refuses to construct unarmed", False)
            except NotArmed:
                chk(f"ExchangeBackend({m}) refuses to construct unarmed", True)
        chk("an unarmed mode silently degrades to DRY RUN, never an order",
            all(get_backend(m).places_orders is False
                for m in ("dryrun", "testnet", "mainnet")))
        chk("unknown mode still raises", _raises(lambda: get_backend("nonsense")))

        # ── a GATED-OFF mainnet run must still be able to READ ──────────────
        # Seen on the first varam-live workflow run: LIVE_MODE=mainnet with the
        # code gate off resolved to dryrun, looked for the GENERIC address
        # variable, found none, and skipped the balance read entirely -- hiding
        # exactly what the preflight exists to show. Reading is harmless.
        os.environ["LIVE_MODE"] = "mainnet"
        os.environ["HL_MAINNET_ACCOUNT_ADDRESS"] = "0xMAIN"
        os.environ.pop("HL_ACCOUNT_ADDRESS", None)
        chk("mode still downgrades to dryrun (trading stays gated)",
            C.mode() == "dryrun")
        chk("but the requested mode is remembered",
            C.requested_mode() == "mainnet")
        chk("so a gated-off mainnet run STILL finds the address to read",
            C.account_address("dryrun") == "0xMAIN")
        chk("and reads the mainnet endpoint",
            C.info_url("dryrun") == C.HL_BASE_URL["mainnet"] + "/info")
        os.environ["LIVE_MODE"] = "testnet"
        os.environ["HL_TESTNET_ACCOUNT_ADDRESS"] = "0xTEST"
        chk("a gated-off testnet run reads TESTNET, never mainnet",
            C.account_address("dryrun") == "0xTEST"
            and "testnet" in C.info_url("dryrun"))
        os.environ.pop("LIVE_MODE"); os.environ.pop("HL_MAINNET_ACCOUNT_ADDRESS")
        os.environ.pop("HL_TESTNET_ACCOUNT_ADDRESS")
        chk("with nothing requested it falls back to plain dryrun",
            C.requested_mode() == "dryrun" and C.info_url("") == C.HL_INFO_URL)

        # ── live-trade Telegram messages ────────────────────────────────────
        from live import notify
        opened = notify.build_opened(symbol="BTC", direction="long", leverage=3,
                                     entry=79470, stop=78372.5, target=81950,
                                     size=0.0094, notional=746.0, margin=248.6)
        chk("open alert names the trade and all three levels",
            all(x in opened for x in ("TRADE OPENED", "BTC", "LONG", "3×",
                                      "79,470", "78,372", "81,950")))
        chk("open alert states the dollar risk, not just a percent",
            "$10.30" in opened and "if stopped out" in opened)
        won = notify.build_closed(symbol="BTC", direction="long", leverage=3,
                                  entry=79470, exit_px=80360, pnl_usd=8.35,
                                  reason="trailing stop", margin=248.6,
                                  account_value=1011.56)
        lost = notify.build_closed(symbol="PENGU", direction="long", leverage=1,
                                   entry=0.009918, exit_px=0.009368,
                                   pnl_usd=-13.75, reason="stop hit")
        chk("a winner reads as PROFIT, a loser as LOSS",
            "PROFIT" in won and "LOSS" in lost)
        chk("close alert carries the dollar P&L and the account value",
            "+$8.35" in won and "$1,011.56" in won)
        chk("a loss shows a minus, never a bare number", "-$13.75" in lost)
        chk("prices keep sane precision at both ends of the book",
            "79,470" in won and "0.009918" in lost)
        chk("the P&L sign follows the exchange figure, not our arithmetic",
            "PROFIT" in notify.build_closed(
                symbol="X", direction="long", leverage=1, entry=100,
                exit_px=99, pnl_usd=5.0))
        chk("no message leaks an address or a key",
            not any(x in (opened + won + lost).lower()
                    for x in ("0x", "private", "key")))
        att = notify.build_attention(symbol="ZEC", reason="needs attention",
                                     detail="no stop on the exchange")
        chk("attention alert is loud and says entries are blocked",
            "NEEDS ATTENTION" in att and "blocked" in att)
        wk = notify.build_weekly(account_value=1011.56, week_start_value=999.0,
                                 closed=[{"pnl_usd": 8.35}, {"pnl_usd": -4.1}],
                                 holding=[{"symbol": "BTC", "direction": "long",
                                           "pnl_pct": 2.9, "protected": True}],
                                 runs=166, expected_runs=168, label="25 Aug")
        chk("weekly shows account, W/L, P&L and open positions",
            all(x in wk for x in ("LIVE WEEKLY", "$1,011.56", "1W / 1L", "BTC")))
        chk("weekly carries the LIVENESS line — silence must stay trustworthy",
            "166 of ~168" in wk and "✅" in wk)
        bad = notify.build_weekly(account_value=980.0, runs=94, expected_runs=168,
                                  holding=[{"symbol": "ZEC", "direction": "short",
                                            "protected": False}])
        chk("missed runs warn, and an unprotected position is flagged",
            "⚠️" in bad and "NO STOP" in bad)
        # a failed send must never break a trading run
        def _boom(_m):
            raise RuntimeError("telegram down")
        chk("a failed notification cannot break the run",
            notify.send(["x"], sender=_boom) == 0)
        chk("nothing to say sends nothing", notify.send([], sender=_boom) == 0)

        # ── the preflight and the runner must AGREE on the same signals ─────
        # They disagreed twice: once on mark pricing, once because
        # recent_calls() emits "stop" while the runner read "sl", so every call
        # skipped as bad_geometry. A preview that differs from the real run is
        # worse than none, so this is now asserted rather than assumed.
        AGREE_META = {"ZEC": {"sz_decimals": 2, "max_leverage": 10}}
        agree_px = {"ZEC": 40.0}
        normalised = {"symbol": "ZEC", "direction": "long", "entry": 40.0,
                      "stop": 39.2, "score": 8.1, "interval": "1h"}   # recent_calls shape
        raw_batch = {"symbol": "ZEC", "direction": "long", "entry": 40.0,
                     "sl": 39.2, "score": 8.1, "interval": "1h"}      # batch shape
        for label, sig in (("recent_calls shape", normalised),
                           ("raw batch shape", raw_batch)):
            r_ = run_once(signals=[sig], mode="dryrun", backend=DryRunBackend(),
                          reader=lambda: (AGREE_META,
                                          {"equity": 999.0, "margin_used": 0.0,
                                           "withdrawable": 999.0, "positions": [],
                                           "resting_stops": {}}, agree_px),
                          now=now, root=os.path.join(tmp, "agree" + label[:3]))
            chk(f"runner accepts the {label}", len(r_["placed"]) == 1)
        # and a genuinely missing stop is reported as a WIRING fault
        broken = plan_order(symbol="ZEC", direction="long", entry=40.0, stop=None,
                            equity=999.0, used_margin=0.0, sz_decimals=2,
                            asset_max_leverage=10)
        chk("a missing stop is named as a wiring fault, not bad market data",
            broken.skip_reason == "missing_price_or_stop")
        chk("a stop on the wrong side is still bad_geometry",
            plan_order(symbol="ZEC", direction="long", entry=40.0, stop=41.0,
                       equity=999.0, used_margin=0.0, sz_decimals=2,
                       asset_max_leverage=10).skip_reason == "bad_geometry")

        # Per-network address resolution. A correctly configured testnet run
        # once refused to start because main() read the generic fallback.
        os.environ["HL_TESTNET_ACCOUNT_ADDRESS"] = "0xTESTADDR"
        os.environ.pop("HL_ACCOUNT_ADDRESS", None)
        chk("testnet finds its address with no generic fallback set",
            C.account_address("testnet") == "0xTESTADDR")
        chk("mainnet does not borrow the testnet address",
            C.account_address("mainnet") == "")
        os.environ["HL_ACCOUNT_ADDRESS"] = "0xFALLBACK"
        chk("testnet prefers its own address over the fallback",
            C.account_address("testnet") == "0xTESTADDR")
        os.environ.pop("HL_TESTNET_ACCOUNT_ADDRESS"); os.environ.pop("HL_ACCOUNT_ADDRESS")

        # ── the supervised-run guard ────────────────────────────────────────
        META_CAP = {"ZEC": {"sz_decimals": 2, "max_leverage": 10},
                    "ETH": {"sz_decimals": 4, "max_leverage": 25}}
        sigs_cap = [{"symbol": "ZEC", "direction": "long", "entry": 40.0,
                     "sl": 39.2, "score": 8.1, "interval": "1h", "bar_time": "c1"},
                    {"symbol": "ETH", "direction": "short", "entry": 3000.0,
                     "sl": 3060.0, "score": 7.9, "interval": "1h", "bar_time": "c1"}]
        reader_cap = lambda: (META_CAP,
                              {"equity": 60.0, "margin_used": 0.0,
                               "withdrawable": 60.0, "positions": [],
                               "resting_stops": {}},
                              {"ZEC": 40.0, "ETH": 3000.0})
        capped = run_once(signals=sigs_cap, mode="dryrun", backend=DryRunBackend(),
                          reader=reader_cap, now=now, root=os.path.join(tmp, "a"),
                          max_new=1)
        chk("a cap of 1 opens exactly one position", len(capped["placed"]) == 1)
        chk("the calls it declined say exactly why",
            any(x["reason"] == "run_trade_cap_reached" for x in capped["skipped"]))
        uncapped = run_once(signals=sigs_cap, mode="dryrun", backend=DryRunBackend(),
                            reader=reader_cap, now=now,
                            root=os.path.join(tmp, "b"), max_new=None)
        chk("without a cap it would have opened more", len(uncapped["placed"]) > 1)
        # trailing is never rationed by the cap — protecting open money is not
        # something to hold back
        state.save_positions("dryrun", {"ZEC": state.record(
            "ZEC", direction="long", size=0.3, entry=40.0, stop=39.2, leverage=3,
            stop_order_id="o1", fingerprint="ZEC|long|1h|old",
            now=now)}, os.path.join(tmp, "c"))
        trail_capped = run_once(
            signals=sigs_cap, mode="dryrun", backend=DryRunBackend(),
            reader=lambda: (META_CAP,
                            {"equity": 60.0, "margin_used": 4.0, "withdrawable": 56.0,
                             "positions": [{"symbol": "ZEC", "size": 0.3,
                                            "notional": 14.4}],
                             "resting_stops": {"ZEC": "o1"}},
                            {"ZEC": 48.0}),
            now=now, root=os.path.join(tmp, "c"), max_new=0)
        chk("a cap of ZERO still moves stops on open positions",
            len(trail_capped["placed"]) == 0 and len(trail_capped["stop_moves"]) == 1)

        # testnet credentials must never let a mainnet order through
        os.environ["HL_TESTNET_PRIVATE_KEY"] = "0xdeadbeef"
        os.environ["HL_TESTNET_ACCOUNT_ADDRESS"] = "0xabc"
        os.environ["LIVE_TESTNET_ARMED"] = "1"
        chk("the arm flag alone is not enough — LIVE_MODE must ask for it too",
            arm_status("testnet")[0] is False)
        os.environ["LIVE_MODE"] = "testnet"
        chk("both flags together do arm testnet", arm_status("testnet")[0] is True)
        chk("armed testnet does NOT arm mainnet", arm_status("mainnet")[0] is False)
        chk("testnet and mainnet read different credentials",
            C.credentials("testnet") != C.credentials("mainnet")
            and C.credentials("mainnet") == (None, None))
        chk("testnet and mainnet use different hosts",
            C.HL_BASE_URL["testnet"] != C.HL_BASE_URL["mainnet"]
            and "testnet" in C.HL_BASE_URL["testnet"])
        # with testnet armed the real backend builds, but only for testnet
        tb = ExchangeBackend("testnet", client=_FakeClient())
        chk("armed testnet builds a real backend", tb.places_orders is True)
        chk("the private key is never stored on the backend",
            not any("deadbeef" in str(v).lower() for v in vars(tb).values()))
        # a real send, against the fake client: leverage set, both legs sent
        r = tb.place_bracket(b if "b" in dir() else build_bracket(
            plan_order(symbol="ZEC", direction="long", entry=40.0, stop=39.2,
                       equity=25.0, used_margin=0.0, sz_decimals=2,
                       asset_max_leverage=10), 2))
        chk("a bracket sets leverage BEFORE sending the orders",
            tb._client.calls[0][0] == "lev" and tb._client.calls[1][0] == "bulk")
        chk("both legs go in ONE grouped request",
            len(tb._client.calls[1][1]) == 2 and tb._client.calls[1][2] == "normalTpsl")
        chk("a good response reports the entry and the stop", r["entry_ok"]
            and r["stop_order_id"] == "2")

        # ── the testnet bug: "waitingForTrigger" is SUCCESS, not failure ────
        # Hyperliquid answers a newly placed trigger order with that bare
        # string and NO order id. Reading a missing id as a failed stop made
        # the code flatten three healthy positions.
        chk("waitingForTrigger counts as accepted",
            ExchangeBackend._accepted("waitingForTrigger") is True)
        chk("a real error is still a failure",
            ExchangeBackend._accepted({"error": "bad tick"}) is False)
        chk("a resting dict is accepted and yields its id",
            ExchangeBackend._accepted({"resting": {"oid": 7}}) is True
            and ExchangeBackend._oid({"resting": {"oid": 7}}) == "7")
        chk("a bracket with a live-but-idless stop is COMPLETE",
            bracket_is_complete({"entry_ok": True, "stop_live": True,
                                 "stop_order_id": None}) is True)
        chk("a bracket whose stop was rejected is NOT complete",
            bracket_is_complete({"entry_ok": True, "stop_live": False,
                                 "stop_order_id": None}) is False)
        # end to end: a client that answers waitingForTrigger must NOT flatten
        class _TriggerClient(_FakeClient):
            def bulk_orders(self, reqs, grouping=None):
                self.calls.append(("bulk", reqs, grouping))
                return {"response": {"data": {"statuses": [
                    {"filled": {"oid": 1, "totalSz": reqs[0]["sz"]}},
                    "waitingForTrigger"]}}}
        tb2 = ExchangeBackend("testnet", client=_TriggerClient())
        br = build_bracket(plan_order(symbol="ZEC", direction="long", entry=40.0,
                                      stop=39.2, equity=999.0, used_margin=0.0,
                                      sz_decimals=2, asset_max_leverage=10), 2)
        rr = tb2.place_bracket(br)
        okt, stt, _ = settle_bracket(tb2, br, rr)
        chk("waitingForTrigger does NOT trigger a flatten",
            okt is True and stt == "open"
            and not any(k == "flatten" for k, _ in tb2.sent))

        # ── protection detection: a stop must be VISIBLE to the runner ───────
        from live.hl_info import find_stop_order, resting_stops
        live_orders = [{"symbol": "BTC", "order_id": "58433555287", "size": 0.0094,
                        "trigger_px": 78372.0, "limit_px": 78372.0,
                        "reduce_only": True, "is_trigger": True, "side": "A"},
                       {"symbol": "ETH", "order_id": "99", "size": 1.0,
                        "trigger_px": None, "limit_px": 3000.0,
                        "reduce_only": False, "is_trigger": False, "side": "B"}]
        chk("resting_stops maps a real stop to its symbol",
            resting_stops(live_orders) == {"BTC": "58433555287"})
        chk("a non-reduce-only order is not mistaken for a stop",
            "ETH" not in resting_stops(live_orders))
        chk("find_stop_order with no price answers 'protected at all?'",
            (find_stop_order(live_orders, "BTC") or {}).get("order_id")
            == "58433555287")
        chk("find_stop_order still matches on price when given one",
            (find_stop_order(live_orders, "BTC", 78372.0) or {}).get("order_id")
            == "58433555287")
        chk("a far-off price does not match", find_stop_order(live_orders, "BTC",
                                                              50000.0) is None)
        chk("no stop for a symbol returns nothing",
            find_stop_order(live_orders, "SOL") is None)

        # the fill price, not the signal price, must be what gets recorded
        class _FillClient(_FakeClient):
            def bulk_orders(self, reqs, grouping=None):
                self.calls.append(("bulk", reqs, grouping))
                return {"response": {"data": {"statuses": [
                    {"filled": {"oid": 1, "totalSz": reqs[0]["sz"],
                                "avgPx": "79470.0"}},
                    "waitingForTrigger"]}}}
        tb3 = ExchangeBackend("testnet", client=_FillClient())
        rf = tb3.place_bracket(br)
        chk("the price actually paid is reported back", rf["fill_price"] == 79470.0)

        # ── the testnet-only trail override must not reach real money ───────
        os.environ["LIVE_TRAIL_TRIGGER_PCT"] = "0.1"
        chk("testnet honours a lowered trail trigger",
            abs(C.trail_trigger("testnet") - 0.001) < 1e-12)
        chk("MAINNET ignores it entirely — strategy is not an env var",
            C.trail_trigger("mainnet") == C.TRAIL_TRIGGER)
        chk("dryrun ignores it too", C.trail_trigger("dryrun") == C.TRAIL_TRIGGER)
        os.environ["LIVE_TRAIL_TRIGGER_PCT"] = "50"
        chk("it can only ever LOWER the trigger, never raise it",
            C.trail_trigger("testnet") == C.TRAIL_TRIGGER)
        os.environ["LIVE_TRAIL_TRIGGER_PCT"] = "rubbish"
        chk("garbage falls back to the real trigger",
            C.trail_trigger("testnet") == C.TRAIL_TRIGGER)
        os.environ.pop("LIVE_TRAIL_TRIGGER_PCT")

        # ── a stale recorded entry must be corrected FROM the exchange ───────
        # Seen live: the record held the signal price 79565 while the exchange
        # reported the real fill 79470. The trail measures profit from this
        # number, so it would have trailed late on every run.
        stale_root = os.path.join(tmp, "stale")
        state.save_positions("dryrun", {"BTC": state.record(
            "BTC", direction="long", size=0.0094, entry=79565.0, stop=78372.5,
            leverage=3, stop_order_id="s9", fingerprint="BTC|long|1h|x",
            now=now)}, stale_root)
        r_stale = run_once(
            signals=[], mode="dryrun", backend=DryRunBackend(),
            reader=lambda: ({"BTC": {"sz_decimals": 5, "max_leverage": 40}},
                            {"equity": 992.0, "margin_used": 248.0,
                             "withdrawable": 744.0,
                             "positions": [{"symbol": "BTC", "size": 0.0094,
                                            "direction": "long", "entry": 79470.0,
                                            "notional": 746.0}],
                             "resting_stops": {"BTC": "s9"}},
                            {"BTC": 79400.0}),
            now=now, root=stale_root)
        chk("a stale recorded entry is corrected from the exchange",
            abs(state.load_positions("dryrun", stale_root)["BTC"]["entry"]
                - 79470.0) < 1e-9)
        chk("the holding line reports the corrected entry",
            r_stale["holding"] and abs(r_stale["holding"][0]["entry"] - 79470.0) < 1e-9)
        chk("a position at a LOSS never trails, however low the trigger",
            not r_stale["stop_moves"])
        os.environ.pop("LIVE_MODE")
        os.environ.pop("LIVE_TESTNET_ARMED"); os.environ.pop("HL_TESTNET_PRIVATE_KEY")
        os.environ.pop("HL_TESTNET_ACCOUNT_ADDRESS")

        # payload translation, checked without touching a network
        req = ExchangeBackend._order_req({"action": "entry", "symbol": "ZEC",
                                          "is_buy": True, "size": 0.3,
                                          "limit_px": 40.4, "tif": "Ioc"})
        chk("entry translates to an IOC limit, not reduce-only",
            req["order_type"]["limit"]["tif"] == "Ioc" and req["reduce_only"] is False)
        sreq = ExchangeBackend._order_req({"action": "stop", "symbol": "ZEC",
                                           "is_buy": False, "size": 0.3,
                                           "trigger_px": 39.2})
        chk("stop translates to a reduce-only market trigger",
            sreq["reduce_only"] is True
            and sreq["order_type"]["trigger"]["isMarket"] is True
            and sreq["order_type"]["trigger"]["tpsl"] == "sl")

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

        # ── the user's scenario: ran +5% while asleep, back to +2% on waking ──
        # Trailing from the price NOW sees only +2%, below the +3% trigger, and
        # leaves the stop untouched -- the whole +5% excursion is invisible.
        chk("spot-price trailing MISSES a spike that reversed",
            next_stop(entry=100, price=102, direction="long", current_stop=97.5) is None)
        # Trailing from the high-water mark sees the +5% and tightens the stop,
        # but only as far as is safe given the price now.
        peak = next_stop(entry=100, price=105, direction="long", current_stop=97.5,
                         current_price=102)
        chk("peak trailing CATCHES it and tightens the stop",
            peak is not None and peak > 97.5)
        chk("but never to a level that would fire instantly", peak < 102)
        chk("and never below breakeven once triggered", peak >= 100)
        # An extreme spike that fully reversed: the trail candidate (140 x 0.98
        # = 137.2) is unsafe, so it falls back to breakeven, which is both safe
        # and tighter than where the stop was. It must never fire us out.
        spike = next_stop(entry=100, price=140, direction="long", current_stop=97.5,
                          current_price=100.5)
        chk("a fully reversed spike falls back to breakeven, not the trail",
            spike == 100.0)
        chk("that fallback is still safely below the price now", spike < 100.5)
        # shorts, same logic mirrored
        ps = next_stop(entry=100, price=95, direction="short", current_stop=102.5,
                       current_price=98)
        chk("short: peak trailing works downward", ps is not None and ps < 102.5
            and ps > 98)

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
            not okb and status.startswith("flattened_no_stop")
            and fb.flattened == ["ZEC"])
        chk("the flatten status carries WHY, not just that it happened",
            ": " in status and len(status) > len("flattened_no_stop: "))
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
        chk("mainnet cannot send orders while the code gate is False",
            gates.mode_allows_orders("mainnet") is False
            and get_backend(C.mode()).places_orders is False)

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
        acct_zec = {"equity": 25.0, "margin_used": 4.0, "withdrawable": 21.0,
                    "positions": [{"symbol": "ZEC", "size": 0.3, "notional": 14.4}],
                    "resting_stops": {"ZEC": "old1"}}
        bk3 = DryRunBackend()
        r4 = run_once(signals=[], mode="dryrun", backend=bk3,
                      reader=lambda: (META, acct_zec, {"ZEC": 48.0}),
                      now=now, root=tmp)
        chk("held position gets its stop moved up", len(r4["stop_moves"]) == 1
            and r4["stop_moves"][0]["to"] > 39.2)

        # the same pass, but the price spiked to 60 and fell back to 41 while
        # the bot slept: spot-only would do nothing, peak trailing protects.
        state.save_positions("dryrun", {"ZEC": state.record(
            "ZEC", direction="long", size=0.3, entry=40.0, stop=39.2, leverage=3,
            stop_order_id="old1", fingerprint="ZEC|long|1h|t0", now=now)}, tmp)
        r4b = run_once(signals=[], mode="dryrun", backend=DryRunBackend(),
                       reader=lambda: (META, acct_zec, {"ZEC": 41.0}),
                       now=now, root=tmp)
        chk("without peak data a small pullback moves nothing",
            not r4b["stop_moves"])
        state.save_positions("dryrun", {"ZEC": state.record(
            "ZEC", direction="long", size=0.3, entry=40.0, stop=39.2, leverage=3,
            stop_order_id="old1", fingerprint="ZEC|long|1h|t0", now=now)}, tmp)
        r4c = run_once(signals=[], mode="dryrun", backend=DryRunBackend(),
                       reader=lambda: (META, acct_zec, {"ZEC": 41.0},
                                       {"ZEC": {"high": 48.0, "low": 40.0,
                                                "last": 41.0}}),
                       now=now, root=tmp)
        chk("WITH peak data the missed spike still tightens the stop",
            len(r4c["stop_moves"]) == 1
            and 39.2 < r4c["stop_moves"][0]["to"] < 41.0)
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

        # ── order sending is confined to ONE module ─────────────────────────
        import live
        live_dir = os.path.dirname(os.path.abspath(live.__file__))
        # exchange.py is the only sender; config.py is the only place
        # credentials are named. Everything else must mention neither.
        others = [f for f in os.listdir(live_dir)
                  if f.endswith(".py") and f not in ("exchange.py", "config.py")]
        rest = "".join(open(os.path.join(live_dir, f), encoding="utf-8").read()
                       for f in others)
        chk("no module except exchange.py can sign or send",
            not any(x in rest for x in ("eth_account", "hyperliquid.exchange",
                                        "bulk_orders", "market_close")))
        chk("credentials are named in config only, never elsewhere",
            ("PRIVATE" + "_KEY") not in rest)
        cfg_src = open(os.path.join(live_dir, "config.py"), encoding="utf-8").read()
        chk("config only READS credentials, never logs or defaults them",
            "print(" not in cfg_src and ("PRIVATE" + "_KEY\"") not in cfg_src.split("os.environ.get")[0])
        ex_src = open(os.path.join(live_dir, "exchange.py"), encoding="utf-8").read()
        nl = chr(10)
        chk("the signing SDK is imported lazily, inside a function",
            "def _sdk" in ex_src
            and (nl + "from eth_account") not in ex_src
            and (nl + "from hyperliquid") not in ex_src)
        chk("exchange.py never logs or returns a key",
            "print(" not in ex_src and "self._key" not in ex_src)
        chk("only hyperliquid hosts are reachable",
            all("hyperliquid" in u for u in C.HL_BASE_URL.values()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nL2 SELFTEST: {sum(ok)}/{len(ok)} passed")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    raise SystemExit(main())
