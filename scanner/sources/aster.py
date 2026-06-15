"""
scanner/sources/aster.py — Aster public market-data adapter (Sprint D2)
=======================================================================
Read-only adapter for Aster's PUBLIC Binance-style futures API. Normalizes
Aster klines into the SAME DataFrame shape the existing CPR/scoring stack
expects (open_time, open, high, low, close, volume), so Aster candles can be
scored by scanner.cpr_engine + scanner.scorer with no changes to those modules.

Read-only: no API key, no auth, no signed requests, no file writes, no Telegram.
This adapter does NOT touch the Hyperliquid path (scanner/fetcher.py, assets.py).

Public endpoints (base https://fapi.asterdex.com):
  GET /fapi/v1/klines       — OHLCV (array-of-arrays; ms timestamps; string values)
  GET /fapi/v1/ticker/24hr  — 24h quote volume per symbol
"""
from __future__ import annotations

import time

import pandas as pd

try:
    import requests
except Exception:                      # pragma: no cover
    requests = None

ASTER_BASE   = "https://fapi.asterdex.com"
KLINES_PATH  = "/fapi/v1/klines"
TICKER_PATH  = "/fapi/v1/ticker/24hr"
TIMEOUT_S    = 15

# Same intervals + lookback the Hyperliquid scanner uses (Aster ENUM matches).
INTERVAL_LOOKBACK: dict[str, int] = {
    "15m": 96, "30m": 96, "1h": 120, "4h": 90, "1d": 60, "1w": 52,
}

# Volume → liquidity score (mirrors scanner.assets TIER_SCORE thresholds, kept
# ISOLATED here for the Aster dry-run; does NOT change Hyperliquid liquidity).
def volume_to_liquidity(quote_volume_usd: float) -> float:
    v = float(quote_volume_usd or 0.0)
    if v >= 100_000_000: return 1.0
    if v >= 10_000_000:  return 0.8
    if v >= 5_000_000:   return 0.5
    return 0.2


# ── Pure normalization (offline-testable) ───────────────────────────────────

KLINE_COLUMNS = ["open_time", "open", "high", "low", "close", "volume"]

def normalize_klines(raw: list) -> "pd.DataFrame | None":
    """Convert Aster's array-of-arrays kline payload into the scorer's DataFrame
    (open_time UTC, open/high/low/close/volume floats). Returns None if empty.
    Pure: no network, no I/O."""
    if not raw:
        return None
    rows = [r[:6] for r in raw if isinstance(r, (list, tuple)) and len(r) >= 6]
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=KLINE_COLUMNS)
    df["open_time"] = pd.to_datetime(pd.to_numeric(df["open_time"], errors="coerce"),
                                     unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[KLINE_COLUMNS].dropna().reset_index(drop=True)
    return df if len(df) else None


def quote_volume_map(ticker_24h: list) -> dict:
    """symbol -> 24h quote volume (float). Tolerant of strings/missing. Pure."""
    vm = {}
    for t in ticker_24h or []:
        sym = t.get("symbol")
        if not sym:
            continue
        try:
            vm[sym] = float(t.get("quoteVolume", t.get("quoteAssetVolume", 0)) or 0)
        except (TypeError, ValueError):
            vm[sym] = 0.0
    return vm


# ── Read-only HTTP (injectable for tests) ────────────────────────────────────

def _http_get(path: str, params: dict | None = None):
    if requests is None:
        raise RuntimeError("requests not available")
    r = requests.get(ASTER_BASE + path, params=params or {},
                     headers={"Content-Type": "application/json"}, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def fetch_klines(symbol: str, interval: str, lookback_bars: int | None = None,
                 get=_http_get) -> "pd.DataFrame | None":
    """Fetch + normalize klines for one symbol/interval. Read-only. Returns the
    normalized DataFrame, or None on error/empty (never raises to the caller)."""
    n = lookback_bars or INTERVAL_LOOKBACK.get(interval, 200)
    try:
        raw = get(KLINES_PATH, {"symbol": symbol, "interval": interval,
                                "limit": min(max(int(n), 1), 1500)})
    except Exception as e:
        print(f"[aster] {symbol} {interval}: fetch failed: {e}")
        return None
    return normalize_klines(raw)


def fetch_24h_volumes(get=_http_get) -> dict:
    """Fetch the full 24h ticker once → {symbol: quote_volume}. Read-only.
    Returns {} on failure (caller degrades gracefully)."""
    try:
        ticks = get(TICKER_PATH, None)
        if isinstance(ticks, dict):
            ticks = [ticks]
    except Exception as e:
        print(f"[aster] ticker/24hr failed ({e}) — continuing without volume")
        return {}
    return quote_volume_map(ticks)


def fetch_symbol_frames(symbol: str, intervals: list, delay_ms: int = 80,
                        get=_http_get) -> dict:
    """Fetch normalized frames for one symbol across intervals. Read-only.
    Returns {interval: DataFrame|None}. One failing TF never aborts the rest."""
    frames = {}
    for iv in intervals:
        frames[iv] = fetch_klines(symbol, iv, get=get)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)
    return frames


# ── Offline self-test ────────────────────────────────────────────────────────

def _selftest() -> int:
    ok = []
    def chk(name, cond): ok.append(cond); print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    raw = [
        [1499040000000, "10.0", "12.0", "9.5", "11.0", "1000.0",
         1499043599999, "11000.0", 50, "500", "5500", "0"],
        [1499043600000, "11.0", "13.0", "10.5", "12.5", "2000.0",
         1499047199999, "25000.0", 80, "900", "11000", "0"],
    ]
    df = normalize_klines(raw)
    chk("normalize columns/order", list(df.columns) == KLINE_COLUMNS)
    chk("string->float", df["close"].iloc[0] == 11.0 and df["volume"].iloc[1] == 2000.0)
    chk("ms->UTC timestamp", str(df["open_time"].dt.tz) == "UTC"
        and df["open_time"].iloc[0].year == 2017)
    chk("empty -> None", normalize_klines([]) is None)
    chk("malformed rows dropped -> None", normalize_klines([[1, "2"]]) is None)
    chk("liquidity tiers",
        volume_to_liquidity(2e8) == 1.0 and volume_to_liquidity(2e7) == 0.8
        and volume_to_liquidity(6e6) == 0.5 and volume_to_liquidity(1e5) == 0.2)
    vm = quote_volume_map([{"symbol": "BTCUSDT", "quoteVolume": "123.4"},
                           {"symbol": "X", "quoteVolume": None}])
    chk("quote_volume_map", vm["BTCUSDT"] == 123.4 and vm["X"] == 0.0)

    # injectable get → fetch_klines uses it (no network)
    fake = lambda path, params=None: raw
    chk("fetch_klines via injected get", fetch_klines("BTCUSDT", "1h", get=fake) is not None)

    print(f"\nASTER ADAPTER SELFTEST: {sum(ok)}/{len(ok)} passed")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_selftest() if "--selftest" in sys.argv else 0)
