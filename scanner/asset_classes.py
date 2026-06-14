"""
scanner/asset_classes.py — static, human-reviewed asset classification
======================================================================
Maps Hyperliquid symbols to an asset class for the separated Telegram
alert UX (Sprint C, corrected in C1). This is a STATIC reviewed list:
NO name heuristics, NO guessing. A symbol absent from ASSET_CLASS is
'uncategorized' and is EXCLUDED from category alerts + reported — never
silently bucketed.

Categories (corrected definition — Sprint C1, 2026-06-13):
  crypto        — normal crypto coins/tokens: L1/L2, DeFi, AI, memes, infra,
                  crypto-native RWA-themed tokens (ONDO), yield / synthetic-
                  dollar tokens (PENDLE, ENA, WLFI), and tokenized crypto-native
                  commodity tokens (PAXG). All of these are CRYPTO for grouping.
  rwa_perp      — TRUE non-crypto market-style instruments ONLY: stocks, stock
                  perps, index perps, private-company / pre-IPO perps, equity-
                  style perps (e.g., MSTR, SpaceX, SP500/SPY-style). Add symbols
                  here ONLY when such an instrument actually appears in the
                  scanner universe. Crypto-native tokens NEVER go here.
  uncategorized — not reviewed yet (default for any unknown symbol)

The live universe is dynamic (re-fetched ~every 6h, volume-filtered), so this
map is a maintained SUPERSET. New listings appear as 'uncategorized' until
reviewed; run output logs them so they can be classified here.

NOTE: Hyperliquid's standard perp universe (the `meta` endpoint this scanner
queries) is CRYPTO-ONLY — it currently contains no stocks/indices/company
perps, so `rwa_perp` is intentionally EMPTY. `SPX` on HL standard perps is the
SPX6900 MEMECOIN (crypto), NOT the S&P 500 index — pinned to crypto defensively.
"""
from __future__ import annotations

ALLOWED_CLASSES = ("crypto", "rwa_perp", "uncategorized")

ASSET_CLASS: dict[str, str] = {
    # ── crypto: majors / L1 / L2 ──
    "BTC": "crypto", "ETH": "crypto", "SOL": "crypto", "XRP": "crypto",
    "BNB": "crypto", "DOGE": "crypto", "ADA": "crypto", "AVAX": "crypto",
    "LINK": "crypto", "LTC": "crypto", "BCH": "crypto", "TRX": "crypto",
    "XLM": "crypto", "DOT": "crypto", "NEAR": "crypto", "SUI": "crypto",
    "APT": "crypto", "ARB": "crypto", "OP": "crypto", "ICP": "crypto",
    "SEI": "crypto", "TON": "crypto", "XMR": "crypto", "ZEC": "crypto",
    "ATOM": "crypto", "TIA": "crypto", "STX": "crypto", "HYPE": "crypto",
    # ── crypto: AI / DeFi / infra ──
    "TAO": "crypto", "FET": "crypto", "RENDER": "crypto", "GRASS": "crypto",
    "VIRTUAL": "crypto", "MORPHO": "crypto", "INJ": "crypto", "AAVE": "crypto",
    "CRV": "crypto", "UNI": "crypto", "JTO": "crypto", "JUP": "crypto",
    "ETHFI": "crypto", "ZRO": "crypto", "WLD": "crypto", "PYTH": "crypto",
    "LDO": "crypto",
    # ── crypto: memes ──
    "PEPE": "crypto", "kPEPE": "crypto", "kSHIB": "crypto", "WIF": "crypto",
    "ORDI": "crypto", "FARTCOIN": "crypto", "PENGU": "crypto", "TRUMP": "crypto",
    "PUMP": "crypto",
    # ── crypto: crypto-native RWA-themed / yield / synthetic-dollar / tokenized
    # commodity (Sprint C1 — these are CRYPTO, not rwa_perp) ──
    "ONDO": "crypto", "PENDLE": "crypto", "ENA": "crypto", "WLFI": "crypto",
    "ASTER": "crypto", "PAXG": "crypto",
    # ── crypto: newly reviewed (Sprint C1, 2026-06-13) ──
    "VVV": "crypto", "XPL": "crypto", "LIT": "crypto", "MEGA": "crypto",
    "IP": "crypto", "NIL": "crypto", "CHIP": "crypto",
    # ── crypto: defensive guard — SPX on HL = SPX6900 memecoin, NOT S&P 500 ──
    "SPX": "crypto",

    # ── rwa_perp: TRUE non-crypto stock/index/company perps ──
    # Intentionally EMPTY — none exist in the current scanner universe.
    # Add real instruments here ONLY when they appear (e.g. "MSTR": "rwa_perp").
}


def classify(symbol: str) -> str:
    """Return the reviewed class for `symbol`, or 'uncategorized' if absent.
    Never guesses from the name."""
    return ASSET_CLASS.get(symbol, "uncategorized")
