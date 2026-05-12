"""
scanner/assets.py  (v2 — dynamic full universe)
================================================
Fetches ALL assets from Hyperliquid API at runtime.
Filters by minimum $1M daily volume to exclude illiquid traps.
Falls back to curated list if API unavailable.
"""
from __future__ import annotations
import json, time
from pathlib import Path
from datetime import datetime, timezone
import requests

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
CACHE_FILE      = Path(__file__).parent.parent / "results" / "asset_cache.json"
CACHE_TTL_HOURS = 6
MIN_VOLUME_USD  = 1_000_000   # $1M minimum daily volume

TIER_SCORE = {1: 1.0, 2: 0.8, 3: 0.5, 4: 0.2}

FALLBACK_ASSETS: list[tuple[str, int, int, int]] = [
    ("BTC",50,5,1),("ETH",50,4,1),("SOL",20,2,2),("DOGE",20,0,2),
    ("XRP",20,1,2),("BNB",15,3,2),("AVAX",20,2,2),("LINK",20,1,2),
    ("UNI",20,1,2),("ADA",20,0,2),("DOT",20,1,2),("ATOM",20,2,2),
    ("NEAR",20,1,2),("APT",20,2,2),("ARB",20,0,2),("OP",20,0,2),
    ("SUI",20,1,3),("INJ",20,2,3),("TIA",20,1,3),("SEI",20,0,3),
    ("WLD",20,1,3),("STX",20,1,3),("PEPE",20,0,3),("WIF",20,1,3),
    ("ORDI",20,2,3),("PENDLE",20,1,3),("JTO",20,1,3),("PYTH",20,0,3),
    ("JUP",20,0,3),("AAVE",10,2,4),("CRV",10,0,4),("LDO",10,1,4),
    ("GMX",10,2,4),("DYDX",10,1,4),
]


def fetch_meta() -> list[dict] | None:
    try:
        r = requests.post(HYPERLIQUID_INFO_URL,
                          json={"type": "meta"},
                          headers={"Content-Type": "application/json"},
                          timeout=15)
        r.raise_for_status()
        return r.json().get("universe", [])
    except Exception as e:
        print(f"[assets] meta fetch failed: {e}")
        return None


def fetch_volumes() -> dict[str, float]:
    try:
        r = requests.post(HYPERLIQUID_INFO_URL,
                          json={"type": "metaAndAssetCtxs"},
                          headers={"Content-Type": "application/json"},
                          timeout=15)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return {}
        meta, ctxs = data[0].get("universe", []), data[1]
        return {a.get("name",""): float(c.get("dayNtlVlm", 0))
                for a, c in zip(meta, ctxs) if a.get("name")}
    except Exception as e:
        print(f"[assets] volume fetch failed: {e}")
        return {}


def build_asset_list(min_vol: float = MIN_VOLUME_USD) -> list[dict]:
    meta    = fetch_meta()
    volumes = fetch_volumes()
    if not meta:
        print("[assets] API unavailable — using fallback list")
        return _fallback()

    assets, skipped_vol, skipped_other = [], 0, 0
    for item in meta:
        sym = item.get("name", "")
        if not sym or len(sym) > 12 or sym.startswith("@"):
            skipped_other += 1; continue
        vol = volumes.get(sym, 0.0)
        if vol < min_vol:
            skipped_vol += 1; continue

        tier = (1 if vol >= 100_000_000 else
                2 if vol >= 10_000_000  else
                3 if vol >= 5_000_000   else 4)

        assets.append({
            "symbol":          sym,
            "max_leverage":    int(item.get("maxLeverage", 10)),
            "sz_decimals":     int(item.get("szDecimals",  2)),
            "volume_usd":      vol,
            "tier":            tier,
            "liquidity_score": TIER_SCORE[tier],
        })

    assets.sort(key=lambda x: x["volume_usd"], reverse=True)
    print(f"[assets] {len(assets)} assets pass ${min_vol/1e6:.0f}M volume filter "
          f"(skipped {skipped_vol} low-vol, {skipped_other} other)")
    return assets


def _fallback() -> list[dict]:
    return [{"symbol":sym,"max_leverage":lev,"sz_decimals":dec,
             "volume_usd":0.0,"tier":tier,"liquidity_score":TIER_SCORE.get(tier,0.5)}
            for sym,lev,dec,tier in FALLBACK_ASSETS]


def get_assets(force_refresh: bool = False) -> list[dict]:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not force_refresh and CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text())
            age   = (time.time() - cache.get("timestamp", 0)) / 3600
            if age < CACHE_TTL_HOURS:
                print(f"[assets] Cache hit: {len(cache['assets'])} assets (age {age:.1f}h)")
                return cache["assets"]
        except Exception:
            pass
    assets = build_asset_list()
    try:
        CACHE_FILE.write_text(json.dumps(
            {"timestamp": time.time(),
             "fetched_at": datetime.now(timezone.utc).isoformat(),
             "assets": assets}, indent=2))
    except Exception as e:
        print(f"[assets] Cache write failed: {e}")
    return assets


def get_symbols(force_refresh: bool = False) -> list[str]:
    return [a["symbol"] for a in get_assets(force_refresh)]


def _map() -> dict[str, dict]:
    return {a["symbol"]: a for a in get_assets()}


def max_leverage(symbol: str) -> int:
    return _map().get(symbol, {}).get("max_leverage", 10)


def liquidity_score(symbol: str) -> float:
    return _map().get(symbol, {}).get("liquidity_score", 0.5)


def sz_decimals_for(symbol: str) -> int:
    return _map().get(symbol, {}).get("sz_decimals", 2)


def calc_leverage(entry: float, sl: float,
                  account_size: float, risk_pct: float,
                  symbol: str) -> dict:
    if entry <= 0 or sl <= 0:
        return {"error": "Invalid prices", "leverage": 1,
                "position_sz": account_size, "qty": 0,
                "risk_usd": account_size * risk_pct, "sl_pct": 0,
                "max_leverage": max_leverage(symbol), "capped": False}
    sl_dist  = abs(entry - sl)
    sl_pct   = sl_dist / entry
    if sl_pct <= 0:
        sl_pct = 0.01
    risk_usd = account_size * risk_pct
    raw_lev  = risk_usd / (account_size * sl_pct)
    max_lev  = max_leverage(symbol)
    final    = min(round(raw_lev, 1), max_lev)
    pos_sz   = account_size * final
    dec      = sz_decimals_for(symbol)
    qty      = round(pos_sz / entry, dec)
    return {
        "leverage":     final,
        "position_sz":  round(pos_sz, 2),
        "qty":          qty,
        "risk_usd":     round(risk_usd, 2),
        "sl_pct":       round(sl_pct * 100, 2),
        "max_leverage": max_lev,
        "capped":       raw_lev > max_lev,
    }


if __name__ == "__main__":
    assets = get_assets(force_refresh=True)
    print(f"\nTotal: {len(assets)} assets")
    for a in assets[:10]:
        print(f"  {a['symbol']:<10} ${a['volume_usd']/1e6:>8.1f}M  "
              f"maxLev={a['max_leverage']}x  tier={a['tier']}")
