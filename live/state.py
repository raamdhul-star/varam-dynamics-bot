"""
live/state.py — local record of what the bot opened, plus an audit log.
=======================================================================
NETWORK-SCOPED: results/live/<mode>/positions.json. A dryrun run must never
read or clear a mainnet record. In btc-paper-bot a shared state file let the
testnet job wipe the record of a REAL open mainnet position, leaving the bot
blind to its own trade for days. The directory split is the actual protection,
so it is not optional and not configurable.

This is a convenience cache, never the truth. The exchange is the truth; see
reconcile.py. Nothing here decides anything.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "results")


def base_dir(mode: str, root: str | None = None) -> str:
    """results/live/<mode>/ — separate per network, always."""
    safe = (mode or "dryrun").strip().lower()
    if safe not in ("dryrun", "testnet", "mainnet"):
        safe = "dryrun"
    return os.path.join(root or RESULTS, "live", safe)


def _pos_path(mode: str, root: str | None = None) -> str:
    return os.path.join(base_dir(mode, root), "positions.json")


def load_positions(mode: str, root: str | None = None) -> dict:
    """{symbol: record}. Unreadable state returns {} — callers must still treat
    the exchange as the truth, so an empty cache is safe, never authoritative."""
    try:
        with open(_pos_path(mode, root)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_positions(mode: str, positions: dict, root: str | None = None) -> None:
    d = base_dir(mode, root)
    os.makedirs(d, exist_ok=True)
    tmp = _pos_path(mode, root) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(positions, f, indent=1, default=str)
    os.replace(tmp, _pos_path(mode, root))       # atomic: never a half-written file


def record(symbol: str, *, direction: str, size: float, entry: float, stop: float,
           leverage: int, stop_order_id=None, status: str = "open",
           fingerprint: str = "", now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {"symbol": symbol, "direction": direction, "size": float(size),
            "entry": float(entry), "stop": float(stop), "leverage": int(leverage),
            "stop_order_id": stop_order_id, "status": status,
            "moved_to_breakeven": False, "fingerprint": fingerprint,
            "opened_at": now.isoformat(), "updated_at": now.isoformat()}


def audit(mode: str, event: str, data: dict, root: str | None = None,
          now: datetime | None = None) -> dict:
    """Append-only JSONL. Every decision, including every refusal, lands here so
    a bad run can be reconstructed afterwards. Audit failure never breaks a run."""
    rec = {"ts": (now or datetime.now(timezone.utc)).isoformat(),
           "mode": mode, "event": event, **data}
    try:
        d = base_dir(mode, root)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "audit.jsonl"), "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass
    return rec


def fingerprint(sig: dict) -> str:
    """Identifies one signal on one candle, so the same call is never entered
    twice across runs."""
    return "|".join(str(sig.get(k, "")) for k in
                    ("symbol", "direction", "interval", "bar_time"))
