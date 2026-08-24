"""
live/exchange.py — the ONLY module that can place an order.
===========================================================
Deliberately thin. Every decision worth arguing about was already made and
tested upstream (sizing, geometry, brackets, trailing, reconciliation); this
just translates a finished payload into a signed call and reports honestly
what came back.

Safety properties, all enforced here rather than assumed of the caller:

  * Refuses to construct at all unless the mode is properly ARMED. Mainnet
    needs config.MAINNET_ENABLED True in reviewed code AND the confirm phrase.
    Testnet needs its own separate flag. Neither can stand in for the other.
  * Testnet and mainnet read DIFFERENT credentials and hit DIFFERENT hosts, so
    a testnet run cannot touch real money even if it is misconfigured.
  * The private key is read at the moment of use, handed straight to the
    signer, and never stored on the instance, logged, printed or returned.
  * A filled entry whose stop did NOT land is FLATTENED immediately. An
    unprotected position is the worst state in the system.

The Hyperliquid SDK is imported lazily so the rest of the package — and the
whole test suite — works without it installed.
"""
from __future__ import annotations

from . import config as C


class ExchangeError(RuntimeError):
    """Anything that stopped an order being placed or confirmed."""


class NotArmed(ExchangeError):
    """The mode is not deliberately armed. Never a reason to 'try anyway'."""


def _sdk():
    """Import the signing SDK only when an order is actually being placed."""
    try:
        from eth_account import Account                      # noqa: WPS433
        from hyperliquid.exchange import Exchange            # noqa: WPS433
    except ImportError as e:                                  # pragma: no cover
        raise ExchangeError(
            "hyperliquid-python-sdk and eth-account are required to place "
            f"orders (pip install hyperliquid-python-sdk): {e}") from e
    return Account, Exchange


def arm_status(mode: str) -> tuple:
    """(armed, reason). The single place that decides whether orders may flow."""
    if mode not in ("testnet", "mainnet"):
        return False, f"mode {mode!r} never places orders"
    if mode == "mainnet" and not C.MAINNET_ENABLED:
        return False, "MAINNET_ENABLED is False in reviewed code"
    if C.mode() != mode:
        return False, f"environment does not arm {mode} (resolved to {C.mode()!r})"
    if not C.credentials_present(mode):
        return False, f"{mode} credentials are not set"
    return True, "armed"


