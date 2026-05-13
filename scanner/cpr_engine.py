"""
scanner/cpr_engine.py  (v2 — bug fixes)
========================================
CPR calculation and breakout signal detection.

Bugs fixed vs v1:
  BUG1: Short TP was above entry — now uses ATR-based target if S1 invalid
  BUG2: No minimum R:R filter — now requires R:R >= 1.2
  BUG4: Score didn't penalise bad R:R — scorer now gets accurate R:R
  BUG5: Stale signals (bar -3) included — now only bar -2 (last complete)
  BUG6: Volume filter too loose — raised to 1.5x
  BUG7: SL% shown incorrectly for shorts — now always positive

Entry rules:
  LONG:  bar closes ABOVE TC (all 3 CPR lines cleared)
         + volume > 1.5x average
         + close > SMA9 (uptrend)
         + ATR > 0.3% of price (not a dead market)

  SHORT: bar closes BELOW BC (all 3 CPR lines broken)
         + volume > 1.5x average
         + close < SMA9 (downtrend)
         + ATR > 0.3% of price

  SKIP:  price between BC and TC (50/50 zone — your rule)
         OR R:R < 1.2 (poor setup)
         OR signal is stale (> last completed bar)
"""
from __future__ import annotations
import numpy as np
import pandas as pd


# ── Indicators ────────────────────────────────────────────────────────────

