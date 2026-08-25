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
from datetime import datetime, timezone

PUBLIC_STATE_URL = ("https://raw.githubusercontent.com/raamdhul-star/"
                    "varam-dynamics-bot/main/results/telegram_state.json")

DEFAULT_LOCAL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "results", "telegram_state.json")

HTTP_TIMEOUT = 20

# How fresh a signal must be to be actionable. The live runner wakes hourly and
# GitHub's scheduler drifts by up to half an hour, so anything inside ~90
# minutes is plausibly from the latest scan. Older than that and the setup the
# scorer saw has usually gone.
MAX_SIGNAL_AGE_MIN = 90.0


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


def _batch_age_min(batch: dict, now: datetime | None = None) -> float | None:
    """Minutes since this batch of signals was alerted. None if unparseable."""
    try:
        t = str((batch or {}).get("time") or "")
        when = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return ((now or datetime.now(timezone.utc)) - when).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def recent_calls(source: str | None = None, limit: int = 40,
                 max_age_min: float | None = MAX_SIGNAL_AGE_MIN,
                 now: datetime | None = None) -> list:
    """Recent alerted signals, newest first, de-duplicated, and FRESH.

    Freshness matters more than it looks. `batches` is a rolling cap of the last
    dozen alerts, which in a quiet market can span more than a day — measured
    live at 16 to 28 hours old. Every run was re-evaluating yesterday's calls,
    which all failed the 1% drift check, reporting "price moved" when the truth
    was "this signal is from yesterday". Rejecting on AGE says what is actually
    wrong, and stops the drift guard being used as a staleness proxy.

    An unparseable timestamp is treated as too old. A signal we cannot date is
    a signal we cannot trust.

    Emits BOTH `stop` and `sl` for the stop price. They are the same number
    under two names, and reading only one of them once made every single call
    skip as malformed — so the shape now satisfies either reader.
    """
    d = _load(source or DEFAULT_LOCAL)
    out, seen = [], set()
    for mid in sorted((d.get("batches") or {}), key=lambda k: str(k), reverse=True):
        batch = d["batches"][mid] or {}
        if max_age_min is not None:
            age = _batch_age_min(batch, now)
            if age is None or age > max_age_min:
                continue
        for s in batch.get("sigs") or []:
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


def describe_source(source: str | None = None, max_age_min: float | None = MAX_SIGNAL_AGE_MIN,
                    now: datetime | None = None) -> str:
    """One line on what the signal file holds, so an empty run explains itself
    instead of looking like a failure."""
    d = _load(source or DEFAULT_LOCAL)
    batches = d.get("batches") or {}
    if not batches:
        return "no signal batches found"
    ages = [a for a in (_batch_age_min(b, now) for b in batches.values())
            if a is not None]
    if not ages:
        return f"{len(batches)} batches, none with a usable timestamp"
    fresh = sum(1 for a in ages if a <= (max_age_min or float("inf")))
    return (f"{len(batches)} batches, newest {min(ages):.0f} min old, "
            f"{fresh} within the {max_age_min:.0f} min freshness window")


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
