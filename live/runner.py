"""
live/runner.py — one pass of the live loop. Places nothing in L2.
=================================================================
Order of work, fail-closed at every step:

  1. kill switch / mode
  2. read the exchange (equity + positions). A failed read STOPS the run —
     an unread exchange is not an empty one.
  3. reconcile every symbol we know about against what the exchange reports
  4. move trailing stops on healthy positions (place-then-cancel)
  5. consider new signals, cheapest gate first
  6. write an audit line for everything, including every refusal

Step 4 runs BEFORE step 5 on purpose: protecting money already at risk matters
more than deploying more of it. If the run dies halfway, the stops moved.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import config as C
from . import gates, notify, state
from .backends import get_backend, settle_bracket
from .hl_info import (HLReadError, account_state, asset_meta, closing_fill,
                      high_low_since, mids, open_orders, poster_for,
                      recent_fills, resting_stops)
from .orders import build_bracket
from .reconcile import CLOSED_AWAY, EXPIRED, PROCEED, reconcile, summarize
from .sizing import plan_order
from .trailing import plan_stop_move


def _margin_of(rec: dict):
    """Margin behind a recorded position, for the P&L-on-margin line."""
    try:
        lev = int(rec.get("leverage") or 1)
        return abs(float(rec["size"]) * float(rec["entry"])) / max(1, lev)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def run_once(*, signals: list, mode: str | None = None, backend=None,
             reader=None, now: datetime | None = None,
             root: str | None = None, max_new: int | None = None) -> dict:
    """One pass. `reader` returns (meta, account, prices[, peaks]) and is
    injectable so the whole flow can be tested offline.

    `max_new` caps how many NEW positions this run may open. Trailing stops on
    existing positions are deliberately NOT capped — protecting money already
    at risk is not something to ration. Used for the first supervised live run
    (--max-trades 1), so exactly one order exists to inspect on the exchange
    before anything gets automated.
    """
    now = now or datetime.now(timezone.utc)
    mode = mode or C.mode()
    out = {"mode": mode, "placed": [], "skipped": [], "stop_moves": [],
           "attention": [], "holding": [], "notifications": [], "halted": None}

    if gates.halted():
        out["halted"] = "kill_switch_engaged"
        state.audit(mode, "halt", {"reason": out["halted"]}, root, now)
        return out

    # ── 2. read the exchange ────────────────────────────────────────────────
    try:
        peaks = None
        fills = None
        if reader is not None:
            got = reader()
            meta, acct, prices = got[0], got[1], got[2]
            peaks = got[3] if len(got) > 3 else None
            fills = got[4] if len(got) > 4 else None
        else:
            # Read the SAME network we trade on. A testnet run must never size
            # or reconcile against real-money balances.
            read = poster_for(mode)
            addr = C.account_address(mode)
            if not addr:
                raise HLReadError(f"no account address set for mode {mode!r}")
            state.audit(mode, "reading", {"endpoint": read.url,
                                          "address": addr[:6] + "..." + addr[-4:]},
                        root, now)
            meta = asset_meta(poster=read)
            acct = account_state(addr, poster=read)
            prices = mids(poster=read)
            # Which positions actually have a stop resting on them. Without
            # this every position looks naked and the "needs attention" alarm
            # fires on every run — which would hide a real naked position.
            acct["resting_stops"] = resting_stops(open_orders(addr, poster=read))
            # For exit alerts: the exchange's own realised P&L. Non-fatal —
            # a missing fill costs a number in a message, never a trade.
            try:
                fills = recent_fills(addr, poster=read)
            except HLReadError as e:
                state.audit(mode, "fills_read_failed", {"error": str(e)}, root, now)
            # high/low since we last looked, for the trailing step below.
            # A failure here is NOT fatal: we fall back to the spot price,
            # which can only ever leave the stop where it already is.
            peaks = {}
            end_ms = int(now.timestamp() * 1000)
            start_ms = end_ms - int(C.PEAK_LOOKBACK_MIN * 60 * 1000)
            for p in acct.get("positions") or []:
                try:
                    peaks[p["symbol"]] = high_low_since(p["symbol"], start_ms,
                                                        end_ms, poster=read)
                except HLReadError as e:
                    state.audit(mode, "peak_read_failed",
                                {"symbol": p.get("symbol"), "error": str(e)},
                                root, now)
    except HLReadError as e:
        out["halted"] = f"exchange_read_failed: {e}"
        state.audit(mode, "halt", {"reason": out["halted"]}, root, now)
        return out

    equity = acct.get("equity", 0.0)
    ok, reason = gates.all_clear(mode=mode, equity=equity)
    if not ok:
        out["halted"] = reason
        state.audit(mode, "halt", {"reason": reason, "equity": equity}, root, now)
        return out

    positions = state.load_positions(mode, root)
    on_exch = {p["symbol"]: p for p in acct.get("positions") or []}
    stops = acct.get("resting_stops") or {}       # {symbol: order_id}

    # ── 3. reconcile ────────────────────────────────────────────────────────
    resolutions = []
    for sym in sorted(set(positions) | set(on_exch)):
        rec = positions.get(sym)
        age = 0.0
        if rec and rec.get("status") == "pending":
            try:
                opened = datetime.fromisoformat(rec["opened_at"])
                age = (now - opened).total_seconds() / 3600
            except (KeyError, TypeError, ValueError):
                age = 0.0
        r = reconcile(symbol=sym, local=rec, exch_position=on_exch.get(sym),
                      has_resting_stop=bool(stops.get(sym)), read_ok=True,
                      pending_age_hours=age,
                      pending_timeout_hours=C.PENDING_TIMEOUT_HOURS,
                      in_book=sym in meta)
        resolutions.append(r)
        state.audit(mode, "reconcile", {"symbol": sym, "outcome": r.outcome,
                                        "detail": r.detail}, root, now)
        if r.outcome in (CLOSED_AWAY, EXPIRED):
            # A position that closed while we were away is a real event worth
            # a message. The exchange's own realised P&L is used — never a
            # figure re-derived from an assumed stop fill.
            if r.outcome == CLOSED_AWAY and rec:
                fill = None
                if fills is not None:
                    fill = closing_fill(fills, sym)
                out["notifications"].append(notify.build_closed(
                    symbol=sym, direction=rec.get("direction", ""),
                    leverage=rec.get("leverage", 1), entry=rec.get("entry"),
                    exit_px=(fill or {}).get("price") or rec.get("stop"),
                    pnl_usd=(fill or {}).get("closed_pnl"),
                    reason=(fill or {}).get("dir") or "closed on the exchange",
                    margin=_margin_of(rec), account_value=equity))
            positions.pop(sym, None)              # stale record, drop it
        elif r.outcome == PROCEED:
            positions.pop(sym, None)
        if r.needs_attention:
            out["notifications"].append(notify.build_attention(
                symbol=sym, reason=r.outcome.replace("_", " "), detail=r.detail))
    summary = summarize(resolutions)
    out["attention"] = summary["attention"]
    blocked = set(summary["blocked"])

    # The exchange is the source of truth about what we paid. A record written
    # before a fix, or adopted from a position we did not open, can disagree —
    # and the trail measures profit from this number, so a stale entry trails
    # at the wrong moment on every subsequent run. Reconcile it.
    for sym, rec in positions.items():
        live_entry = (on_exch.get(sym) or {}).get("entry")
        if not live_entry or not rec.get("entry"):
            continue
        if abs(float(live_entry) - float(rec["entry"])) / float(live_entry) > 1e-6:
            state.audit(mode, "entry_corrected",
                        {"symbol": sym, "was": rec["entry"], "now": live_entry,
                         "note": "exchange is the truth"}, root, now)
            rec["entry"] = float(live_entry)

    # ── 4. move trailing stops (before opening anything new) ────────────────
    # Trail from the HIGH-WATER MARK since we last looked, not from the price
    # right now. The bot sleeps for an hour; a trade that ran +5% and fell back
    # to +2% in that time is invisible to a spot check, and we would leave the
    # stop far below a peak that really happened. Measured better AND safer:
    # profit factor 3.32 vs 3.02, drawdown -14% vs -19%.
    # trailing.next_stop still refuses any stop at or through the current
    # price, so a big missed spike can never fire us out at market.
    for sym, rec in list(positions.items()):
        if rec.get("status") != "open" or sym not in on_exch:
            continue
        px = prices.get(sym)
        if px is None:
            continue
        peak = px
        if peaks is not None:
            hl = peaks.get(sym)
            if hl:
                peak = hl["high"] if rec["direction"] == "long" else hl["low"]
        move = plan_stop_move(symbol=sym, entry=rec["entry"], price=peak,
                              current_price=px,
                              direction=rec["direction"],
                              current_stop=rec["stop"],
                              old_order_id=rec.get("stop_order_id"),
                              moved_to_breakeven=rec.get("moved_to_breakeven", False),
                              trigger=C.trail_trigger(mode))
        if move is None:
            continue
        m = meta.get(sym) or {}
        # PLACE FIRST: if this fails, the old stop is still resting.
        placed = backend.place_stop(sym, move.new_stop, rec["size"],
                                    is_buy=(rec["direction"] != "long"))
        if not placed.get("ok"):
            state.audit(mode, "stop_move_failed",
                        {"symbol": sym, "from": move.old_stop, "to": move.new_stop,
                         "note": "old stop still resting — still protected"}, root, now)
            continue
        if move.old_order_id:
            backend.cancel_order(sym, move.old_order_id)
        else:
            # The old stop's id was never known (Hyperliquid answers a new
            # trigger order with "waitingForTrigger" and no id). Sweep any
            # other reduce-only stop on this symbol so they do not pile up one
            # per hour. Only ever runs AFTER the new stop is confirmed placed.
            sweep = getattr(backend, "cancel_stale_stops", None)
            if sweep:
                res_sweep = sweep(sym, keep_order_id=placed.get("order_id"))
                if res_sweep.get("cancelled"):
                    state.audit(mode, "stale_stops_cancelled",
                                {"symbol": sym, "n": res_sweep["cancelled"]},
                                root, now)
        rec["stop"] = move.new_stop
        rec["stop_order_id"] = placed.get("order_id")
        rec["moved_to_breakeven"] = True
        rec["updated_at"] = now.isoformat()
        out["stop_moves"].append({"symbol": sym, "from": move.old_stop,
                                  "to": move.new_stop, "reason": move.reason})
        state.audit(mode, "stop_moved", out["stop_moves"][-1], root, now)

    # ── 5. new signals ──────────────────────────────────────────────────────
    seen = {r.get("fingerprint") for r in positions.values() if r.get("fingerprint")}
    used = sum(abs(p.get("notional", 0)) / max(1, p.get("leverage") or 1)
               for p in on_exch.values())

    def skip(sym, why):
        out["skipped"].append({"symbol": sym, "reason": why})
        state.audit(mode, "skip", {"symbol": sym, "reason": why}, root, now)

    for sig in signals or []:
        sym = sig.get("symbol", "?")
        if max_new is not None and len(out["placed"]) >= max_new:
            skip(sym, "run_trade_cap_reached"); continue
        if not gates.score_ok(sig.get("score")):
            skip(sym, "below_score_floor"); continue
        fp = state.fingerprint(sig)
        if gates.is_duplicate(fp, seen):
            skip(sym, "duplicate_signal"); continue
        if gates.symbol_busy(sym, blocked) or sym in positions:
            skip(sym, "symbol_already_live"); continue
        if not gates.capacity_left(len(positions), used, equity):
            skip(sym, "no_capacity"); continue

        m = meta.get(sym)
        if not m:
            skip(sym, "asset_not_in_book"); continue
        plan = plan_order(symbol=sym, direction=sig.get("direction", ""),
                          entry=sig.get("entry"),
                          # Two shapes reach here: raw batch signals use "sl",
                          # recent_calls() normalises to "stop". Accept both —
                          # reading only one silently skipped EVERY call.
                          stop=sig.get("stop", sig.get("sl")),
                          equity=equity, used_margin=used,
                          sz_decimals=m["sz_decimals"],
                          asset_max_leverage=m["max_leverage"],
                          # size off the price NOW, not the hour-old signal
                          mark_price=prices.get(sym))
        if not plan.ok:
            skip(sym, plan.skip_reason); continue

        bracket = build_bracket(plan, m["sz_decimals"])
        if bracket is None:
            skip(sym, "bracket_unbuildable"); continue

        resp = backend.place_bracket(bracket)
        ok_b, status, stop_oid = settle_bracket(backend, bracket, resp)
        if not ok_b:
            # An entry that filled and then had to be flattened is NOT a quiet
            # skip — money moved and a fee was paid. Record everything the
            # exchange said, including the exact request we sent.
            if status.startswith("flattened_no_stop"):
                state.audit(mode, "flattened_no_stop", {
                    "symbol": sym, "error": resp.get("stop_error"),
                    "stop_status": resp.get("stop_status"),
                    "sent": resp.get("sent_stop_request"),
                    "bracket": bracket}, root, now)
                out["attention"].append(sym)
            skip(sym, status); continue

        # Record the price actually PAID where the exchange reports it. The
        # trail measures profit from this number, so using the stale signal
        # price would move the stop at the wrong moment on every trade.
        actual_entry = resp.get("fill_price") or plan.entry
        positions[sym] = state.record(
            sym, direction=plan.direction, size=plan.size, entry=actual_entry,
            stop=plan.stop, leverage=plan.leverage, stop_order_id=stop_oid,
            fingerprint=fp, now=now)
        used += plan.margin
        seen.add(fp)
        out["notifications"].append(notify.build_opened(
            symbol=sym, direction=plan.direction, leverage=plan.leverage,
            entry=actual_entry, stop=plan.stop, target=sig.get("tp"),
            size=plan.size, notional=plan.notional, margin=plan.margin))
        out["placed"].append({"symbol": sym, "size": plan.size, "entry": plan.entry,
                              "stop": plan.stop, "leverage": plan.leverage,
                              "margin": round(plan.margin, 2),
                              "notional": round(plan.notional, 2)})
        state.audit(mode, "placed", out["placed"][-1], root, now)

    # What we are actually holding, and whether each position is protected.
    # Without this the output looks identical whether we hold a healthy
    # position or nothing at all — silence meant both.
    for sym, p in sorted(on_exch.items()):
        rec = positions.get(sym) or {}
        px = prices.get(sym)
        entry = rec.get("entry") or p.get("entry")
        # Derive direction from the size sign when the exchange payload omits
        # it — a negative size IS a short, so never depend on a label.
        side = p.get("direction") or rec.get("direction")
        if not side:
            try:
                side = "long" if float(p.get("size") or 0) >= 0 else "short"
            except (TypeError, ValueError):
                side = "long"
        pnl = None
        if px and entry:
            pnl = ((px - entry) / entry * 100 if side == "long"
                   else (entry - px) / entry * 100)
        out["holding"].append({
            "symbol": sym, "direction": side, "size": p.get("size"),
            "entry": entry, "price": px, "pnl_pct": pnl,
            "stop": rec.get("stop"),
            "protected": bool(stops.get(sym)),
            "stop_order_id": stops.get(sym),
            "unrealized": p.get("unrealized"),
        })

    state.save_positions(mode, positions, root)
    return out
