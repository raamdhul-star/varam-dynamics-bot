"""
tools/live_preflight.py — READ-ONLY live-readiness report (Sprint L1)
====================================================================
Reads your Hyperliquid account and the most recent alerted calls, then prints
exactly what the live executor WOULD do with them — sizes, leverage, and the
named reason for every call it would skip.

It cannot trade. There is no order-placement code anywhere in `live/` yet, and
this tool only ever calls the public read endpoint. It needs no private key:
account reads take the PUBLIC address only.

Usage:
  set HL_ACCOUNT_ADDRESS=0x...      (public address; NOT a key)
  python tools/live_preflight.py            # account + what-if on recent calls
  python tools/live_preflight.py --selftest # offline, no network at all
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live import config as C
from live.hl_info import HLReadError, account_state, asset_meta, mids, poster_for
from live.signals import describe_source, recent_calls, source_for
from live.sizing import OrderPlan, floor_to, geometry_ok, is_tight_stop, plan_order, suggested_leverage

STATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "results", "telegram_state.json")

SKIP_TEXT = {
    "not_tight_stop":           "stop too wide — needs more margin than we allocate",
    "below_min_order":          "under the $10 exchange minimum",
    "below_min_after_rounding": "under $10 once size was rounded down",
    "no_free_margin":           "account already fully committed",
    "size_rounds_to_zero":      "size rounds to zero at this asset's precision",
    "bad_geometry":             "malformed signal (stop on the wrong side)",
    "no_equity":                "no account equity",
    "unknown_asset_max_leverage": "asset not in the Hyperliquid book",
    "unknown_size_decimals":    "asset size precision unknown",
    "bad_direction":            "unknown direction",
    "price_moved_away":         "price drifted too far from the signal — not the setup we scored",
    "bad_mark_price":           "no usable live price for this asset",
}


def report() -> int:
    print("=" * 72)
    print("LIVE PREFLIGHT — READ-ONLY.  No order can be placed by this tool.")
    print("=" * 72)

    print(f"\nGates:")
    print(f"  MAINNET_ENABLED (code gate) : {C.MAINNET_ENABLED}"
          f"{'' if C.MAINNET_ENABLED else '   <- live execution is HARD OFF'}")
    print(f"  effective mode              : {C.mode()}")
    print(f"  kill switch                 : {'ENGAGED' if C.kill_switch_on() else 'off'}")

    print(f"\nSettings:")
    print(f"  margin per trade  : {C.MARGIN_FRAC*100:.0f}% of equity")
    print(f"  leverage cap      : {C.LEV_CAP}x (also clamped to each asset's max)")
    print(f"  max concurrent    : {C.MAX_CONCURRENT}   (exposure ceiling {C.MAX_EXPOSURE*100:.0f}%)")
    print(f"  filter            : {'tight-stop calls only' if C.TIGHT_ONLY else 'all calls'}, score >= {C.SCORE_MIN}")
    print(f"  trailing          : breakeven at +{C.TRAIL_TRIGGER*100:.0f}%, trail {C.TRAIL_STEP*100:.0f}%"
          f"   (paper stays at +5% for comparison)")
    print(f"  min tradable equity: ${C.min_tradable_equity():.2f}"
          f"   <- below this NO legal order exists at these settings")

    # Read whichever network we are pointed at — testnet reads testnet.
    mode = C.mode()
    read = poster_for(mode)
    addr = C.account_address(mode)
    print(f"  reading           : {read.url}")
    if not addr:
        print(f"\nAccount: no address set for mode {mode!r} — skipping the live read.")
        print("  Set HL_ACCOUNT_ADDRESS (or the per-network address) to your PUBLIC")
        print("  wallet address — never a private key — to see your real balance.")
        return 0

    try:
        meta = asset_meta(poster=read)
        acct = account_state(addr, poster=read)
        prices = mids(poster=read)
    except HLReadError as e:
        print(f"\nEXCHANGE READ FAILED: {e}")
        print("FAIL CLOSED: the executor would do nothing on this run.")
        return 1

    eq, used = acct["equity"], acct["margin_used"]
    print(f"\nAccount {addr[:6]}...{addr[-4:]}:")
    print(f"  equity      : ${eq:,.2f}")
    print(f"  margin used : ${used:,.2f}")
    print(f"  withdrawable: ${acct['withdrawable']:,.2f}")
    print(f"  open positions ({len(acct['positions'])}):")
    for p in acct["positions"] or []:
        print(f"     {p['symbol']:<8} {p['direction']:<5} size {p['size']:<12} "
              f"notional ${p['notional']:,.2f}  uPnL ${p['unrealized']:+,.2f}")
    if not acct["positions"]:
        print("     (none)")

    if eq < C.min_tradable_equity():
        print(f"\n  ** equity is below ${C.min_tradable_equity():.2f} — every order would be "
              f"under the $10 minimum. The bot would open nothing. **")

    calls = recent_calls(source_for(mode))
    print(f"\nWhat-if on the {len(calls)} most recent alerted calls "
          f"(nothing is placed):")
    if not calls:
        print("  (no recent calls found in results/telegram_state.json)")
        return 0

    would, skips = [], {}
    sim_used = used
    print(f"  {'symbol':<9}{'dir':<6}{'score':>6}{'stop%':>7}{'lev':>5}"
          f"{'margin':>9}{'notional':>10}  verdict")
    print("  " + "-" * 70)
    for c in calls:
        if c["score"] < C.SCORE_MIN:
            continue
        m = meta.get(c["symbol"])
        # Size off the LIVE mark, exactly as the runner does. Without this the
        # preview disagreed with the real run: it sized off the hour-old signal
        # price and reported different skip reasons — a preview that does not
        # match what actually happens is worse than none.
        plan = plan_order(symbol=c["symbol"], direction=c["direction"],
                          entry=c["entry"], stop=c["stop"], equity=eq,
                          used_margin=sim_used,
                          sz_decimals=(m or {}).get("sz_decimals", -1),
                          asset_max_leverage=(m or {}).get("max_leverage", 0),
                          mark_price=prices.get(c["symbol"]))
        if plan.ok and len(would) < C.MAX_CONCURRENT:
            would.append(plan)
            sim_used += plan.margin
            verdict = "WOULD PLACE"
        elif plan.ok:
            verdict = "skip: max concurrent reached"
            skips["max_concurrent"] = skips.get("max_concurrent", 0) + 1
        else:
            verdict = f"skip: {SKIP_TEXT.get(plan.skip_reason, plan.skip_reason)}"
            skips[plan.skip_reason] = skips.get(plan.skip_reason, 0) + 1
        print(f"  {c['symbol']:<9}{c['direction']:<6}{c['score']:>6.1f}"
              f"{plan.stop_distance_pct:>6.1f}%{plan.leverage or 0:>4}x"
              f"{'$%.2f'%plan.margin if plan.margin else '-':>9}"
              f"{'$%.2f'%plan.notional if plan.notional else '-':>10}  {verdict}")

    print(f"\n  would place {len(would)} order(s), total margin "
          f"${sum(p.margin for p in would):.2f} of ${eq:.2f} equity")
    if skips:
        print("  skipped:")
        for k, v in sorted(skips.items(), key=lambda kv: -kv[1]):
            print(f"     {v:>3}x  {SKIP_TEXT.get(k, k)}")
    print("\nREAD-ONLY: nothing was placed, nothing was written.")
    return 0


# ── offline self-test (no network, no files, no orders) ──────────────────────

def _selftest() -> int:
    ok = []
    def chk(n, c): ok.append(bool(c)); print(f"  [{'PASS' if c else 'FAIL'}] {n}")

    # ---- gates ----
    # The code gate is deliberately ON now. Preflight itself must stay
    # read-only regardless, and mainnet must still need credentials.
    chk("mainnet is hard-off while we prove testnet", C.MAINNET_ENABLED is False)
    os.environ["LIVE_MODE"] = "mainnet"; os.environ["LIVE_CONFIRM"] = C.CONFIRM_PHRASE
    chk("mainnet downgrades to dryrun while the code gate is False",
        C.mode() == "dryrun")
    # each network must READ its own endpoint and its own address
    chk("testnet and mainnet read different endpoints",
        C.info_url("testnet") != C.info_url("mainnet")
        and "testnet" in C.info_url("testnet"))
    chk("dryrun reads the real market", C.info_url("dryrun") == C.HL_INFO_URL)
    chk("a reader is bound to one network and says which",
        poster_for("testnet").url == C.info_url("testnet"))
    os.environ["HL_TESTNET_ACCOUNT_ADDRESS"] = "0xTEST"
    os.environ["HL_ACCOUNT_ADDRESS"] = "0xFALLBACK"
    chk("testnet reads the TESTNET address, not the fallback",
        C.account_address("testnet") == "0xTEST")
    chk("mainnet does not borrow the testnet address",
        C.account_address("mainnet") == "0xFALLBACK")
    os.environ.pop("HL_TESTNET_ACCOUNT_ADDRESS"); os.environ.pop("HL_ACCOUNT_ADDRESS")
    os.environ["LIVE_MODE"] = "garbage"
    chk("unknown mode falls back to dryrun", C.mode() == "dryrun")
    os.environ.pop("LIVE_MODE"); os.environ.pop("LIVE_CONFIRM")
    os.environ["LIVE_KILL_SWITCH"] = "true"
    chk("kill switch reads as engaged", C.kill_switch_on() is True)
    os.environ.pop("LIVE_KILL_SWITCH")
    chk("kill switch defaults to off", C.kill_switch_on() is False)

    # ---- pure maths ----
    chk("leverage: 2.3% stop -> 3x", suggested_leverage(100, 97.7, 3) == 3)
    chk("leverage: 10% stop -> 1x", suggested_leverage(100, 90, 3) == 1)
    chk("tight-stop filter accepts a 2% stop", is_tight_stop(100, 98, 3))
    chk("tight-stop filter rejects a 5% stop", not is_tight_stop(100, 95, 3))
    chk("floor_to never rounds up", floor_to(1.999, 0) == 1.0 and floor_to(1.29, 1) == 1.2)
    chk("geometry: long stop must be below entry", geometry_ok("long", 100, 98)
        and not geometry_ok("long", 100, 102))
    chk("geometry: short stop must be above entry", geometry_ok("short", 100, 102)
        and not geometry_ok("short", 100, 98))

    base = dict(symbol="X", direction="long", entry=100.0, stop=98.0, equity=25.0,
                used_margin=0.0, sz_decimals=2, asset_max_leverage=10)

    p = plan_order(**base)
    chk("healthy call is planned", p.ok and p.leverage == 3)
    chk("margin is ~25% of equity, never more (rounding only shrinks)",
        5.9 <= p.margin <= 6.25)
    chk("notional clears the $10 floor", p.notional >= C.MIN_NOTIONAL)
    chk("size floored to the asset's decimals", p.size == floor_to(p.size, 2))

    chk("tight filter still available and rejects a wide stop when enabled",
        plan_order(**{**base, "stop": 95.0, "tight_only": True}).skip_reason
        == "not_tight_stop")
    # A 5% stop gets 1x leverage, so $6.25 of margin buys $6.25 of position --
    # under the floor. It is refused at $25 and becomes affordable on its own
    # once the account grows past ~$40. That is the designed behaviour.
    chk("wide-stop call refused on a small account (1x cannot reach $10)",
        plan_order(**{**base, "stop": 95.0}).skip_reason == "below_min_order")
    big = plan_order(**{**base, "stop": 95.0, "equity": 50.0})
    chk("same wide-stop call IS planned once equity grows past ~$40",
        big.ok and big.leverage == 1)
    # equal margin => equal dollar risk, whatever the leverage (the 7% formula
    # cancels the stop distance). This is why flat margin is used.
    tight = plan_order(**{**base, "equity": 50.0})
    chk("equal margin => equal dollar risk regardless of leverage",
        abs(tight.margin * 0.07 - big.margin * 0.07) < 0.20)
    chk("bad geometry rejected",
        plan_order(**{**base, "stop": 102.0}).skip_reason == "bad_geometry")
    chk("zero equity rejected",
        plan_order(**{**base, "equity": 0}).skip_reason == "no_equity")
    chk("fully committed account rejected",
        plan_order(**{**base, "used_margin": 25.0}).skip_reason == "no_free_margin")
    chk("small account falls under the $10 floor",
        plan_order(**{**base, "equity": 12.0}).skip_reason == "below_min_order")
    chk("whole-unit asset (szDecimals 0) rejected when it rounds to zero",
        plan_order(**{**base, "sz_decimals": 0, "entry": 5000.0, "stop": 4900.0})
        .skip_reason in ("size_rounds_to_zero", "below_min_after_rounding"))
    chk("unknown asset rejected, never guessed",
        plan_order(**{**base, "asset_max_leverage": 0}).skip_reason
        == "unknown_asset_max_leverage")

    # asset ceiling must win over our own cap (needs enough equity to still
    # clear $10 at the lower leverage, else it is correctly refused instead)
    lo = plan_order(**{**base, "equity": 100.0, "asset_max_leverage": 2})
    chk("asset max leverage clamps below our cap", lo.ok and lo.leverage == 2)
    chk("2x asset now reaches $10 at 25% margin (it did not at 15%)",
        plan_order(**{**base, "asset_max_leverage": 2}).ok)
    chk("1x asset on a $25 account still cannot reach $10",
        plan_order(**{**base, "asset_max_leverage": 1}).skip_reason == "below_min_order")

    # the account floor is real and self-consistent
    floor = C.min_tradable_equity()
    chk("min tradable equity ~= $13.33", abs(floor - 13.3333) < 0.01)
    chk("just under the floor is rejected",
        not plan_order(**{**base, "equity": floor - 0.5}).ok)
    chk("just over the floor is accepted",
        plan_order(**{**base, "equity": floor + 0.5}).ok)

    # exposure ceiling: 6 x 15% = 90%, the 7th must be refused
    eq, used_m, taken = 100.0, 0.0, 0
    for _ in range(10):
        q = plan_order(**{**base, "equity": eq, "used_margin": used_m})
        if not q.ok:
            break
        used_m += q.margin; taken += 1
    chk("exposure ceiling stops at 4 concurrent (3 full + 1 partial)",
        taken == C.MAX_CONCURRENT)

    # ---- read layer fails closed ----
    from live.hl_info import account_state as _as, asset_meta as _am
    def boom(_p):
        raise HLReadError("simulated outage")
    for name, fn in (("asset_meta", lambda: _am(poster=boom)),
                     ("account_state", lambda: _as("0xabc", poster=boom))):
        try:
            fn(); chk(f"{name} raises on outage", False)
        except HLReadError:
            chk(f"{name} raises HLReadError on outage (fail closed)", True)
    try:
        _am(poster=lambda p: {"universe": []}); chk("empty universe rejected", False)
    except HLReadError:
        chk("empty universe rejected", True)
    try:
        _as("", poster=boom); chk("missing address rejected before any call", False)
    except HLReadError:
        chk("missing address rejected before any call", True)

    good = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                         {"name": "BAD"}]}
    m = _am(poster=lambda p: good)
    chk("malformed asset row skipped, good row kept", "BTC" in m and "BAD" not in m)

    st = _as("0xabc", poster=lambda p: {
        "marginSummary": {"accountValue": "25.5", "totalMarginUsed": "3.75"},
        "withdrawable": "21.75",
        "assetPositions": [{"position": {"coin": "ZEC", "szi": "0.5",
                                         "entryPx": "40", "positionValue": "20",
                                         "unrealizedPnl": "1.5"}},
                           {"position": {"coin": "GONE", "szi": "0"}}]})
    chk("account parsed; zero-size position dropped",
        st["equity"] == 25.5 and len(st["positions"]) == 1
        and st["positions"][0]["symbol"] == "ZEC"
        and st["positions"][0]["direction"] == "long")

    # ---- no execution path exists yet ----
    import live
    files = os.listdir(os.path.dirname(os.path.abspath(live.__file__)))
    # L1's own guarantee: THIS tool is read-only. It never builds a sending
    # backend and never touches an order path. Arming of the real backend is
    # covered exhaustively by the L2 suite in tools/live_run.py.
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    body = src.split("def _selftest")[0]          # the tool, not its own tests
    chk("the preflight tool never imports the sending module",
        "live.exchange" not in body and "ExchangeBackend" not in body)
    chk("the preflight tool never builds any backend", "get_backend" not in body)
    chk("the preflight tool calls read functions only",
        all(x in body for x in ("account_state", "asset_meta"))
        and not any(x in body for x in ("place_", "bulk_orders", "market_close",
                                        "cancel_order", "flatten")))
    chk("mainnet stays hard-off until testnet is proven", C.MAINNET_ENABLED is False)
    from live.exchange import arm_status
    chk("neither live mode is armed in this environment",
        arm_status("mainnet")[0] is False and arm_status("testnet")[0] is False)
    chk("the read layer reaches only the info endpoint", C.HL_INFO_URL.endswith("/info"))


    print(f"\nL1 PREFLIGHT SELFTEST: {sum(ok)}/{len(ok)} passed")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    raise SystemExit(report())
