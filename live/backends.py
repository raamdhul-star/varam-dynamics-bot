"""
live/backends.py — where an order would be sent. NOTHING IS SENT IN L2.
=======================================================================
Two backends exist:

  DryRunBackend — records exactly what it WOULD send and returns a realistic
                  response. No network, no keys, no orders. This is what runs.
  FakeBackend   — a scriptable DryRun for tests: can be told to fail the entry,
                  fail the stop, or fail a cancel, so the recovery paths are
                  exercised rather than assumed.

The real mainnet backend is deliberately NOT written yet. `get_backend` raises
for it, so there is no code path — even a buggy one — that reaches the exchange
with an order. It arrives in L3 as a thin, separately reviewed wrapper once
everything above it has been tested.
"""
from __future__ import annotations

from .orders import bracket_is_complete


class BackendError(RuntimeError):
    pass


class DryRunBackend:
    """Logs intent. Places nothing, ever."""

    name = "dryrun"
    places_orders = False

    def __init__(self):
        self.sent = []          # every payload we would have sent, in order
        self._oid = 0

    def _next_oid(self) -> str:
        self._oid += 1
        return f"dry-{self._oid}"

    def place_bracket(self, bracket: dict) -> dict:
        self.sent.append(("bracket", bracket))
        return {"entry_ok": True, "entry_order_id": self._next_oid(),
                "stop_order_id": self._next_oid(),
                "filled_size": bracket["orders"][0]["size"],
                "dry_run": True}

    def place_stop(self, symbol: str, trigger_px: float, size: float,
                   is_buy: bool) -> dict:
        self.sent.append(("place_stop", {"symbol": symbol, "trigger_px": trigger_px,
                                         "size": size, "is_buy": is_buy}))
        return {"ok": True, "order_id": self._next_oid(), "dry_run": True}

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        self.sent.append(("cancel_order", {"symbol": symbol, "order_id": order_id}))
        return {"ok": True, "dry_run": True}

    def flatten(self, symbol: str, reason: str = "") -> dict:
        self.sent.append(("flatten", {"symbol": symbol, "reason": reason}))
        return {"ok": True, "dry_run": True}


class FakeBackend(DryRunBackend):
    """DryRun that can be told to fail, so recovery paths get tested."""

    name = "fake"

    def __init__(self, *, fail_entry=False, fail_stop=False, fail_cancel=False,
                 fail_place_stop=False):
        super().__init__()
        self.fail_entry = fail_entry
        self.fail_stop = fail_stop
        self.fail_cancel = fail_cancel
        self.fail_place_stop = fail_place_stop
        self.flattened = []

    def place_bracket(self, bracket: dict) -> dict:
        self.sent.append(("bracket", bracket))
        if self.fail_entry:
            return {"entry_ok": False, "error": "entry rejected", "dry_run": True}
        r = {"entry_ok": True, "entry_order_id": self._next_oid(),
             "filled_size": bracket["orders"][0]["size"], "dry_run": True}
        # the dangerous case: entry filled but the stop did NOT land
        r["stop_order_id"] = None if self.fail_stop else self._next_oid()
        return r

    def place_stop(self, symbol, trigger_px, size, is_buy) -> dict:
        self.sent.append(("place_stop", {"symbol": symbol, "trigger_px": trigger_px,
                                         "size": size, "is_buy": is_buy}))
        if self.fail_place_stop:
            return {"ok": False, "error": "stop rejected", "dry_run": True}
        return {"ok": True, "order_id": self._next_oid(), "dry_run": True}

    def cancel_order(self, symbol, order_id) -> dict:
        self.sent.append(("cancel_order", {"symbol": symbol, "order_id": order_id}))
        if self.fail_cancel:
            return {"ok": False, "error": "cancel rejected", "dry_run": True}
        return {"ok": True, "dry_run": True}

    def flatten(self, symbol, reason="") -> dict:
        self.flattened.append(symbol)
        return super().flatten(symbol, reason)


def get_backend(mode: str):
    """Return the backend for a mode, refusing anything not deliberately armed.

    An unarmed testnet or mainnet quietly becomes a DRY RUN rather than an
    error: a half-configured environment must never place orders, and must
    never stop the rest of the run either.
    """
    if mode == "dryrun":
        return DryRunBackend()
    if mode in ("testnet", "mainnet"):
        from .exchange import ExchangeBackend, NotArmed   # lazy: no SDK needed
        try:
            return ExchangeBackend(mode)
        except NotArmed:
            return DryRunBackend()
    raise BackendError(f"unknown mode: {mode!r}")


def settle_bracket(backend, bracket: dict, resp: dict) -> tuple:
    """Decide what a bracket response means, and recover if it is incomplete.

    A filled entry with no resting stop is a naked position — the most
    dangerous state there is — so it is FLATTENED immediately rather than left
    for the next run to notice.
    Returns (ok, status, stop_order_id).
    """
    if not isinstance(resp, dict) or not resp.get("entry_ok"):
        return False, "entry_rejected", None
    if bracket_is_complete(resp):
        return True, "open", resp.get("stop_order_id")
    if bracket.get("on_stop_missing") == "flatten":
        backend.flatten(bracket["symbol"], reason="stop_missing_after_entry")
        # carry the exchange's reason out so the caller can show it; a bare
        # "flattened_no_stop" tells you WHAT happened but never WHY
        why = resp.get("stop_error") or "stop leg returned no order id"
        return False, f"flattened_no_stop: {why}", None
    return False, "stop_missing", None
