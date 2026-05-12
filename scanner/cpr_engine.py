"""
scanner/cpr_engine.py
=====================
CPR calculation and breakout signal detection.

Entry rules (exactly matching your manual method):
  LONG:  bar closes ABOVE TC (top of all 3 CPR lines cleared)
  SHORT: bar closes BELOW BC (bottom of all 3 CPR lines broken)
  SKIP:  price between BC and TC = 50/50, not taken

CPR Types:
  Narrow    — TC and BC very close together (< 0.5% of price)
              Strong magnet, highest reliability
  Ascending — TC > previous close (bullish bias)
  Descending— TC < previous close (bearish bias)
  Inside    — CPR fully inside previous session range
  Outside   — CPR fully outside previous session range
  Neutral   — neither ascending nor descending

Indicators computed per bar:
  ATR(14)       — for SL calculation
  SMA9 (close)  — trend direction confirmation
  Vol MA(20)    — volume confirmation gate
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ── Indicator helpers ────────────────────────────────────────────────────

def _ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()],
                   axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period).mean()


def _vol_ma(s: pd.Series, period: int = 20) -> pd.Series:
    return s.rolling(period).mean()


# ── CPR calculation ──────────────────────────────────────────────────────

def attach_cpr(df: pd.DataFrame) -> pd.DataFrame:
    """
    Attach CPR levels to a DataFrame.
    Uses the previous bar's H/L/C to compute current bar's CPR.

    Adds columns:
      pivot, bc, tc         — CPR levels
      r1, s1                — first resistance / support
      cpr_width_pct         — (TC - BC) / pivot as %
      is_narrow             — True if width < 0.5%
      cpr_type              — Narrow/Ascending/Descending/Inside/Outside/Neutral
    """
    df = df.copy()

    ph = df["high"].shift(1)
    pl = df["low"].shift(1)
    pc = df["close"].shift(1)
    pr = df["high"].shift(2)   # previous-previous high (for Outside check)
    ps = df["low"].shift(2)    # previous-previous low

    pivot = (ph + pl + pc) / 3
    bc    = (ph + pl) / 2
    tc    = 2 * pivot - bc

    # Ensure TC is always above BC
    tc_real = np.maximum(tc, bc)
    bc_real = np.minimum(tc, bc)

    r1 = 2 * pivot - pl
    s1 = 2 * pivot - ph

    width_pct = (tc_real - bc_real).abs() / pivot.replace(0, np.nan) * 100

    # CPR type classification
    conditions = [
        width_pct < 0.5,                          # Narrow
        (tc_real > ph) | (bc_real < pl),           # Outside
        (tc_real > pc) & (bc_real >= pc * 0.999),  # Ascending
        (bc_real < pc) & (tc_real <= pc * 1.001),  # Descending
    ]
    choices = ["Narrow", "Outside", "Ascending", "Descending"]
    cpr_type = np.select(conditions, choices, default="Neutral")

    # Inside: CPR fully inside previous session range
    inside = (tc_real <= ph) & (bc_real >= pl)
    cpr_type = np.where(
        (cpr_type == "Neutral") & inside, "Inside", cpr_type
    )

    df["pivot"]         = pivot.round(8)
    df["bc"]            = bc_real.round(8)
    df["tc"]            = tc_real.round(8)
    df["r1"]            = r1.round(8)
    df["s1"]            = s1.round(8)
    df["cpr_width_pct"] = width_pct.round(4)
    df["is_narrow"]     = width_pct < 0.5
    df["cpr_type"]      = cpr_type

    # Indicators
    df["atr"]    = _atr(df, 14).round(8)
    df["sma9"]   = _sma(df["close"], 9).round(8)
    df["vol_ma"] = _vol_ma(df["volume"], 20)
    df["atr_pct"] = (df["atr"] / df["close"].replace(0, np.nan) * 100).round(4)

    return df


# ── Signal detection ─────────────────────────────────────────────────────

def detect_signals(df: pd.DataFrame,
                   sl_atr_mult: float = 1.5,
                   tp_atr_mult: float = 3.0,
                   vol_mult: float = 1.2,
                   min_atr_pct: float = 0.3,
                   warmup: int = 30) -> pd.DataFrame:
    """
    Detect CPR breakout signals on prepared DataFrame.

    Long signal:  close > TC (cleared all 3 blue lines)
                  + volume > vol_mult × vol_ma
                  + SMA9 rising (close > sma9)
                  + ATR > min_atr_pct (not dead market)

    Short signal: close < BC (broken all 3 blue lines)
                  + volume > vol_mult × vol_ma
                  + SMA9 falling (close < sma9)
                  + ATR > min_atr_pct

    Adds columns:
      signal        — 'long', 'short', or None
      entry_price   — close price at signal bar
      sl_price      — stop loss price
      tp_price      — primary target (R1 for long, S1 for short)
      sl_pct        — SL distance as % of entry
      rr_ratio      — risk:reward ratio
    """
    df = df.copy()

    close  = df["close"]
    tc     = df["tc"]
    bc     = df["bc"]
    r1     = df["r1"]
    s1     = df["s1"]
    atr    = df["atr"]
    sma9   = df["sma9"]
    vol    = df["volume"]
    vol_ma = df["vol_ma"]
    atr_pct = df["atr_pct"]

    # Core conditions
    vol_ok    = (vol > vol_ma * vol_mult) & vol_ma.notna()
    atr_ok    = atr_pct > min_atr_pct
    warmup_ok = df.index >= warmup

    # Long: close above TC (all 3 CPR lines cleared)
    prev_close = close.shift(1)
    long_entry = (
        (close > tc) &              # closed above TC
        (prev_close <= tc) &        # previous bar was below or at TC
        (close > sma9) &            # SMA9 supports uptrend
        vol_ok & atr_ok & warmup_ok
    )

    # Short: close below BC (all 3 CPR lines broken)
    short_entry = (
        (close < bc) &              # closed below BC
        (prev_close >= bc) &        # previous bar was above or at BC
        (close < sma9) &            # SMA9 supports downtrend
        vol_ok & atr_ok & warmup_ok
    )

    # Build signal series
    signal = pd.Series(None, index=df.index, dtype=object)
    signal[long_entry]  = "long"
    signal[short_entry] = "short"

    df["signal"] = signal

    # Entry, SL, TP calculations
    long_sl  = close - sl_atr_mult * atr
    short_sl = close + sl_atr_mult * atr
    long_tp  = r1                    # R1 = green line target
    short_tp = s1                    # S1 = blue target for shorts

    df["entry_price"] = np.where(signal.notna(), close, np.nan)
    df["sl_price"]    = np.where(
        signal == "long",  long_sl,
        np.where(signal == "short", short_sl, np.nan)
    )
    df["tp_price"] = np.where(
        signal == "long",  long_tp,
        np.where(signal == "short", short_tp, np.nan)
    )

    # SL % and R:R
    df["sl_pct"] = (
        (df["entry_price"] - df["sl_price"]).abs() /
        df["entry_price"].replace(0, np.nan) * 100
    ).round(3)

    tp_dist = (df["tp_price"] - df["entry_price"]).abs()
    sl_dist = (df["sl_price"] - df["entry_price"]).abs()
    df["rr_ratio"] = (tp_dist / sl_dist.replace(0, np.nan)).round(2)

    return df


def get_latest_signal(df: pd.DataFrame) -> dict | None:
    """
    Extract the most recent signal from a prepared+detected DataFrame.
    Returns dict with signal details, or None if no recent signal.

    Only returns signal if it's on the LAST completed bar
    (not a signal that happened 5 bars ago).
    """
    if df is None or len(df) < 2:
        return None

    # Check last 2 bars (current forming + last complete)
    for i in [-2, -3]:
        try:
            row = df.iloc[i]
        except IndexError:
            continue

        if pd.isna(row.get("signal")):
            continue

        return {
            "signal":      row["signal"],
            "bar_time":    row["open_time"],
            "entry_price": row["entry_price"],
            "sl_price":    row["sl_price"],
            "tp_price":    row["tp_price"],
            "sl_pct":      row["sl_pct"],
            "rr_ratio":    row["rr_ratio"],
            "close":       row["close"],
            "tc":          row["tc"],
            "bc":          row["bc"],
            "pivot":       row["pivot"],
            "r1":          row["r1"],
            "s1":          row["s1"],
            "cpr_type":    row["cpr_type"],
            "cpr_width":   row["cpr_width_pct"],
            "is_narrow":   row["is_narrow"],
            "atr":         row["atr"],
            "atr_pct":     row["atr_pct"],
            "volume":      row["volume"],
            "vol_ma":      row["vol_ma"],
            "vol_ratio":   round(row["volume"] / row["vol_ma"], 2)
                           if row["vol_ma"] > 0 else 0,
            "sma9":        row["sma9"],
            "sma9_rising": row["close"] > row["sma9"],
        }

    return None


def scan_symbol_interval(df: pd.DataFrame) -> dict | None:
    """
    Full pipeline: attach CPR → detect signals → return latest signal.
    Returns None if no signal found.
    """
    if df is None or len(df) < 35:
        return None
    df = attach_cpr(df)
    df = detect_signals(df)
    return get_latest_signal(df)


if __name__ == "__main__":
    # Test on synthetic data
    import numpy as np

    np.random.seed(42)
    n = 100
    price = 2300.0
    rows = []
    for i in range(n):
        price *= (1 + np.random.normal(0, 0.008))
        h = price * (1 + abs(np.random.normal(0, 0.004)))
        l = price * (1 - abs(np.random.normal(0, 0.004)))
        rows.append({
            "open_time": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(hours=i),
            "open": price, "high": h, "low": l, "close": price,
            "volume": np.random.lognormal(8, 0.5)
        })

    df = pd.DataFrame(rows)
    df = attach_cpr(df)
    df = detect_signals(df)

    signals = df[df["signal"].notna()]
    print(f"Test: {len(df)} bars → {len(signals)} signals found")
    print(f"  Long:  {(signals['signal']=='long').sum()}")
    print(f"  Short: {(signals['signal']=='short').sum()}")

    if len(signals):
        s = signals.iloc[-1]
        print(f"\nLatest signal:")
        print(f"  {s['signal'].upper()} @ {s['close']:.4f}")
        print(f"  SL: {s['sl_price']:.4f} ({s['sl_pct']:.2f}%)")
        print(f"  TP: {s['tp_price']:.4f}")
        print(f"  R:R: {s['rr_ratio']:.2f}")
        print(f"  CPR: {s['cpr_type']}  width={s['cpr_width_pct']:.3f}%")
        vol_ratio = s['volume'] / s['vol_ma'] if s['vol_ma'] > 0 else 0
        print(f"  Vol: {vol_ratio:.2f}× avg")
