"""
tools/aster_discover.py — Sprint D1: Aster market DISCOVERY (read-only)
======================================================================
Hits Aster's PUBLIC futures market-data endpoints and prints a report of the
available markets, 24h quote volume, and CANDIDATE category suggestions for
human review. It is purely informational:

  • read-only — no API key, no auth, no signed requests
  • writes NOTHING (no results/, no state, no files)
  • sends NO Telegram messages
  • does NOT touch or import the live Hyperliquid scanner/alert flow
  • candidate labels are SUGGESTIONS ONLY — they do NOT update
    scanner/asset_classes.py or any live classification

Public endpoints used (base https://fapi.asterdex.com, Binance-style):
  GET /fapi/v1/exchangeInfo   — symbol list + metadata
  GET /fapi/v1/ticker/24hr    — 24h volume / quote volume

Usage:
  python tools/aster_discover.py            # live read-only discovery report
  python tools/aster_discover.py --selftest # offline tests with mocked data
"""
from __future__ import annotations

import sys

try:
    import requests
except Exception:                      # pragma: no cover - only if env lacks requests
    requests = None

ASTER_BASE      = "https://fapi.asterdex.com"
EXCHANGE_INFO   = "/fapi/v1/exchangeInfo"
TICKER_24H      = "/fapi/v1/ticker/24hr"
TIMEOUT_S       = 15

# Timeframes of interest (reference only — discovery does not fetch candles).
TIMEFRAMES_OF_INTEREST = ["1h", "4h", "1d", "1w"]
VOLUME_FLOOR_REF       = 1_000_000     # $1M reference floor (HL parity) — report only

# ── Conservative keyword sets for CANDIDATE suggestions (review only) ────────
# Well-known equity / index / pre-IPO / private-company tickers & names. A base
# asset matching these is flagged candidate_rwa_perp for HUMAN verification.
STOCK_INDEX_KEYWORDS = {
    "SPACEX", "SPCX", "SPX500", "SP500", "SPY", "SPX",
    "MSTR", "OPENAI", "ANTHROPIC", "NVDA", "TSLA", "AAPL", "MSFT",
    "GOOGL", "GOOG", "AMZN", "META", "COIN", "HOOD", "CRCL", "AMD",
    "NFLX", "BABA", "GME", "PLTR", "NDX", "DJI", "QQQ", "STRIPE", "XAU",
}
# Known crypto-native base assets (broad). Base in this set ⇒ candidate_crypto.
KNOWN_CRYPTO = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "LTC",
    "BCH", "TRX", "XLM", "DOT", "NEAR", "SUI", "APT", "ARB", "OP", "ICP",
    "SEI", "TON", "XMR", "ZEC", "ATOM", "TIA", "STX", "HYPE", "TAO", "FET",
    "RENDER", "GRASS", "VIRTUAL", "MORPHO", "INJ", "AAVE", "CRV", "UNI", "JTO",
    "JUP", "ETHFI", "ZRO", "WLD", "PYTH", "LDO", "PEPE", "WIF", "ORDI",
    "FARTCOIN", "PENGU", "TRUMP", "PUMP", "ASTER", "ONDO", "PENDLE", "ENA",
    "WLFI", "PAXG", "SPX6900", "BERA", "EIGEN", "ENS", "MKR", "COMP", "GMX",
}
# Names we specifically want exact Aster symbols for (D1 report requirement).
WATCHLIST_KEYWORDS = {"SPACEX", "SPCX", "OPENAI", "ANTHROPIC", "MSTR",
                      "SPX500", "SP500", "SPY", "SPX"}


# ── Pure, offline-testable helpers ───────────────────────────────────────────

def parse_markets(exchange_info: dict) -> list[dict]:
    """Extract a clean market list from an exchangeInfo payload. Pure."""
    out = []
    for s in (exchange_info or {}).get("symbols", []) or []:
        sym = s.get("symbol")
        if not sym:
            continue
        out.append({
            "symbol":        sym,
            "base":          s.get("baseAsset", ""),
            "quote":         s.get("quoteAsset", ""),
            "status":        s.get("status", ""),
            "contract_type": s.get("contractType", ""),
        })
    return out


def volume_map(ticker_24h: list) -> dict:
    """Map symbol -> 24h quote volume (float). Tolerant of strings/missing. Pure."""
    vm = {}
    for t in ticker_24h or []:
        sym = t.get("symbol")
        if not sym:
            continue
        raw = t.get("quoteVolume", t.get("quoteAssetVolume", 0)) or 0
        try:
            vm[sym] = float(raw)
        except (TypeError, ValueError):
            vm[sym] = 0.0
    return vm