class ExchangeBackend:
    """Places real orders. Only constructible when properly armed."""

    places_orders = True

    def __init__(self, mode: str, client=None):
        armed, reason = arm_status(mode)
        if not armed:
            raise NotArmed(reason)
        self.name = mode
        self.mode = mode
        self.sent = []
        self._client = client          # injectable for tests; never a real key
        if client is None:
            Account, Exchange = _sdk()
            key, addr = C.credentials(mode)
            # The key goes straight into the signer and is not kept anywhere.
            self._client = Exchange(Account.from_key(key),
                                    C.HL_BASE_URL[mode], account_address=addr)
            del key

    # ── helpers ─────────────────────────────────────────────────────────────
    @staticmethod
    def _order_req(o: dict) -> dict:
        """Our payload -> the SDK's order request shape."""
        if o["action"] == "entry":
            return {"coin": o["symbol"], "is_buy": o["is_buy"], "sz": o["size"],
                    "limit_px": o["limit_px"], "reduce_only": False,
                    "order_type": {"limit": {"tif": o.get("tif", "Ioc")}}}
        return {"coin": o["symbol"], "is_buy": o["is_buy"], "sz": o["size"],
                "limit_px": o["trigger_px"], "reduce_only": True,
                "order_type": {"trigger": {"triggerPx": o["trigger_px"],
                                           "isMarket": True, "tpsl": "sl"}}}

    @staticmethod
    def _statuses(resp) -> list:
        try:
            return resp["response"]["data"]["statuses"]
        except (KeyError, TypeError):
            return []

    @classmethod
    def _oid(cls, status) -> str | None:
        if not isinstance(status, dict):
            return None            # e.g. "waitingForTrigger" — accepted, no id
        for k in ("resting", "filled"):
            if isinstance(status.get(k), dict) and status[k].get("oid"):
                return str(status[k]["oid"])
        return None

    @staticmethod
    def _errored(status) -> bool:
        return isinstance(status, dict) and "error" in status

    @classmethod
    def _accepted(cls, status) -> bool:
        """Did the exchange ACCEPT this leg?

        A newly placed trigger order comes back as the bare string
        "waitingForTrigger" — armed and live, but with no order id. Treating a
        missing id as failure made the code flatten three perfectly good
        positions on testnet. Acceptance and having-an-id are different
        questions and are now asked separately.
        """
        if cls._errored(status):
            return False
        if isinstance(status, str):
            return "error" not in status.lower()
        return bool(cls._oid(status))

    # ── actions ─────────────────────────────────────────────────────────────
    def place_bracket(self, bracket: dict) -> dict:
        """Set leverage, then send entry + stop together. Reports what landed;
        it does NOT decide what to do about a partial result — backends.
        settle_bracket owns that, so the recovery rule lives in one place."""
        sym = bracket["symbol"]
        entry, stop = bracket["orders"][0], bracket["orders"][1]
        self.sent.append(("bracket", bracket))
        try:
            self._client.update_leverage(int(entry["leverage"]), sym)
        except Exception as e:                                # noqa: BLE001
            return {"entry_ok": False, "error": f"set leverage: {e}"}
        try:
            resp = self._client.bulk_orders(
                [self._order_req(entry), self._order_req(stop)],
                grouping=bracket.get("grouping", "normalTpsl"))
        except Exception as e:                                # noqa: BLE001
            return {"entry_ok": False, "error": f"bulk_orders: {e}"}

        st = self._statuses(resp)
        if len(st) < 2 or self._errored(st[0]):
            err = st[0].get("error") if st and self._errored(st[0]) else "no status"
            return {"entry_ok": False, "error": str(err), "raw": resp}
        filled = 0.0
        try:
            filled = float(st[0]["filled"]["totalSz"])
        except (KeyError, TypeError, ValueError):
            filled = entry["size"]
        # The price we ACTUALLY paid. The trailing stop measures profit from
        # the recorded entry, so recording the hour-old signal price instead
        # would trigger the trail at the wrong point on every trade.
        fill_px = None
        try:
            fill_px = float(st[0]["filled"]["avgPx"])
        except (KeyError, TypeError, ValueError):
            fill_px = None
        # stop_order_id stays None if that leg errored -> settle_bracket flattens.
        # Keep the exchange's OWN words about why: without them a failed stop
        # is indistinguishable from a dozen different causes.
        stop_err = None
        if not self._accepted(st[1]):
            stop_err = (str(st[1].get("error")) if self._errored(st[1])
                        else f"stop rejected: {st[1]!r}")
        stop_oid = self._oid(st[1])
        if stop_err is None and not stop_oid:
            # Accepted but no id (the "waitingForTrigger" case). The stop IS
            # live; look its id up so the trailing step can replace it later.
            stop_oid = self._lookup_stop_oid(bracket["symbol"], stop["trigger_px"])
        return {"entry_ok": True, "entry_order_id": self._oid(st[0]),
                "stop_order_id": stop_oid,
                "stop_live": stop_err is None,
                "stop_error": stop_err, "stop_status": st[1],
                "sent_stop_request": self._order_req(stop),
                "filled_size": filled, "fill_price": fill_px, "raw": resp}

    def _lookup_stop_oid(self, symbol: str, trigger_px: float):
        """Best effort. A missing id is NOT a missing stop — never flatten on it."""
        try:
            from .hl_info import find_stop_order, open_orders, poster_for
            addr = C.account_address(self.mode)
            found = find_stop_order(open_orders(addr, poster=poster_for(self.mode)),
                                    symbol, trigger_px)
            return found["order_id"] if found else None
        except Exception:                                     # noqa: BLE001
            return None

    def place_stop(self, symbol: str, trigger_px: float, size: float,
                   is_buy: bool) -> dict:
        self.sent.append(("place_stop", {"symbol": symbol, "trigger_px": trigger_px,
                                         "size": size, "is_buy": is_buy}))
        try:
            resp = self._client.order(
                symbol, is_buy, size, trigger_px,
                {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True)
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "error": str(e)}
        st = self._statuses(resp)
        if not st or self._errored(st[0]):
            return {"ok": False, "error": str(st[0].get("error") if st else "no status")}
        return {"ok": True, "order_id": self._oid(st[0]), "raw": resp}

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        self.sent.append(("cancel_order", {"symbol": symbol, "order_id": order_id}))
        try:
            resp = self._client.cancel(symbol, int(order_id))
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": (resp or {}).get("status") == "ok", "raw": resp}

    def cancel_stale_stops(self, symbol: str, keep_order_id=None) -> dict:
        """Cancel every resting reduce-only stop on `symbol` except the newest.

        The trailing step places the new stop BEFORE cancelling the old one, so
        the position is never unprotected. When the old stop's id was never
        known (the "waitingForTrigger" case) there is nothing to cancel by id,
        and stale stops would otherwise pile up one per hour. This sweeps them
        by reading what is actually resting. Best effort: a failure here leaves
        extra reduce-only stops, which is untidy but never dangerous.
        """
        try:
            from .hl_info import open_orders, poster_for
            addr = C.account_address(self.mode)
            orders = open_orders(addr, poster=poster_for(self.mode))
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "error": str(e), "cancelled": 0}
        n = 0
        for o in orders:
            if str(o.get("symbol", "")).upper() != str(symbol).upper():
                continue
            if not o.get("reduce_only") or not o.get("order_id"):
                continue
            if keep_order_id and str(o["order_id"]) == str(keep_order_id):
                continue
            if self.cancel_order(symbol, o["order_id"]).get("ok"):
                n += 1
        return {"ok": True, "cancelled": n}

    def flatten(self, symbol: str, reason: str = "") -> dict:
        """Close the position at market. Used when a stop failed to attach, so
        it must not itself depend on a stop existing."""
        self.sent.append(("flatten", {"symbol": symbol, "reason": reason}))
        try:
            resp = self._client.market_close(symbol)
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "error": str(e), "symbol": symbol}
        return {"ok": True, "raw": resp, "symbol": symbol}
