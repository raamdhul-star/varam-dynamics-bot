"""
scanner/fetcher.py
==================
Fetches OHLCV candles for all 50 assets across 4 timeframes
from Hyperliquid's public API.

Timeframes scanned:
  1h  — entry timeframe (your primary signal bar)
  4h  — intermediate trend confirmation
  1d  — daily trend (macro direction)
  1w  — weekly trend (big picture)

Also fetches:
  15m — lower TF micro-structure check
  30m — lower TF momentum check

Each fetch returns a pandas DataFrame with columns:
  open_time, open, high, low, close, volume
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

# Interval → lookback bars (enough for CPR + indicators)
INTERVAL_LOOKBACK: dict[str, int] = {
    "15m": 96,     # 24h of 15m bars
    "30m": 96,     # 48h of 30m bars
    "1h":  120,    # 5 days of 1h bars
    "4h":  90,     # 15 days of 4h bars
    "1d":  60,     # 60 days of daily bars
    "1w":  52,     # 1 year of weekly bars
}

# Interval → milliseconds per bar
INTERVAL_MS: dict[str, int] = {
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h":  60 * 60 * 1000,
    "4h":  4  * 60 * 60 * 1000,
    "1d":  24 * 60 * 60 * 1000,
    "1w":  7  * 24 * 60 * 60 * 1000,
}

SCAN_INTERVALS = ["1h", "4h", "1d", "1w"]
LOWER_TF_INTERVALS = ["15m", "30m"]
ALL_INTERVALS = LOWER_TF_INTERVALS + SCAN_INTERVALS


def fetch_candles(symbol: str, interval: str,
                  lookback_bars: int | None = None,
                  timeout: int = 10) -> pd.DataFrame | None:
    """
    Fetch OHLCV candles from Hyperliquid for one symbol+interval.
    Returns DataFrame or None on error.
    """
    n_bars = lookback_bars or INTERVAL_LOOKBACK.get(interval, 100)
    ms_per_bar = INTERVAL_MS.get(interval)
    if not ms_per_bar:
        print(f"[fetcher] Unknown interval: {interval}")
        return None

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - (n_bars * ms_per_bar)

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": now_ms,
        }
    }

    try:
        resp = requests.post(
            HYPERLIQUID_INFO_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json()

        if not raw:
            return None

        df = pd.DataFrame(raw)
        # HL candle fields: t=open_time, o=open, h=high, l=low, c=close, v=volume
        df = df.rename(columns={
            "t": "open_time", "o": "open", "h": "high",
            "l": "low",       "c": "close", "v": "volume",
            "n": "trades"
        })
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df[["open_time", "open", "high", "low", "close", "volume"]]\
               .dropna().reset_index(drop=True)
        return df

    except Exception as e:
        print(f"[fetcher] {symbol} {interval}: {e}")
        return None


def fetch_all(symbols: list[str],
              intervals: list[str] | None = None,
              delay_ms: int = 50) -> dict[str, dict[str, pd.DataFrame]]:
    """
    Fetch candles for all symbols across all intervals.
    Returns: {symbol: {interval: DataFrame}}

    delay_ms: pause between requests to avoid rate limiting
    """
    intervals = intervals or ALL_INTERVALS
    results: dict[str, dict[str, pd.DataFrame]] = {}
    total = len(symbols) * len(intervals)
    done = 0

    print(f"[fetcher] Fetching {len(symbols)} assets × {len(intervals)} TFs "
          f"= {total} requests...")

    for sym in symbols:
        results[sym] = {}
        for iv in intervals:
            df = fetch_candles(sym, iv)
            if df is not None and len(df) >= 20:
                results[sym][iv] = df
            done += 1
            if delay_ms > 0:
                time.sleep(delay_ms / 1000)

        n_ok = sum(1 for v in results[sym].values() if v is not None)
        print(f"  {sym:<8} {n_ok}/{len(intervals)} TFs fetched")

    ok_assets = sum(1 for v in results.values() if v)
    print(f"[fetcher] Done — {ok_assets}/{len(symbols)} assets have data")
    return results


def fetch_scan_intervals(symbols: list[str],
                         delay_ms: int = 50) -> dict[str, dict[str, pd.DataFrame]]:
    """Fetch only the 4 main scan intervals (skip lower TFs for speed)."""
    return fetch_all(symbols, SCAN_INTERVALS, delay_ms)


def fetch_lower_tf(symbol: str) -> dict[str, pd.DataFrame]:
    """Fetch 15m and 30m for a single symbol (used for micro-structure check)."""
    result = {}
    for iv in LOWER_TF_INTERVALS:
        df = fetch_candles(symbol, iv)
        if df is not None:
            result[iv] = df
    return result


if __name__ == "__main__":
    # Quick test — fetch BTC and ETH across all intervals
    test_syms = ["BTC", "ETH"]
    data = fetch_all(test_syms, ALL_INTERVALS, delay_ms=100)

    for sym in test_syms:
        print(f"\n{sym}:")
        for iv, df in data[sym].items():
            if df is not None:
                print(f"  {iv:>4}: {len(df):>4} bars  "
                      f"latest close={df['close'].iloc[-1]:.4f}  "
                      f"({df['open_time'].iloc[-1].strftime('%Y-%m-%d %H:%M')} UTC)")