def join_markets(markets: list, vmap: dict) -> list[dict]:
    """Attach quote_volume to each market (0.0 if absent). Pure, non-mutating."""
    joined = []
    for m in markets:
        j = dict(m)
        j["quote_volume"] = float(vmap.get(m["symbol"], 0.0))
        joined.append(j)
    return joined


def candidate_class(base: str, symbol: str) -> str:
    """CANDIDATE suggestion only (never authoritative): 'candidate_rwa_perp'
    for known equity/index/pre-IPO names, 'candidate_crypto' for known crypto
    bases, else 'unknown'. Conservative — unknowns are NOT guessed."""
    b = (base or "").upper()
    s = (symbol or "").upper()
    # equity/index/pre-IPO keyword match (substring on base, exact-ish on symbol)
    for kw in STOCK_INDEX_KEYWORDS:
        if b == kw or kw in b:
            return "candidate_rwa_perp"
    if b in KNOWN_CRYPTO:
        return "candidate_crypto"
    # last-resort: keyword appears anywhere in the raw symbol
    for kw in STOCK_INDEX_KEYWORDS:
        if kw in s:
            return "candidate_rwa_perp"
    return "unknown"


def find_watchlist(markets: list) -> list[dict]:
    """Return markets whose base/symbol matches the SpaceX/OpenAI/Anthropic/
    MSTR/SP500-style watchlist (exact Aster symbols requested in D1). Pure."""
    hits = []
    for m in markets:
        b, s = (m.get("base") or "").upper(), (m.get("symbol") or "").upper()
        if any(kw == b or kw in b or kw in s for kw in WATCHLIST_KEYWORDS):
            hits.append(m)
    return hits


def classify_all(joined: list) -> dict:
    """Bucket joined markets into candidate_crypto / candidate_rwa_perp /
    unknown. Returns {bucket: [market,...]}. Pure."""
    buckets = {"candidate_crypto": [], "candidate_rwa_perp": [], "unknown": []}
    for m in joined:
        buckets[candidate_class(m.get("base", ""), m["symbol"])].append(m)
    return buckets


def build_report(joined: list, top_n: int = 20) -> str:
    """Render the human-readable discovery report. Pure (no I/O)."""
    lines = []
    n = len(joined)
    trading = [m for m in joined if str(m.get("status", "")).upper() in ("TRADING", "")]
    lines.append("=" * 64)
    lines.append("ASTER MARKET DISCOVERY (read-only · suggestions only · no live change)")
    lines.append("=" * 64)
    lines.append(f"Total markets found: {n}")
    lines.append(f"Markets with status TRADING (or unspecified): {len(trading)}")
    lines.append(f"Reference volume floor: ${VOLUME_FLOOR_REF:,} | TFs of interest: "
                 f"{', '.join(TIMEFRAMES_OF_INTEREST)}")

    above = [m for m in joined if m['quote_volume'] >= VOLUME_FLOOR_REF]
    lines.append(f"Markets >= ${VOLUME_FLOOR_REF/1e6:.0f}M 24h quote volume: {len(above)}")

    top = sorted(joined, key=lambda m: m["quote_volume"], reverse=True)[:top_n]
    lines.append(f"\n--- Top {len(top)} markets by 24h quote volume ---")
    for m in top:
        lines.append(f"  {m['symbol']:<16} {m.get('base',''):<10} "
                     f"vol=${m['quote_volume']/1e6:>10.2f}M  "
                     f"[{candidate_class(m.get('base',''), m['symbol'])}]")

    watch = find_watchlist(joined)
    lines.append("\n--- Watchlist (SpaceX / OpenAI / Anthropic / MSTR / SP500-style) ---")
    if watch:
        for m in watch:
            lines.append(f"  EXACT SYMBOL: {m['symbol']:<16} base={m.get('base','')} "
                         f"status={m.get('status','')} vol=${m['quote_volume']/1e6:.2f}M")
    else:
        lines.append("  (none found in current Aster universe)")

    buckets = classify_all(joined)
    for name in ("candidate_crypto", "candidate_rwa_perp", "unknown"):
        syms = sorted(m["symbol"] for m in buckets[name])
        lines.append(f"\n--- {name} ({len(syms)}) ---")
        lines.append("  " + (", ".join(syms) if syms else "(none)"))

    lines.append("\n" + "!" * 64)
    lines.append("NOTE: candidate_* labels are SUGGESTIONS FOR REVIEW ONLY.")
    lines.append("They do NOT update scanner/asset_classes.py or any live classification.")
    lines.append("!" * 64)
    return "\n".join(lines)


