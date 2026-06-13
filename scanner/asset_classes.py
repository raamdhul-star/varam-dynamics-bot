"""
scanner/asset_classes.py — static, human-reviewed asset classification
======================================================================
Maps Hyperliquid symbols to an asset class for the separated Telegram
alert UX (Sprint C). This is a STATIC reviewed list: NO name heuristics,
NO guessing. A symbol absent from ASSET_CLASS is 'uncategorized' and is
EXCLUDED from category alerts + reported — never silently bucketed.

Categories:
  crypto        — L1/L2 tokens, DeFi, AI, memes, infra, general crypto
  rwa_perp      — tokenized real-world assets, real-yield / yield-tokenization,
                  synthetic-dollar protocols, perp/derivatives infrastructure
  uncategorized — not reviewed yet (default for any unknown symbol)

The live universe is dynamic (re-fetched ~every 6h, volume-filtered), so this
map is a maintained SUPERSET. New listings appear as 'uncategorized' until
reviewed; run output logs them so they can be classified here.
"""
from __future__ import annotations

ALLOWED_CLASSES = ("crypto", "rwa_perp", "uncategorized")

ASSET_CLASS: dict[str, str] = {
    # ── crypto (L1/L2, DeFi, AI, infra, memes) ──
    "BTC": "crypto", "ETH": "crypto", "SOL": "crypto", "XRP": "crypto",
    "BNB": "crypto", "DOGE": "crypto", "ADA": "crypto", "AVAX": "crypto",
    "LINK": "crypto", "LTC": "crypto", "BCH": "crypto", "TRX": "crypto",
    "XLM": "crypto", "DOT": "crypto", "NEAR": "crypto", "SUI": "crypto",
    "APT": "crypto", "ARB": "crypto", "OP": "crypto", "ICP": "crypto",
    "SEI": "crypto", "TON": "crypto", "XMR": "crypto", "ZEC": "crypto",
    "ATOM": "crypto", "TIA": "crypto", "STX": "crypto", "HYPE": "crypto",
    "TAO": "crypto", "FET": "crypto", "RENDER": "crypto", "GRASS": "crypto",
    "VIRTUAL": "crypto", "MORPHO": "crypto", "INJ": "crypto", "AAVE": "crypto",
    "CRV": "crypto", "UNI": "crypto", "JTO": "crypto", "JUP": "crypto",
    "ETHFI": "crypto", "ZRO": "crypto", "WLD": "crypto", "PYTH": "crypto",
    "LDO": "crypto", "PEPE": "crypto", "kPEPE": "crypto", "kSHIB": "crypto",
    "WIF": "crypto", "ORDI": "crypto", "FARTCOIN": "crypto", "PENGU": "crypto",
    "TRUMP": "crypto", "PUMP": "crypto",
    # ── rwa_perp (reviewed 2026-06-13) ──
    "PAXG": "rwa_perp", "ONDO": "rwa_perp", "PENDLE": "rwa_perp",
    "ENA": "rwa_perp", "WLFI": "rwa_perp", "ASTER": "rwa_perp",
    # NOTE: XPL, MEGA, VVV, LIT deliberately left OUT ⇒ 'uncategorized' (pending review).
}


def classify(symbol: str) -> str:
    """Return the reviewed class for `symbol`, or 'uncategorized' if absent.
    Never guesses from the name."""
    return ASSET_CLASS.get(symbol, "uncategorized")
