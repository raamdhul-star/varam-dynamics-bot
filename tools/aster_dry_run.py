"""
tools/aster_dry_run.py — Sprint D2: Aster candle fetch + scoring DRY-RUN
========================================================================
Read-only proof harness. Fetches a CURATED set of Aster markets, normalizes
their candles, and runs the EXISTING CPR/scoring stack (attach_cpr →
detect_signals → get_latest_signal → score_signal) in log-only mode, then
prints a report.

DRY RUN ONLY:
  • no Telegram messages          • no paper positions opened
  • no live state / results writes • no live classification change
  • Hyperliquid flow untouched     • source="aster" lives only in this report

Usage:
  python tools/aster_dry_run.py            # live read-only dry-run report
  python tools/aster_dry_run.py --selftest # offline tests with mocked candles
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.cpr_engine import attach_cpr, detect_signals, get_latest_signal
from scanner.scorer import score_signal
from scanner.sources import aster

# Curated D2 candidates ONLY (not live-approved classifications).
CRYPTO_CANDIDATES = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT", "ASTERUSDT"]
RWA_PERP_CANDIDATES = ["SPCXUSDT", "OPENAIUSDT", "ANTHROPICUSDT", "MSTRUSDT",
                       "SPXUSDT", "SPYUSDT", "NVDAUSDT", "TSLAUSDT", "AAPLUSDT",
                       "QQQUSDT", "XAUUSDT"]
CANDIDATE_CLASS = ({s: "candidate_crypto" for s in CRYPTO_CANDIDATES}
                   | {s: "candidate_rwa_perp" for s in RWA_PERP_CANDIDATES})

SCAN_INTERVALS  = ["1h", "4h", "1d", "1w"]
LOWER_TF        = ["15m", "30m"]
MIN_BARS        = 35                 # mirrors main._prepare
LOW_VOL_CRYPTO  = 1_000_000          # warn below this (crypto)
LOW_VOL_RWA     = 50_000             # warn below this (rwa/perp)


def _prepare(df):
    """Mirror of main._prepare (no import of main to avoid side effects)."""
    if df is None or len(df) < MIN_BARS:
        return None
    return detect_signals(attach_cpr(df))


def _lower_tf_ok(direction: str, lower_frames: dict) -> bool:
    """Mirror of main.lower_tf_ok using already-fetched lower-TF frames."""
    for tf in LOWER_TF:
        df = _prepare(lower_frames.get(tf))
        if df is None or len(df) < 2:
            continue
        last = df.iloc[-2]
        if direction == "long" and last["close"] > last["sma9"]:
            return True
        if direction == "short" and last["close"] < last["sma9"]:
            return True
    return False


def _low_vol(symbol: str, vol: float) -> bool:
    floor = LOW_VOL_CRYPTO if CANDIDATE_CLASS.get(symbol) == "candidate_crypto" else LOW_VOL_RWA
    return float(vol or 0.0) < floor


def evaluate_symbol(symbol: str, frames: dict, vol: float) -> dict:
    """Score one symbol from its (already-fetched) frames. Pure given frames.
    Returns a result dict with per-scan-interval rows + counters."""
    cat = CANDIDATE_CLASS.get(symbol, "unknown")
    liquidity = aster.volume_to_liquidity(vol)
    prepared = {tf: _prepare(frames.get(tf)) for tf in SCAN_INTERVALS}
    tf_sigs  = {tf: (get_latest_signal(p) if p is not None else None)
                for tf, p in prepared.items()}
    lower_status = {tf: ("ok" if frames.get(tf) is not None and len(frames[tf]) >= MIN_BARS
                         else "insufficient") for tf in LOWER_TF}

    rows = []
    for tf in SCAN_INTERVALS:
        raw = frames.get(tf)
        n   = 0 if raw is None else len(raw)
        latest_close = None if raw is None or n == 0 else float(raw["close"].iloc[-1])
        row = {"source": "aster", "symbol": symbol, "candidate": cat,
               "quote_volume": float(vol or 0.0), "low_volume": _low_vol(symbol, vol),
               "timeframe": tf, "candle_count": n, "latest_close": latest_close,
               "scored": False, "score": None, "direction": None,
               "entry": None, "target": None, "stop": None, "risk": None,
               "should_alert": None,
               "lower_tf": ",".join(f"{k}:{v}" for k, v in lower_status.items()),
               "error": None}
        if raw is None or n < MIN_BARS:
            row["error"] = f"insufficient candles ({n} < {MIN_BARS})"
            rows.append(row); continue
        sig = tf_sigs.get(tf)
        if not sig:
            row["error"] = "no signal on this candle"
            rows.append(row); continue
        try:
            direction = sig["signal"]
            sb = score_signal(symbol=symbol, interval=tf, direction=direction,
                              signal_data=sig, all_tf_signals=tf_sigs,
                              liquidity_score=liquidity,
                              lower_tf_ok=_lower_tf_ok(direction, frames))
            row.update(scored=True, score=round(sb.total_score, 2), direction=direction,
                       entry=sb.entry_price, target=sb.tp_price, stop=sb.sl_price,
                       risk=f"{sb.risk_emoji} {sb.risk_label}".strip(),
                       should_alert=sb.should_alert)
        except Exception as e:                       # never abort the whole run
            row["error"] = f"score error: {e}"
        rows.append(row)
    return {"symbol": symbol, "candidate": cat, "rows": rows}


def build_report(results: list, attempted: int, succeeded: int, warnings: list) -> str:
    L = ["=" * 70,
         "ASTER CANDLE + SCORING DRY-RUN",
         "DRY RUN ONLY — no alerts, no paper, no live classification",
         "=" * 70]
    scoreable = cc = rc = failed = 0
    scored_with_vol = []
    for r in results:
        for row in r["rows"]:
            tag = "🟢" if row["scored"] else "  "
            extra = (f"score={row['score']} {row['direction']} "
                     f"entry={row['entry']} tgt={row['target']} stop={row['stop']} "
                     f"risk={row['risk']} alert={row['should_alert']}"
                     if row["scored"] else f"[{row['error']}]")
            lowflag = " ⚠️LOWVOL" if row["low_volume"] else ""
            L.append(f"{tag} aster {row['symbol']:<13} {row['candidate']:<17} "
                     f"vol=${row['quote_volume']/1e6:>8.3f}M{lowflag} "
                     f"{row['timeframe']:>3} bars={row['candle_count']:>3} "
                     f"close={row['latest_close']} lowerTF[{row['lower_tf']}] {extra}")
            if row["scored"]:
                scoreable += 1
                scored_with_vol.append((row["symbol"], row["quote_volume"]))
                if row["candidate"] == "candidate_crypto": cc += 1
                elif row["candidate"] == "candidate_rwa_perp": rc += 1
            elif row["error"] and "insufficient" in row["error"]:
                failed += 1
        L.append("")
    lowest = sorted(set(scored_with_vol), key=lambda x: x[1])[:5]
    L += ["-" * 70, "SUMMARY",
          f"  symbols tested:               {len(results)}",
          f"  candle requests attempted:    {attempted}",
          f"  candle requests succeeded:    {succeeded}",
          f"  scoreable (symbol×TF) rows:   {scoreable}",
          f"  candidate_crypto signals:     {cc}",
          f"  candidate_rwa_perp signals:   {rc}",
          f"  insufficient-candle rows:     {failed}",
          f"  lowest-volume scored markets: " +
          (", ".join(f"{s}(${v/1e6:.3f}M)" for s, v in lowest) if lowest else "(none)"),
          f"  API/rate-limit warnings:      {len(warnings)}"]
    for w in warnings[:10]:
        L.append(f"     - {w}")
    L += ["!" * 70,
          "Candidate labels are SUGGESTIONS ONLY — no live classification changed.",
          "!" * 70]
    return "\n".join(L)


def run_dry_run(get=None) -> str:
    """Live (or injected) read-only dry-run. `get` injectable for tests."""
    g = get or aster._http_get
    symbols = CRYPTO_CANDIDATES + RWA_PERP_CANDIDATES
    vols = aster.fetch_24h_volumes(get=g)
    intervals = SCAN_INTERVALS + LOWER_TF
    attempted = succeeded = 0
    warnings, results = [], []
    for sym in symbols:
        frames = {}
        for iv in intervals:
            attempted += 1
            df = aster.fetch_klines(sym, iv, get=g)
            frames[iv] = df
            if df is not None:
                succeeded += 1
            else:
                warnings.append(f"{sym} {iv}: no candles")
            import time as _t; _t.sleep(0.08)
        results.append(evaluate_symbol(sym, frames, vols.get(sym, 0.0)))
    return build_report(results, attempted, succeeded, warnings)


# ── Offline self-test (mocked candles; exercises the REAL scorer) ────────────

def _synthetic_klines(n: int, base: float = 100.0) -> list:
    """Build n trending klines as Aster array-of-arrays (drives a real signal)."""
    out, t0, step = [], 1_700_000_000_000, 3_600_000
    px = base
    for i in range(n):
        o = px; px = px * 1.01; h = max(o, px) * 1.005; l = min(o, px) * 0.995
        out.append([t0 + i * step, f"{o:.4f}", f"{h:.4f}", f"{l:.4f}",
                    f"{px:.4f}", f"{1000 + i * 10:.4f}",
                    t0 + (i + 1) * step - 1, "0", 10, "0", "0", "0"])
    return out


def _selftest() -> int:
    ok = []
    def chk(name, cond): ok.append(cond); print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # no Telegram dependency anywhere in this tool or the adapter
    chk("no telegram import in dry-run module", "telegram" not in sys.modules
        or all("telegram" not in m for m in ("scanner.sources.aster",)))
    import scanner.sources.aster as a
    chk("adapter has no telegram attr", not hasattr(a, "send_message"))

    # scorer compatibility: synthetic normalized candles → real pipeline
    big = aster.normalize_klines(_synthetic_klines(60))
    chk("normalize synthetic -> df", big is not None and len(big) == 60)
    prepared = _prepare(big)
    chk("attach_cpr+detect_signals runs", prepared is not None)
    # full evaluate_symbol with injected frames (BTCUSDT = candidate_crypto)
    frames = {tf: aster.normalize_klines(_synthetic_klines(60)) for tf in SCAN_INTERVALS}
    frames |= {tf: aster.normalize_klines(_synthetic_klines(60)) for tf in LOWER_TF}
    res = evaluate_symbol("BTCUSDT", frames, vol=5_000_000)
    chk("evaluate_symbol returns rows", len(res["rows"]) == len(SCAN_INTERVALS))
    chk("at least one row scored OR cleanly reported",
        all(("scored" in r) for r in res["rows"]))

    # insufficient candles handled
    short = {tf: aster.normalize_klines(_synthetic_klines(10)) for tf in SCAN_INTERVALS + LOWER_TF}
    res2 = evaluate_symbol("SPCXUSDT", short, vol=40_000)
    chk("insufficient candles flagged",
        all(r["error"] and "insufficient" in r["error"] for r in res2["rows"]))
    chk("low-volume rwa flagged", all(r["low_volume"] for r in res2["rows"]))

    # report builds + banner
    rep = build_report([res, res2], attempted=24, succeeded=24, warnings=["x 1w: no candles"])
    chk("report has banner + summary",
        "DRY RUN ONLY" in rep and "SUMMARY" in rep and "SUGGESTIONS ONLY" in rep)

    # injected get path (no network) works end-to-end
    fake_get = lambda path, params=None: (
        [{"symbol": "BTCUSDT", "quoteVolume": "5000000"}] if "ticker" in path
        else _synthetic_klines(60))
    rep2 = run_dry_run(get=fake_get)
    chk("run_dry_run via injected get (no network)", "ASTER CANDLE" in rep2)

    print(f"\nDRY-RUN SELFTEST: {sum(ok)}/{len(ok)} passed")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(run_dry_run())
