"""
live/signals.py — where live trading gets its calls from. READ-ONLY.
====================================================================
The signals are produced by the PUBLIC scanner repo and committed to
results/telegram_state.json on every scan. Live trading reads them from there
and never generates its own — so live and paper always act on the identical
calls, which is the only thing that makes comparing them honest.

Two sources, same shape:
  * a local path  — when live runs inside the scanner repo
  * an https URL  — when live runs from its own PRIVATE repo and pulls the
                    calls from the public one (no credentials: it is public)

Nothing here decides anything. A failure returns [] and the caller simply has
no calls to consider, which is the safe outcome.
"""
from __future__ import annotations

import json
import os
import urllib.request

PUBLIC_STATE_URL = ("https://raw.githubusercontent.com/raamdhul-star/"
                    "varam-dynamics-bot/main/results/telegram_state.json")

DEFAULT_LOCAL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "results", "telegram_state.json")

HTTP_TIMEOUT = 20


def _load(source: str) -> dict:
    """Read the scanner state from a path or an https URL. {} on any failure."""
    try:
        if str(source).startswith(("http://", "https://")):
            with urllib.request.urlopen(str(source), timeout=HTTP_TIMEOUT) as r:
                d = json.load(r)
        else:
            with open(source, encoding="utf-8") as f:
                d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                                      # noqa: BLE001
        return {}


def recent_calls(source: str | None = None, limit: int = 40) -> list:
    """Most recent alerted signals, newest first, de-duplicated.

    Emits BOTH `stop` and `sl` for the stop price. They are the same number
    under two names, and reading only one of them once made every single call
    skip as malformed — so the shape now satisfies either reader.
    """
    d = _load(source or DEFAULT_LOCAL)
    out, seen = [], set()
    for mid in sorted((d.get("batches") or {}), key=lambda k: str(k), reverse=True):
        for s in (d["batches"][mid] or {}).get("sigs") or []:
            try:
                key = (s["symbol"], s["direction"], s.get("interval"))
                if key in seen:
                    continue
                seen.add(key)
                stop = float(s["sl"])
                out.append({"symbol": s["symbol"], "direction": s["direction"],
                            "entry": float(s["entry"]), "stop": stop, "sl": stop,
                            "tp": float(s["tp"]) if s.get("tp") else None,
                            "score": float(s.get("score") or 0),
                            "interval": s.get("interval", "?"),
                            "bar_time": s.get("bar_time", "")})
            except (KeyError, TypeError, ValueError):
                continue
            if len(out) >= limit:
                return out
    return out


def source_for(mode: str = "") -> str:
    """Local file when present, otherwise the public repo over https.

    LIVE_SIGNALS_URL overrides both, so the private repo can point somewhere
    else without a code change.
    """
    override = (os.environ.get("LIVE_SIGNALS_URL", "") or "").strip()
    if override:
        return override
    if os.path.exists(DEFAULT_LOCAL):
        return DEFAULT_LOCAL
    return PUBLIC_STATE_URL
