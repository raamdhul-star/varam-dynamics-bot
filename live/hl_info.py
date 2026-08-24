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

from .config import HL_INFO_URL, HTTP_TIMEOUT


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
