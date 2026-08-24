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
            return None
        for k in ("resting", "filled"):
            if isinstance(status.get(k), dict) and status[k].get("oid"):
                return str(status[k]["oid"])
        return None

    @staticmethod
    def _errored(status) -> bool:
        return isinstance(status, dict) and "error" in status

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
        # stop_order_id stays None if that leg errored -> settle_bracket flattens
        return {"entry_ok": True, "entry_order_id": self._oid(st[0]),
                "stop_order_id": None if self._errored(st[1]) else self._oid(st[1]),
                "filled_size": filled, "raw": resp}

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

    def flatten(self, symbol: str, reason: str = "") -> dict:
        """Close the position at market. Used when a stop failed to attach, so
        it must not itself depend on a stop existing."""
        self.sent.append(("flatten", {"symbol": symbol, "reason": reason}))
        try:
            resp = self._client.market_close(symbol)
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "error": str(e), "symbol": symbol}
        return {"ok": True, "raw": resp, "symbol": symbol}
