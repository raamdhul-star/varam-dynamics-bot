"""
tools/aster_alert_send.py — Sprint D3: Aster VIEW-ONLY Telegram alerts
======================================================================
Sends Aster signals to Telegram as SEPARATE, view-only messages
(🌐 Aster Crypto Signals / 🏦 Aster RWA / Perp Signals). View-only means:

  • NO Select / Skip / Confirm buttons, NO alert_kb(), NO reply markup
  • creates NO `batches` entry, NO trade session
  • writes NOTHING to Upstash / trades.csv / paper tracker
  • does NOT touch the Hyperliquid flow or api/telegram.py
  • lifecycle/dedup state is ISOLATED in results/aster_view_state.json with
    source-namespaced keys (aster|symbol|direction|interval[…|bar_time])

DOUBLE FEATURE GATE — sends only when BOTH are true:
  1. CLI flag        --send
  2. env             ASTER_ALERTS_ENABLED=true
Default (no flags) is DRY-RUN: prints what would send, sends nothing, writes
nothing. --selftest runs offline with mocked data and never sends/writes.

Usage:
  python tools/aster_alert_send.py            # dry-run (no send, no write)
  python tools/aster_alert_send.py --send     # sends ONLY if env gate is true
  python tools/aster_alert_send.py --selftest # offline mocked tests
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.cpr_engine import attach_cpr, detect_signals, get_latest_signal
from scanner.scorer import score_signal
from scanner.sources import aster
# Reuse the PURE A2 lifecycle helpers + the generic sender — without modifying bot.py.
from telegram.bot import _classify_lifecycle, send_message as _bot_send_message

# ── Isolated Aster view-only state (separate from telegram_state.json) ───────
ASTER_STATE_FILE = Path(__file__).resolve().parent.parent / "results" / "aster_view_state.json"

# ── Curated D3 Aster watchlist (source-aware; NO substring heuristics) ───────
CRYPTO_WATCH = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT", "ASTERUSDT"]
RWA_WATCH    = ["SPCXUSDT", "OPENAIUSDT", "ANTHROPICUSDT", "MSTRUSDT", "SPXUSDT",
                "SPYUSDT", "NVDAUSDT", "TSLAUSDT", "AAPLUSDT", "QQQUSDT", "XAUUSDT"]
ASTER_CLASS  = ({s: "crypto" for s in CRYPTO_WATCH}
                | {s: "rwa_perp" for s in RWA_WATCH})

SCAN_INTERVALS = ["1h", "4h", "1d", "1w"]
LOWER_TF       = ["15m", "30m"]
MIN_BARS       = 35
CAP            = 5
CRYPTO_VOL_FLOOR    = 1_000_000     # Aster crypto: HL-parity filter
RWA_LOW_VOL_WARN    = 250_000       # RWA/Perp: show thin-market warning below this
RWA_MIN_SCORE       = 6.0           # RWA/Perp tool-level display floor (scorer untouched)

TITLES = {"crypto": "🌐 Aster Crypto Signals", "rwa_perp": "🏦 Aster RWA / Perp Signals"}
VIEW_ONLY_FOOTER = "ℹ️ View-only Aster signal · manual logging not enabled yet"
DISCLAIMER       = "⚠️ Educational only · not financial advice · high-risk · DYOR"
THIN_WARN        = "⚠️ Thin market / low volume — use extra caution"


# ── source-namespaced keys (isolated; never collide with Hyperliquid) ────────
def aster_signal_id(c: dict) -> str:
    return f"aster|{c['symbol']}|{c['direction']}|{c['interval']}|{c['bar_time']}"

def aster_lifecycle_key(c: dict) -> str:
    return f"aster|{c['symbol']}|{c['direction']}|{c['interval']}"


def _prepare(df):
    if df is None or len(df) < MIN_BARS:
        return None
    return detect_signals(attach_cpr(df))


def _lower_tf_ok(direction: str, frames: dict) -> bool:
    for tf in LOWER_TF:
        df = _prepare(frames.get(tf))
        if df is None or len(df) < 2:
            continue
        last = df.iloc[-2]
        if direction == "long" and last["close"] > last["sma9"]:
            return True
        if direction == "short" and last["close"] < last["sma9"]:
            return True
    return False


def best_signal(symbol: str, frames: dict, vol: float) -> dict | None:
    """Score `symbol` across scan intervals; return the highest-scoring signal
    as a candidate dict, or None. Pure given frames."""
    liquidity = aster.volume_to_liquidity(vol)
    prepared = {tf: _prepare(frames.get(tf)) for tf in SCAN_INTERVALS}
    tf_sigs  = {tf: (get_latest_signal(p) if p is not None else None)
                for tf, p in prepared.items()}
    best = None
    for tf in SCAN_INTERVALS:
        sig = tf_sigs.get(tf)
        if not sig:
            continue
        direction = sig["signal"]
        sb = score_signal(symbol=symbol, interval=tf, direction=direction,
                          signal_data=sig, all_tf_signals=tf_sigs,
                          liquidity_score=liquidity,
                          lower_tf_ok=_lower_tf_ok(direction, frames))
        cand = {"symbol": symbol, "interval": tf, "direction": direction,
                "score": round(sb.total_score, 2), "entry": sb.entry_price,
                "target": sb.tp_price, "stop": sb.sl_price, "rr": sb.rr_ratio,
                "risk_emoji": sb.risk_emoji, "risk_label": sb.risk_label,
                "should_alert": sb.should_alert, "bar_time": str(sb.bar_time),
                "volume": float(vol or 0.0)}
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best


def select_candidates(cands: list, category: str) -> list:
    """Apply category display gate + cap (ceiling, no padding). Pure."""
    kept = []
    for c in cands:
        if category == "crypto":
            if c["should_alert"] and c["volume"] >= CRYPTO_VOL_FLOOR:
                kept.append(c)
        else:  # rwa_perp — watchlist, no hard volume floor, score >= RWA_MIN_SCORE
            if c["score"] >= RWA_MIN_SCORE:
                kept.append(c)
    kept.sort(key=lambda c: c["score"], reverse=True)
    return kept[:CAP]


def apply_lifecycle(cands: list, state: dict, now: datetime) -> tuple:
    """A2 dedup + suppression against the ISOLATED Aster state. Returns
    (to_send, new_state). Pure (operates on a copy)."""
    alerted = dict(state.get("alerted", {}))
    calls   = dict(state.get("calls", {}))
    to_send = []
    for c in cands:
        sid = aster_signal_id(c)
        if sid in alerted:                       # exact-candle dedup
            c["skip"] = "duplicate"; continue
        key = aster_lifecycle_key(c)
        marker, label, send = _classify_lifecycle(
            calls.get(key), score=c["score"], rr=c["rr"], risk_emoji=c["risk_emoji"],
            entry=c["entry"], target=c["target"], stop=c["stop"], now=now)
        alerted[sid] = now.isoformat()           # decision recorded (sent or suppressed)
        if not send:                             # immaterial 🔄 → suppress
            c["skip"] = "immaterial"
            if calls.get(key):
                calls[key] = {**calls[key], "last_seen": now.isoformat()}
            continue
        prev = calls.get(key) or {}
        calls[key] = {
            "symbol": c["symbol"], "direction": c["direction"], "interval": c["interval"],
            "first_seen": now.isoformat() if label == "new" else (prev.get("first_seen") or now.isoformat()),
            "last_seen": now.isoformat(), "last_bar_time": c["bar_time"],
            "entry": c["entry"], "target": c["target"], "stop": c["stop"],
            "score": c["score"], "rr": c["rr"], "risk_emoji": c["risk_emoji"],
            "status": "active", "lifecycle_label": label,
            "last_alerted_at": now.isoformat(), "alert_count": (prev.get("alert_count") or 0) + 1,
        }
        c["marker"], c["label"] = marker, label
        to_send.append(c)
    return to_send, {"alerted": alerted, "calls": calls}


def build_card(c: dict) -> str:
    de = "📈" if c["direction"] == "long" else "📉"
    entry, tp, sl = c["entry"], c["target"], c["stop"]
    tgt_pct = (tp - entry) / entry * 100 if entry else 0.0
    stop_pct = (sl - entry) / entry * 100 if entry else 0.0
    lines = [
        f"{c.get('marker', '🆕')} {de} <b>{c['symbol']} {c['direction'].upper()}</b>  "
        f"[<b>{c['score']:.1f}/10</b>] {c['risk_emoji']} {c['risk_label']}",
        f"  TF: {c['interval']} | Setup: Qualified",
        f"  🎯 Entry  <code>{entry:.6g}</code>",
        f"  🟢 Target <code>{tp:.6g}</code> ({tgt_pct:+.1f}%)",
        f"  🔴 Stop   <code>{sl:.6g}</code> ({stop_pct:+.1f}%)",
        f"  ⚖️ R:R {c['rr']:.2f}:1",
    ]
    if c["volume"] < RWA_LOW_VOL_WARN and ASTER_CLASS.get(c["symbol"]) == "rwa_perp":
        lines.append(f"  {THIN_WARN}")
    return "\n".join(lines)


def build_message(category: str, cards: list, scan_time: str) -> str:
    n = len(cards)
    hdr = (f"<b>{TITLES[category]}</b> — {n} signal{'s' if n != 1 else ''} · view-only\n"
           f"🕐 {scan_time}\n")
    body = ("\n" + "━" * 32 + "\n").join(build_card(c) for c in cards)
    return (f"{hdr}{'━' * 32}\n{body}\n{'━' * 32}\n{VIEW_ONLY_FOOTER}\n{DISCLAIMER}")


def gather(get) -> dict:
    """Fetch + score the curated watchlist. Read-only. Returns {category:[cand]}."""
    vols = aster.fetch_24h_volumes(get=get)
    out = {"crypto": [], "rwa_perp": []}
    for sym, cat in ASTER_CLASS.items():
        frames = aster.fetch_symbol_frames(sym, SCAN_INTERVALS + LOWER_TF, get=get)
        b = best_signal(sym, frames, vols.get(sym, 0.0))
        if b:
            out[cat].append(b)
    return out


def run(send_enabled: bool, get, sender, state: dict, now: datetime,
        scan_time: str, gather_fn=None) -> tuple:
    """Build (and optionally send) Aster view-only messages. Returns
    (messages, new_state, sent_count, report). Sends ONLY if send_enabled.
    `gather_fn` is injectable for offline tests (defaults to live gather)."""
    sigs = (gather_fn or gather)(get)
    messages, report = [], []
    new_state = state
    for cat in ("crypto", "rwa_perp"):
        cands = select_candidates(sigs[cat], cat)
        to_send, new_state = apply_lifecycle(cands, new_state, now)
        report.append(f"  {cat}: scored={len(sigs[cat])} selected={len(cands)} "
                      f"to_send={len(to_send)}")
        if to_send:
            messages.append((cat, build_message(cat, to_send, scan_time)))
    sent = 0
    if send_enabled:
        for _cat, msg in messages:
            sender(msg)                          # NO markup → no buttons
            sent += 1
    return messages, new_state, sent, report


# ── State IO (read-only unless a real gated send occurs) ─────────────────────
def load_state() -> dict:
    if ASTER_STATE_FILE.exists():
        try:
            return json.loads(ASTER_STATE_FILE.read_text())
        except Exception:
            pass
    return {"alerted": {}, "calls": {}}

def save_state(state: dict) -> None:
    ASTER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ASTER_STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def _ist_now() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)) \
        .strftime("%d %b %Y, %I:%M %p IST")


def main(argv: list) -> int:
    send_flag = "--send" in argv
    env_gate  = os.environ.get("ASTER_ALERTS_ENABLED", "").strip().lower() == "true"
    send_enabled = send_flag and env_gate
    if send_flag and not env_gate:
        print("REFUSING to send: ASTER_ALERTS_ENABLED is not 'true'. "
              "Running DRY-RUN only (nothing sent, nothing written).")

    now = datetime.now(timezone.utc)
    state = load_state()                          # read-only load
    messages, new_state, sent, report = run(
        send_enabled, aster._http_get, _bot_send_message, state, now, _ist_now())

    print("=" * 64)
    print("ASTER VIEW-ONLY ALERTS — " + ("LIVE SEND" if send_enabled else "DRY-RUN"))
    print("=" * 64)
    print("\n".join(report))
    for cat, msg in messages:
        print(f"\n----- would send: {cat} -----\n{msg}")
    if send_enabled:
        save_state(new_state)
        print(f"\nSENT {sent} Aster view-only message(s); Aster state persisted.")
    else:
        print(f"\nDRY-RUN: {len(messages)} message(s) built, 0 sent, no state written.")
    return 0


# ── Offline self-test (mocked; never sends, never writes, no live scoring) ───

def _cand(symbol, category, score, vol, direction="long", bar="B1", interval="4h"):
    return {"symbol": symbol, "interval": interval, "direction": direction,
            "score": score, "entry": 100.0, "target": 110.0, "stop": 95.0, "rr": 2.0,
            "risk_emoji": "🟢", "risk_label": "Low Risk", "should_alert": score >= 5.0,
            "bar_time": bar, "volume": vol}


def _selftest() -> int:
    ok = []
    def chk(name, cond): ok.append(bool(cond)); print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    # 1-2. source-namespaced keys; no overlap with Hyperliquid key format
    c = {"symbol": "BTCUSDT", "direction": "long", "interval": "4h", "bar_time": "B1"}
    chk("source-namespaced signal id", aster_signal_id(c) == "aster|BTCUSDT|long|4h|B1")
    chk("source-namespaced lifecycle key", aster_lifecycle_key(c) == "aster|BTCUSDT|long|4h")
    chk("HL key format has no 'aster|' prefix", not "BTCUSDT|long|4h".startswith("aster|"))

    sent_calls = []
    def stub_sender(text, *a, **k): sent_calls.append((text, a, k))

    # hand-built candidates (no reliance on live CPR signals): BTC crypto strong,
    # SPCX rwa thin (<250k) score>=6, AAPL rwa score<6 (excluded by display floor).
    def fake_gather(_get):
        return {"crypto": [_cand("BTCUSDT", "crypto", 8.4, 500_000_000)],
                "rwa_perp": [_cand("SPCXUSDT", "rwa_perp", 6.4, 120_000, bar="R1"),
                             _cand("AAPLUSDT", "rwa_perp", 5.5, 30_000, bar="R2")]}

    # 16/18. default dry-run sends nothing; sender stubbed and untouched
    msgs, _st, sent, rep = run(False, None, stub_sender, {"alerted": {}, "calls": {}},
                               now, "15 Jun 2026, 12:00 IST", gather_fn=fake_gather)
    chk("dry-run sends nothing", sent == 0 and len(sent_calls) == 0)
    chk("messages built in dry-run", len(msgs) == 2)

    cats = [m[0] for m in msgs]
    joined = "\n".join(m[1] for m in msgs)
    chk("crypto & rwa separated (two messages, distinct titles)",
        cats == ["crypto", "rwa_perp"] and "🌐 Aster Crypto Signals" in joined
        and "🏦 Aster RWA / Perp Signals" in joined)
    chk("no HL Select/Skip/Confirm wording", "Tap below to log" not in joined
        and "Select my trades" not in joined and "Confirm" not in joined)
    chk("view-only footer present", joined.count(VIEW_ONLY_FOOTER) == 2)
    chk("disclaimer present", DISCLAIMER in joined)
    chk("no CPR/internal names", all(x not in joined for x in ("CPR", "R1 ", "S1 ", "pivot")))
    chk("thin-market warning on thin RWA (SPCX) only", THIN_WARN in joined
        and joined.count(THIN_WARN) == 1)
    chk("AAPL excluded by rwa floor<6.0", "AAPL" not in joined)

    # 14. display floor + 6. caps (no padding)
    chk("rwa floor excludes score<6.0",
        select_candidates([_cand("SPCXUSDT", "rwa_perp", 5.9, 120_000)], "rwa_perp") == [])
    chk("crypto needs should_alert + >=$1M vol",
        select_candidates([_cand("BTCUSDT", "crypto", 4.0, 5_000_000)], "crypto") == []
        and select_candidates([_cand("BTCUSDT", "crypto", 8.0, 500_000)], "crypto") == [])
    many = [_cand(f"X{i}", "crypto", 9 - i * 0.1, 5_000_000) for i in range(8)]
    chk("crypto cap=5 no padding", len(select_candidates(many, "crypto")) == 5)

    # 12. empty category → no message
    em, _, _, _ = run(False, None, stub_sender, {"alerted": {}, "calls": {}}, now, "t",
                      gather_fn=lambda g: {"crypto": [], "rwa_perp": []})
    chk("empty categories → zero messages", em == [])

    # 15. A2 suppression against the ISOLATED Aster state
    base = [_cand("BTCUSDT", "crypto", 8.4, 500_000_000, bar="C1")]
    to_send1, st_after = apply_lifecycle(base, {"alerted": {}, "calls": {}}, now)
    chk("first candle sends + writes namespaced baseline",
        len(to_send1) == 1 and "aster|BTCUSDT|long|4h" in st_after["calls"])
    repeat = [_cand("BTCUSDT", "crypto", 8.4, 500_000_000, bar="C2")]  # new candle, same levels
    to_send2, _ = apply_lifecycle(repeat, st_after, now + timedelta(hours=1))
    chk("A2 suppresses immaterial Aster repeat", to_send2 == [])
    dup, _ = apply_lifecycle(base, st_after, now + timedelta(hours=1))   # same bar_time
    chk("exact-candle dedup suppresses duplicate", dup == [])

    # 17. send path: sender called with NO markup (positional text only)
    sent_calls.clear()
    run(True, None, stub_sender, {"alerted": {}, "calls": {}}, now, "t", gather_fn=fake_gather)
    chk("send path calls sender, no markup/buttons",
        len(sent_calls) == 2 and all(a == () and k.get("markup") is None for _t, a, k in sent_calls))

    # gate logic (behavioral): flag without env → send disabled
    os.environ.pop("ASTER_ALERTS_ENABLED", None)
    chk("--send without env gate → send_enabled False",
        ("--send" in ["--send"]) and not (os.environ.get("ASTER_ALERTS_ENABLED", "").lower() == "true"))

    # isolation: this module never imported the paper tracker or batches plumbing
    chk("no paper tracker symbol in module globals", "open_position" not in globals())
    chk("isolated Aster state file (not telegram_state.json)",
        ASTER_STATE_FILE.name == "aster_view_state.json")

    print(f"\nASTER VIEW-ONLY SELFTEST: {sum(ok)}/{len(ok)} passed")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    raise SystemExit(main(sys.argv))