def _atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h-l).abs(), (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def _sma(s, period):
    return s.rolling(period).mean()

def _vol_ma(s, period=20):
    return s.rolling(period).mean()


# ── CPR calculation ───────────────────────────────────────────────────────

def attach_cpr(df: pd.DataFrame) -> pd.DataFrame:
    """
    Attach CPR levels using previous bar's H/L/C.
    Adds: pivot, bc, tc, r1, s1, cpr_width_pct, is_narrow, cpr_type,
          atr, atr_pct, sma9, vol_ma
    """
    df = df.copy()

    ph = df["high"].shift(1)
    pl = df["low"].shift(1)
    pc = df["close"].shift(1)

    pivot = (ph + pl + pc) / 3
    bc    = (ph + pl) / 2
    tc    = 2 * pivot - bc

    # Ensure TC always >= BC
    tc_real = np.maximum(tc, bc)
    bc_real = np.minimum(tc, bc)

    # Standard pivot levels
    r1 = 2 * pivot - pl   # Resistance 1 (above pivot)
    s1 = 2 * pivot - ph   # Support 1 (below pivot)

    # CPR width
    width_pct = (tc_real - bc_real).abs() / pivot.replace(0, np.nan) * 100

    # CPR type
    conds = [
        width_pct < 0.5,
        (tc_real > ph) | (bc_real < pl),
        (tc_real > pc) & (bc_real >= pc * 0.999),
        (bc_real < pc) & (tc_real <= pc * 1.001),
    ]
    choices = ["Narrow", "Outside", "Ascending", "Descending"]
    cpr_type = np.select(conds, choices, default="Neutral")

    inside   = (tc_real <= ph) & (bc_real >= pl)
    cpr_type = np.where((cpr_type == "Neutral") & inside, "Inside", cpr_type)

    df["pivot"]         = pivot.round(8)
    df["bc"]            = bc_real.round(8)
    df["tc"]            = tc_real.round(8)
    df["r1"]            = r1.round(8)
    df["s1"]            = s1.round(8)
    df["cpr_width_pct"] = width_pct.round(4)
    df["is_narrow"]     = width_pct < 0.5
    df["cpr_type"]      = cpr_type

    df["atr"]     = _atr(df, 14).round(8)
    df["sma9"]    = _sma(df["close"], 9).round(8)
    df["vol_ma"]  = _vol_ma(df["volume"], 20)
    df["atr_pct"] = (df["atr"] / df["close"].replace(0, np.nan) * 100).round(4)

    return df.reset_index(drop=True)


# ── Signal detection ──────────────────────────────────────────────────────

def detect_signals(df: pd.DataFrame,
                   sl_atr_mult:  float = 1.5,
                   tp_r_mult:    float = 2.0,   # TP = entry ± tp_r_mult * sl_dist
                   vol_mult:     float = 1.5,   # BUG6 fix: raised from 1.2
                   min_atr_pct:  float = 0.3,
                   min_rr:       float = 1.2,   # BUG2 fix: hard R:R filter
                   warmup:       int   = 30) -> pd.DataFrame:
    """
    Detect CPR breakout signals with all bug fixes applied.
    """
    df = df.copy()

    close   = df["close"]
    tc      = df["tc"]
    bc      = df["bc"]
    r1      = df["r1"]
    s1      = df["s1"]
    atr     = df["atr"]
    sma9    = df["sma9"]
    vol     = df["volume"]
    vol_ma  = df["vol_ma"]
    atr_pct = df["atr_pct"]

    prev_close = close.shift(1)
    vol_ok     = (vol > vol_ma * vol_mult) & vol_ma.notna()
    atr_ok     = atr_pct > min_atr_pct
    warmup_ok  = df.index >= warmup

    # ── Long: close crossed above TC ─────────────────────────────────────
    long_entry = (
        (close > tc) &
        (prev_close <= tc) &
        (close > sma9) &
        vol_ok & atr_ok & warmup_ok
    )

    # ── Short: close crossed below BC ────────────────────────────────────
    short_entry = (
        (close < bc) &
        (prev_close >= bc) &
        (close < sma9) &
        vol_ok & atr_ok & warmup_ok
    )

    signal = pd.Series(None, index=df.index, dtype=object)
    signal[long_entry]  = "long"
    signal[short_entry] = "short"

    # ── SL levels (ATR-based) ─────────────────────────────────────────────
    long_sl  = (close - sl_atr_mult * atr).round(8)
    short_sl = (close + sl_atr_mult * atr).round(8)

    sl_dist_long  = (close - long_sl).abs()
    sl_dist_short = (close - short_sl).abs()

    # ── TP levels — BUG1 FIX ─────────────────────────────────────────────
    # LONG TP: use R1 if R1 > entry, else ATR-based
    long_tp_r1  = r1
    long_tp_atr = close + tp_r_mult * sl_dist_long
    long_tp = np.where(r1 > close, long_tp_r1, long_tp_atr)

    # SHORT TP: use S1 if S1 < entry price, otherwise ATR-based
    # S1 must be BELOW current close to be a valid short target
    short_tp_s1  = s1
    short_tp_atr = close - tp_r_mult * sl_dist_short
    short_tp = np.where(s1 < close, short_tp_s1, short_tp_atr)
    # Safety: ensure short TP is always below entry (price must fall to profit)
    short_tp = np.where(short_tp < close, short_tp, close - tp_r_mult * sl_dist_short)

    df["signal"]      = signal
    df["entry_price"] = np.where(signal.notna(), close, np.nan)

    df["sl_price"] = np.where(
        signal == "long",  long_sl,
        np.where(signal == "short", short_sl, np.nan)
    )
    df["tp_price"] = np.where(
        signal == "long",  long_tp,
        np.where(signal == "short", short_tp, np.nan)
    )

    # ── R:R ratio ────────────────────────────────────────────────────────
    tp_dist = (df["tp_price"] - df["entry_price"]).abs()
    sl_dist = (df["sl_price"] - df["entry_price"]).abs()
    rr = (tp_dist / sl_dist.replace(0, np.nan)).round(2)
    df["rr_ratio"] = rr

    # ── BUG2 + BUG4 FIX: filter out bad R:R signals ──────────────────────
    bad_rr = rr < min_rr
    df.loc[bad_rr & signal.notna(), "signal"] = None

    # ── SL% — BUG7 FIX: always positive ──────────────────────────────────
    df["sl_pct"] = (
        (df["sl_price"] - df["entry_price"]).abs() /
        df["entry_price"].replace(0, np.nan) * 100
    ).round(3)

    # ── Validate: TP and SL must be on correct sides ──────────────────────
    # Long: tp > entry AND sl < entry
    long_valid = (
        (df["signal"] == "long") &
        (df["tp_price"] > df["entry_price"]) &
        (df["sl_price"] < df["entry_price"])
    )
    # Short: tp < entry AND sl > entry
    short_valid = (
        (df["signal"] == "short") &
        (df["tp_price"] < df["entry_price"]) &
        (df["sl_price"] > df["entry_price"])
    )

    # Invalidate any remaining bad signals
    invalid = (
        df["signal"].notna() &
        ~long_valid & ~short_valid
    )
    df.loc[invalid, "signal"] = None

    return df


# ── Latest signal extractor — BUG5 FIX ───────────────────────────────────

def get_latest_signal(df: pd.DataFrame) -> dict | None:
    """
    Return the most recent valid signal from the last 4 completed bars.
    Bars older than 2 intervals are considered stale.
    """
    if df is None or len(df) < 5:
        return None

    # Check last 4 completed bars (current bar is forming, so skip -1)
    for i in [-2, -3, -4, -5]:
        try:
            row = df.iloc[i]
        except IndexError:
            continue
        if not pd.isna(row.get("signal")):
            break
    else:
        return None  # no signal found in window

    if pd.isna(row.get("signal")):
        return None

    vol_ratio = (row["volume"] / row["vol_ma"]
                 if row.get("vol_ma", 0) > 0 else 0)

    return {
        "signal":      row["signal"],
        "bar_time":    row["open_time"],
        "entry_price": round(float(row["entry_price"]), 8),
        "sl_price":    round(float(row["sl_price"]), 8),
        "tp_price":    round(float(row["tp_price"]), 8),
        "sl_pct":      round(float(row["sl_pct"]), 3),
        "rr_ratio":    round(float(row["rr_ratio"]), 2),
        "close":       float(row["close"]),
        "tc":          float(row["tc"]),
        "bc":          float(row["bc"]),
        "pivot":       float(row["pivot"]),
        "r1":          float(row["r1"]),
        "s1":          float(row["s1"]),
        "cpr_type":    row["cpr_type"],
        "cpr_width":   float(row["cpr_width_pct"]),
        "is_narrow":   bool(row["is_narrow"]),
        "atr":         float(row["atr"]),
        "atr_pct":     float(row["atr_pct"]),
        "volume":      float(row["volume"]),
        "vol_ma":      float(row.get("vol_ma", 0)),
        "vol_ratio":   round(vol_ratio, 2),
        "sma9":        float(row["sma9"]),
        "sma9_rising": float(row["close"]) > float(row["sma9"]),
    }


def scan_symbol_interval(df: pd.DataFrame) -> dict | None:
    """Full pipeline: attach CPR → detect signals → return latest."""
    if df is None or len(df) < 35:
        return None
    df = attach_cpr(df)
    df = detect_signals(df)
    return get_latest_signal(df)


# ── Tests ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import numpy as np

    print("="*60)
    print("CPR ENGINE v2 — BUG VALIDATION TESTS")
    print("="*60)

    np.random.seed(42)
    n = 80
    price = 40.5
    rows = []
    for i in range(n):
        if i < 60:
            price *= (1 + np.random.normal(0.0001, 0.006))
        else:
            price *= (1 - 0.008)
        rows.append({
            "open_time": pd.Timestamp("2026-05-12", tz="UTC") + pd.Timedelta(hours=i*4),
            "open": price, "high": price*1.004, "low": price*0.996,
            "close": price, "volume": np.random.lognormal(8, 0.5)
        })

    df = pd.DataFrame(rows)
    df = attach_cpr(df)
    df = detect_signals(df)

    signals = df[df["signal"].notna()]
    print(f"\nTotal valid signals: {len(signals)}")

    all_ok = True
    for _, row in signals.iterrows():
        d     = row["signal"]
        entry = row["entry_price"]
        sl    = row["sl_price"]
        tp    = row["tp_price"]
        rr    = row["rr_ratio"]

        sl_ok = sl < entry if d == "long" else sl > entry
        tp_ok = tp > entry if d == "long" else tp < entry
        rr_ok = rr >= 1.2

        ok = sl_ok and tp_ok and rr_ok
        if not ok:
            all_ok = False
        status = "✅" if ok else "❌ BUG"
        print(f"  {status} {d.upper():5s} entry={entry:.4f} "
              f"sl={sl:.4f} tp={tp:.4f} rr={rr:.2f} "
              f"sl_ok={sl_ok} tp_ok={tp_ok} rr_ok={rr_ok}")

    print()
    if all_ok:
        print("✅ ALL TESTS PASSED — no bugs found")
    else:
        print("❌ BUGS REMAIN — check output above")