# ── Live fetch (read-only) ───────────────────────────────────────────────────

def _http_get(path: str):
    """GET a public Aster endpoint. Read-only, no auth. Returns parsed JSON or
    raises. Injected/replaced in tests."""
    if requests is None:
        raise RuntimeError("requests not available")
    r = requests.get(ASTER_BASE + path,
                     headers={"Content-Type": "application/json"}, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def discover(get=_http_get) -> str:
    """Fetch exchangeInfo + 24h ticker (read-only) and build the report.
    `get` is injectable for offline testing. Fails gracefully."""
    try:
        info = get(EXCHANGE_INFO)
    except Exception as e:
        return f"[aster_discover] exchangeInfo fetch FAILED (read-only, no harm): {e}"
    try:
        ticks = get(TICKER_24H)
        if isinstance(ticks, dict):     # single-symbol shape guard
            ticks = [ticks]
    except Exception as e:
        print(f"[aster_discover] ticker/24hr fetch failed ({e}) — continuing without volume")
        ticks = []
    markets = parse_markets(info)
    joined  = join_markets(markets, volume_map(ticks))
    return build_report(joined)


# ── Offline self-test (mocked data; asserts pure helpers) ────────────────────

def _selftest() -> int:
    info = {"symbols": [
        {"symbol": "BTCUSDT",   "baseAsset": "BTC",   "quoteAsset": "USDT", "status": "TRADING"},
        {"symbol": "ETHUSDT",   "baseAsset": "ETH",   "quoteAsset": "USDT", "status": "TRADING"},
        {"symbol": "MSTRUSDT",  "baseAsset": "MSTR",  "quoteAsset": "USDT", "status": "TRADING"},
        {"symbol": "SPACEXUSDT","baseAsset": "SPACEX","quoteAsset": "USDT", "status": "TRADING"},
        {"symbol": "FOOBARUSDT","baseAsset": "FOOBAR","quoteAsset": "USDT", "status": "TRADING"},
    ]}
    ticks = [
        {"symbol": "BTCUSDT",   "quoteVolume": "500000000"},
        {"symbol": "ETHUSDT",   "quoteVolume": "120000000"},
        {"symbol": "MSTRUSDT",  "quoteVolume": "3000000"},
        {"symbol": "SPACEXUSDT","quoteVolume": "9000000"},
        # FOOBARUSDT intentionally missing volume
    ]
    ok = []
    def chk(name, cond): ok.append(cond); print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    markets = parse_markets(info)
    chk("parse_markets count", len(markets) == 5)
    vm = volume_map(ticks)
    chk("volume_map parses strings", vm["BTCUSDT"] == 500000000.0)
    joined = join_markets(markets, vm)
    chk("join missing volume -> 0.0", next(m for m in joined if m["symbol"] == "FOOBARUSDT")["quote_volume"] == 0.0)
    chk("candidate crypto (BTC)", candidate_class("BTC", "BTCUSDT") == "candidate_crypto")
    chk("candidate rwa_perp (MSTR)", candidate_class("MSTR", "MSTRUSDT") == "candidate_rwa_perp")
    chk("candidate rwa_perp (SPACEX)", candidate_class("SPACEX", "SPACEXUSDT") == "candidate_rwa_perp")
    chk("unknown stays unknown", candidate_class("FOOBAR", "FOOBARUSDT") == "unknown")
    watch = find_watchlist(markets)
    chk("watchlist finds MSTR+SPACEX", {m["symbol"] for m in watch} == {"MSTRUSDT", "SPACEXUSDT"})
    b = classify_all(joined)
    chk("buckets split", len(b["candidate_crypto"]) == 2 and len(b["candidate_rwa_perp"]) == 2
        and len(b["unknown"]) == 1)
    rep = build_report(joined)
    chk("report mentions EXACT SYMBOL + suggestion note",
        "EXACT SYMBOL" in rep and "SUGGESTIONS FOR REVIEW ONLY" in rep)
    print(f"\nSELFTEST: {sum(ok)}/{len(ok)} passed")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(discover())
