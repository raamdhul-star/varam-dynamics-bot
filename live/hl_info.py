"""
live/hl_info.py — READ-ONLY Hyperliquid market/account reader.
=============================================================
Only touches the public POST /info endpoint. No signing, no keys, no orders.
Reading an account needs its PUBLIC address only; a private key is never used,
never read from the environment here, and never logged.

The exchange is the source of truth: every caller must read positions from here
rather than trusting local state. Failures raise HLReadError so callers can
FAIL CLOSED (do nothing) instead of proceeding on stale assumptions.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import HL_INFO_URL, HTTP_TIMEOUT, info_url


class HLReadError(RuntimeError):
    """Any failure to read the exchange. Callers must treat this as 'halt'."""


def _post(payload: dict, url: str = HL_INFO_URL, timeout: int = HTTP_TIMEOUT):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError) as e:
        raise HLReadError(f"{payload.get('type')}: {type(e).__name__}: {e}") from e


def poster_for(mode: str = ""):
    """A reader bound to ONE network's endpoint.

    Every read in a run must go to the same network the orders go to. Reading
    mainnet while trading testnet would size testnet orders off real balances
    and reconcile testnet positions against real ones — the same class of bug
    that once let a testnet job wipe a live mainnet record.
    """
    url = info_url(mode)

    def _bound(payload: dict):
        return _post(payload, url=url)

    _bound.url = url          # so callers can log/verify which network is in use
    return _bound


def asset_meta(poster=_post) -> dict:
    """{name: {"sz_decimals": int, "max_leverage": int}} for every perp.

    Both fields are order-validity critical: size must be floored to
    sz_decimals (PUMP is 0 -> whole units only) and leverage above the asset's
    max_leverage is rejected (the book ranges 3x..40x).
    """
    raw = poster({"type": "meta"})
    universe = (raw or {}).get("universe")
    if not isinstance(universe, list) or not universe:
        raise HLReadError("meta: empty or malformed universe")
    out = {}
    for a in universe:
        try:
            out[a["name"]] = {"sz_decimals": int(a["szDecimals"]),
                              "max_leverage": int(a["maxLeverage"])}
        except (KeyError, TypeError, ValueError):
            continue                      # skip malformed rows, never guess
    if not out:
        raise HLReadError("meta: no usable assets")
    return out


def account_state(address: str, poster=_post) -> dict:
    """Read an account by PUBLIC address.

    Returns {equity, withdrawable, margin_used, positions:[{symbol, size,
    entry, notional, unrealized, leverage}]}.
    """
    if not address or not str(address).strip():
        raise HLReadError("no account address supplied")
    raw = poster({"type": "clearinghouseState", "user": str(address).strip()})
    if not isinstance(raw, dict) or "marginSummary" not in raw:
        raise HLReadError("clearinghouseState: malformed response")
    ms = raw.get("marginSummary") or {}

    def f(d, k, default=0.0):
        try:
            return float(d.get(k, default))
        except (TypeError, ValueError):
            raise HLReadError(f"clearinghouseState: bad number for {k!r}")

    positions = []
    for ap in raw.get("assetPositions") or []:
        p = (ap or {}).get("position") or {}
        try:
            sz = float(p.get("szi", 0))
        except (TypeError, ValueError):
            continue
        if sz == 0:
            continue
        positions.append({
            "symbol":     p.get("coin", "?"),
            "size":       sz,
            "direction":  "long" if sz > 0 else "short",
            "entry":      float(p.get("entryPx") or 0) or None,
            "notional":   abs(float(p.get("positionValue") or 0)),
            "unrealized": float(p.get("unrealizedPnl") or 0),
            "leverage":   ((p.get("leverage") or {}).get("value")),
        })
    return {"equity": f(ms, "accountValue"),
            "withdrawable": f(raw, "withdrawable"),
            "margin_used": f(ms, "totalMarginUsed"),
            "positions": positions}


def high_low_since(symbol: str, start_ms: int, end_ms: int,
                   poster=_post) -> dict:
    """Highest high and lowest low over a window, from 1-minute candles.

    The bot sleeps between runs. Using only the price at wake-up misses any
    move that happened and reversed while it slept -- exactly the case where a
    trade ran +5% and fell back to +2% before we looked. Trailing from this
    high-water mark instead was measured better: profit factor 3.32 vs 3.02 and
    a SHALLOWER drawdown (-14% vs -19%).

    Read-only. Returns {high, low, last}; raises HLReadError so callers keep
    the existing stop rather than acting on a guess.
    """
    raw = poster({"type": "candleSnapshot",
                  "req": {"coin": symbol, "interval": "1m",
                          "startTime": int(start_ms), "endTime": int(end_ms)}})
    if not isinstance(raw, list) or not raw:
        raise HLReadError(f"candleSnapshot {symbol}: empty")
    highs, lows, last = [], [], None
    for c in raw:
        try:
            highs.append(float(c["h"])); lows.append(float(c["l"]))
            last = float(c["c"])
        except (KeyError, TypeError, ValueError):
            continue
    if not highs or last is None:
        raise HLReadError(f"candleSnapshot {symbol}: no usable candles")
    return {"high": max(highs), "low": min(lows), "last": last}


def open_orders(address: str, poster=_post) -> list:
    """Resting orders for an account, including trigger (stop) orders.

    Needed because Hyperliquid answers a newly placed trigger order with the
    bare status "waitingForTrigger" and NO order id. The order is live and
    armed; the id simply has to be looked up afterwards so the trailing step
    can cancel and replace it later.
    """
    if not address or not str(address).strip():
        raise HLReadError("no account address supplied")
    raw = poster({"type": "frontendOpenOrders", "user": str(address).strip()})
    if raw is None:
        raise HLReadError("frontendOpenOrders: no response")
    if not isinstance(raw, list):
        raise HLReadError("frontendOpenOrders: malformed response")
    out = []
    for o in raw:
        if not isinstance(o, dict):
            continue
        try:
            out.append({"symbol": o.get("coin", "?"),
                        "order_id": str(o.get("oid")) if o.get("oid") else None,
                        "size": float(o.get("sz") or 0),
                        "limit_px": float(o.get("limitPx") or 0) or None,
                        "trigger_px": float(o["triggerPx"]) if o.get("triggerPx") else None,
                        "reduce_only": bool(o.get("reduceOnly")),
                        "is_trigger": bool(o.get("isTrigger") or o.get("triggerPx")),
                        "side": o.get("side")})
        except (TypeError, ValueError):
            continue
    return out


def find_stop_order(orders: list, symbol: str, trigger_px: float,
                    tol_pct: float = 0.5):
    """The resting reduce-only STOP for a symbol, matched on trigger price.

    Matching on price rather than assuming order means a stop placed by an
    earlier run, or by hand, is still found and managed rather than orphaned.
    """
    best, best_gap = None, None
    for o in orders or []:
        if str(o.get("symbol", "")).upper() != str(symbol).upper():
            continue
        if not o.get("reduce_only") or not o.get("order_id"):
            continue
        px = o.get("trigger_px") or o.get("limit_px")
        if not px or not trigger_px:
            continue
        gap = abs(px - trigger_px) / trigger_px * 100
        if gap <= tol_pct and (best_gap is None or gap < best_gap):
            best, best_gap = o, gap
    return best


def mids(poster=_post) -> dict:
    """{symbol: mid price}. Used only to price and sanity-check, never to trade."""
    raw = poster({"type": "allMids"})
    if not isinstance(raw, dict) or not raw:
        raise HLReadError("allMids: empty response")
    out = {}
    for k, v in raw.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    if not out:
        raise HLReadError("allMids: no usable prices")
    return out
