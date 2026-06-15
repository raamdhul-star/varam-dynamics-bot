"""
api/telegram.py — Varam-Dynamics Telegram webhook (Phase 2F, Vercel Python BETA)
==============================================================================
SCOPE (Phase 1 + 2A..2E-a + 2F invite-only):
  • verify the Telegram secret header (X-Telegram-Bot-Api-Secret-Token)
  • PUBLIC: /start /whoami /help /request_access
  • APPROVED-ONLY: SELECT/TOGGLE/DONE/LEV/SZ/CONFIRM, /mytrades, CLOSE/PX,
    manual close-price messages  (full intake + close lifecycle)
  • ADMIN-ONLY: /pending, AP:<index> (approve), RJ:<index> (reject)

Identity = numeric from.id (username/display are display-only). Admin =
ADMIN_CHAT_ID and is IMPLICITLY approved (never locked out). Allowlist lives in
Upstash (users:allowlist / users:pending, both NO TTL). Phase 2F writes to
UPSTASH ONLY — no GitHub / results/ / CSV / paper writes. NOT YET: CSV sync
(2E-b, needs token), cancel/abandon, multi-admin, webhook cut-over.

Self-contained: standard library only; does NOT import telegram/bot.py (whose
import-time mkdir() would be fragile on Vercel's read-only filesystem). The two
tiny pure keyboard builders below mirror telegram/bot.py exactly.

Secrets are read from environment variables and NEVER printed.
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import base64
import csv
import io
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone


# ── Config ──────────────────────────────────────────────────────────────────
GITHUB_REPO = os.environ.get("GITHUB_REPO", "raamdhul-star/varam-dynamics-bot")
GITHUB_REF  = os.environ.get("GITHUB_REF", "main")


# ── Telegram API (stdlib only; no 'requests' dependency) ────────────────────

def _tg(method: str, payload: dict) -> dict | None:
    """Best-effort Telegram call. Returns None if no token (e.g. local test)
    or on any error. Never prints the token."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[webhook] {method} failed: {str(e).replace(token, '***')}")
        return None


def _verify_secret(secret_header: str | None) -> bool:
    """True only if WEBHOOK_SECRET is configured AND matches the header."""
    expected = os.environ.get("WEBHOOK_SECRET", "")
    return bool(expected) and secret_header == expected


# ── Read-only state fetch (GitHub Contents API; public repo => no token) ────

def _load_remote_state() -> dict:
    """Read results/telegram_state.json from GitHub, read-only. Returns {} on
    any error. NEVER writes anything."""
    url = (f"https://api.github.com/repos/{GITHUB_REPO}"
           f"/contents/results/telegram_state.json?ref={GITHUB_REF}")
    headers = {"Accept": "application/vnd.github.raw+json",
               "User-Agent": "varam-dynamics-webhook"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[webhook] state fetch failed: {e}")
        return {}


# ── Upstash Redis REST session store (stdlib only; no new dependency) ───────
SESSION_TTL = 1800     # 30 minutes — ONLY for the in-progress button session
MAX_SELECT  = 5
LEV_OPTIONS = [2, 3, 5, 7, 10, 15, 20, 25, 50]   # mirror telegram/bot.py
SIZE_PCT    = [25, 50, 75, 100]                  # mirror telegram/bot.py
RISK_PCT    = 0.07                               # 7% account risk per trade

def _kv_creds() -> tuple[str, str]:
    url = (os.environ.get("UPSTASH_REDIS_REST_URL")
           or os.environ.get("KV_REST_API_URL") or "")
    tok = (os.environ.get("UPSTASH_REDIS_REST_TOKEN")
           or os.environ.get("KV_REST_API_TOKEN") or "")
    return url.rstrip("/"), tok

def _kv_cmd(args: list):
    """Run one Redis command via Upstash REST. Returns result, or None on any
    failure (missing creds, network, error). Never prints token or values."""
    url, tok = _kv_creds()
    if not url or not tok:
        return None
    try:
        req = urllib.request.Request(
            url, data=json.dumps(args).encode(),
            headers={"Authorization": f"Bearer {tok}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()).get("result")
    except Exception as e:
        print(f"[webhook] kv error: {type(e).__name__}")
        return None

def _kv_get(key: str):
    res = _kv_cmd(["GET", key])
    if not res:
        return None
    try:
        return json.loads(res)
    except Exception:
        return None

def _kv_set(key: str, value: dict, ttl_seconds: int | None = SESSION_TTL) -> bool:
    # ttl_seconds=None -> SET with NO expiry. Used for open-trade records, which
    # must persist until explicitly closed/cancelled/abandoned (a later phase).
    # Sessions keep the default SESSION_TTL so they still self-clean.
    cmd = ["SET", key, json.dumps(value)]
    if ttl_seconds is not None:
        cmd += ["EX", str(int(ttl_seconds))]
    return _kv_cmd(cmd) == "OK"

def _sess_key(user_id, message_id) -> str:
    return f"sess:{user_id}:{message_id}"

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _new_session(user_id, message_id) -> dict:
    return {"user_id": user_id, "message_id": message_id, "selected": [],
            "created_at": _now_iso(), "updated_at": _now_iso()}

def _load_session(user_id, message_id):
    return _kv_get(_sess_key(user_id, message_id))


# ── Asset metadata (read-only from GitHub) + account size + leverage math ───

def _load_asset_cache() -> dict:
    """symbol -> {max_leverage, sz_decimals} from results/asset_cache.json.
    Returns {} on any failure (caller falls back to safe defaults)."""
    url = (f"https://api.github.com/repos/{GITHUB_REPO}"
           f"/contents/results/asset_cache.json?ref={GITHUB_REF}")
    headers = {"Accept": "application/vnd.github.raw+json",
               "User-Agent": "varam-dynamics-webhook"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        out = {}
        for a in data.get("assets", []):
            sym = a.get("symbol")
            if sym:
                out[sym] = {"max_leverage": int(a.get("max_leverage", 10)),
                            "sz_decimals": int(a.get("sz_decimals", 2))}
        return out
    except Exception as e:
        print(f"[webhook] asset_cache fetch failed: {e}")
        return {}

def _asset_meta(cache: dict, symbol: str) -> tuple[int, int]:
    m = cache.get(symbol) or {}
    return int(m.get("max_leverage", 10)), int(m.get("sz_decimals", 2))

def _account_size() -> float:
    try:
        return float(os.environ.get("ACCOUNT_SIZE") or "200")
    except Exception:
        return 200.0

def calc_leverage(entry: float, sl: float, account_size: float,
                  risk_pct: float, max_lev: int, sz_decimals: int) -> dict:
    """Pure leverage/size math (mirror scanner/assets.calc_leverage, with
    max_lev/sz_decimals supplied from asset_cache)."""
    if not entry or not sl or entry <= 0 or sl <= 0:
        return {"leverage": 1, "position_sz": round(account_size, 2), "qty": 0,
                "risk_usd": round(account_size * risk_pct, 2), "sl_pct": 0,
                "capped": False}
    sl_pct = abs(entry - sl) / entry or 0.01
    risk_usd = account_size * risk_pct
    raw = risk_usd / (account_size * sl_pct)
    final = min(round(raw, 1), max_lev)
    pos = account_size * final
    return {"leverage": final, "position_sz": round(pos, 2),
            "qty": round(pos / entry, sz_decimals),
            "risk_usd": round(risk_usd, 2), "sl_pct": round(sl_pct * 100, 2),
            "capped": raw > max_lev}


# ── Pure keyboard builders (mirror telegram/bot.py; no side-effects) ────────

def _kb(rows: list) -> dict:
    return {"inline_keyboard": rows}


def multiselect_kb(sigs: list, selected: set) -> dict:
    rows = []
    for s in sigs:
        tick = "✅" if s["symbol"] in selected else "☐"
        de   = "📈" if s["direction"] == "long" else "📉"
        rows.append([{"text": f"{tick} {s['symbol']} {de} [{s['score']:.1f}]",
                      "callback_data": f"TOGGLE_{s['symbol']}_{s['direction']}"}])
    n = len(selected)
    rows.append([{"text": (f"✅ Done — {n} trade{'s' if n != 1 else ''}" if n
                           else "⏭ Done (nothing selected)"),
                  "callback_data": "DONE"}])
    return _kb(rows)


def leverage_kb(sym: str, direction: str, suggested: float, max_lev: int) -> dict:
    """Preset leverage buttons (custom/back deferred in Phase 2C)."""
    opts = [l for l in LEV_OPTIONS if l <= max_lev]
    rows, row = [], []
    for l in opts:
        star = "⭐" if abs(l - suggested) < 0.6 else ""
        row.append({"text": f"{star}{l}x",
                    "callback_data": f"LEV_{sym}_{direction}_{l}"})
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return _kb(rows)


def size_kb(sym: str, direction: str, lev: float, acct: float) -> dict:
    """Preset size buttons (custom deferred in Phase 2C)."""
    rows, row = [], []
    for pct in SIZE_PCT:
        amt = round(acct * pct / 100)
        row.append({"text": f"${amt} ({pct}%)",
                    "callback_data": f"SZ_{sym}_{direction}_{lev}_{amt}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return _kb(rows)


def _preview_text(configured: list) -> str:
    """Preview summary of configured trades. Phase 2C END — logs nothing."""
    lines = ["🔍 <b>Preview — NOT logged</b>"]
    for c in configured:
        arrow = "📈" if c["direction"] == "long" else "📉"
        lines.append(
            f"{arrow} <b>{c['symbol']} {c['direction'].upper()}</b> — "
            f"Entry {c['entry']:.6g} · {c['leverage']:g}x · "
            f"${c['size_usd']:g} · risk ${c['risk_usd']:g}")
    lines.append("\n🔻 Review, then tap <b>Confirm &amp; log</b> to record these as open trades.")
    return "\n".join(lines)


def _confirm_kb() -> dict:
    return _kb([[{"text": "✅ Confirm & log", "callback_data": "CONFIRM"}]])


def _logged_summary(records: list) -> str:
    """Phase 4.2C: rich logged-trade summary (display only; built from records
    already written to Upstash). Keeps the user oriented after Confirm."""
    n = len(records)
    lines = [f"✅ Logged {n} open trade{'s' if n != 1 else ''}", ""]
    for i, r in enumerate(records, 1):
        prefix = f"{i}. " if n > 1 else ""
        lines.append(f"{prefix}<b>{r['symbol']} {str(r['direction']).upper()}</b>")
        lines.append(f"Entry: {float(r['entry_price']):.6g}")
        lines.append(f"Target: {float(r['tp_price']):.6g}")
        lines.append(f"Stop: {float(r['sl_price']):.6g}")
        lines.append(f"Leverage: {float(r['leverage_used']):g}x")
        lines.append(f"Size: ${float(r['size_usd']):g}")
        lines.append(f"Risk: ${float(r['risk_usd']):.2f}")
        lines.append("")
    lines.append("Send /mytrades to manage or close " + ("them." if n > 1 else "it."))
    return "\n".join(lines)


def _skip_text(batch: dict | None) -> str:
    """Phase 4.2C: skip message, listing the skipped calls when batch context
    is available. Logs nothing."""
    lines = ["⏭️ Skipped", "", "No trade was logged from this alert."]
    sigs = (batch or {}).get("sigs") or []
    if sigs:
        lines += ["", "Skipped:"]
        for s in sigs:
            lines.append(f"{s.get('symbol')} {str(s.get('direction', '')).upper()}")
    lines += ["", "You can wait for the next Signal alert."]
    return "\n".join(lines)


# ── Close flow helpers (Phase 2E-a; Upstash only, no TTL on trade keys) ──────

def _close_key(user_id) -> str:
    return f"close:{user_id}"

# ── Platform/source tag (D4B; display-only, backward-compatible) ─────────────
def _trade_source(trade_or_sig) -> str:
    """source of a trade record or batch sig; missing ⇒ 'hyperliquid' (old data)."""
    return (trade_or_sig or {}).get("source") or "hyperliquid"

def _platform_label(source: str) -> str:
    return {"hyperliquid": "Hyperliquid", "aster": "Aster"}.get(source or "hyperliquid",
                                                                "Hyperliquid")

def _mytrades_kb(opens: list) -> dict:
    return _kb([[{"text": f"Close {t.get('symbol')} {t.get('direction', '').upper()} "
                          f"· {_platform_label(_trade_source(t))}",
                  "callback_data": f"CLOSE:{i}"}] for i, t in enumerate(opens)])

def _px_kb() -> dict:
    return _kb([[{"text": "🎯 Target", "callback_data": "PX:target"},
                 {"text": "🛑 Stop",   "callback_data": "PX:stop"}],
                [{"text": "⚖️ Breakeven", "callback_data": "PX:be"},
                 {"text": "✏️ Manual",   "callback_data": "PX:manual"}]])

def _close_trade(user_id, trade_id, exit_price, reason):
    """Move an open trade -> closed with computed P&L. Returns the closed dict,
    'not_found', or None (KV failure). Both trade keys persist with NO TTL."""
    opens = _kv_get(f"trades:{user_id}") or []
    tr = next((t for t in opens
               if t.get("trade_id") == trade_id and t.get("status") == "open"), None)
    if not tr:
        return "not_found"
    entry = tr.get("entry_price")
    if not entry:
        return "not_found"
    size = tr.get("size_usd") or 0
    if tr.get("direction") == "long":
        pnl_pct = (exit_price - entry) / entry * 100
    else:
        pnl_pct = (entry - exit_price) / entry * 100
    eps = 0.05
    result = "win" if pnl_pct > eps else "loss" if pnl_pct < -eps else "breakeven"
    now = _now_iso()
    closed = {**tr, "exit_price": exit_price, "pnl_pct": round(pnl_pct, 3),
              "pnl_usd": round(size * pnl_pct / 100, 2), "result": result,
              "closed_at": now, "close_reason": reason, "status": "closed",
              "last_updated_at": now, "csv_synced": False}
    # 1) record the close first (dedupe) so a later failure can't lose the trade
    closed_list = _kv_get(f"closed_trades:{user_id}") or []
    if not any(c.get("trade_id") == trade_id for c in closed_list):
        closed_list = closed_list + [closed]
    if not _kv_set(f"closed_trades:{user_id}", closed_list, ttl_seconds=None):
        return None
    # 2) remove from the open list
    new_opens = [t for t in opens if t.get("trade_id") != trade_id]
    if not _kv_set(f"trades:{user_id}", new_opens, ttl_seconds=None):
        return None
    return closed

def _close_summary(c: dict, is_admin: bool = False) -> str:
    emoji = "✅" if c["result"] == "win" else "❌" if c["result"] == "loss" else "↔️"
    saved = ("📝 Saved to your closed trades. Run /sync_history to file permanent history."
             if is_admin else "📝 Saved to your closed trades.")
    return (f"{emoji} <b>{c['symbol']} {c['direction'].upper()} — closed</b> "
            f"· {_platform_label(_trade_source(c))}\n"
            f"Entry {float(c['entry_price']):.6g} → Exit {float(c['exit_price']):.6g}\n"
            f"Result: <b>{c['pnl_pct']:+.2f}%</b>  (${c['pnl_usd']:+.2f})  ·  {c['result']}\n"
            f"Method: {c.get('close_reason')}\n"
            f"{saved}")


# ── Invite-only access control (Phase 2F; Upstash only, no TTL) ─────────────
NOT_APPROVED = ("⏳ Not approved yet. Send /request_access to request access. "
                "Send /help to learn more.")

def _admin_id() -> int:
    try:
        return int(os.environ.get("ADMIN_CHAT_ID") or 0)
    except Exception:
        return 0

def _is_admin(user_id) -> bool:
    aid = _admin_id()
    return bool(aid) and user_id == aid

def _allowlist() -> dict:
    return _kv_get("users:allowlist") or {}

def _pending() -> dict:
    return _kv_get("users:pending") or {}

def _is_approved(user_id) -> bool:
    if _is_admin(user_id):          # admin is implicitly approved (never locked out)
        return True
    rec = _allowlist().get(str(user_id))
    return bool(rec) and rec.get("status") == "approved"

def _pending_list(pend: dict) -> list:
    # deterministic order so /pending listing and AP:/RJ:<index> resolve alike
    return [pend[k] for k in sorted(pend) if pend[k].get("status") == "pending"]


# ── Help text + button (Phase 4.1; display only, generic labels) ────────────

def _help_kb() -> dict:
    return _kb([[{"text": "❓ Help", "callback_data": "HELP"}]])

def _help_text(is_admin: bool = False) -> str:
    lines = [
        "ℹ️ <b>Varam-Dynamics — Help</b>", "",
        "<b>Commands</b>",
        "/start — your status &amp; quick start",
        "/whoami — show your account id",
        "/request_access — ask the admin for access",
        "/mytrades — view &amp; close your logged trades",
        "/help — this message",
    ]
    if is_admin:
        lines += [
            "/status — bot status summary (admin)",
            "/maintenance_start — announce maintenance start (admin)",
            "/maintenance_done — announce maintenance complete (admin)",
            "/sync_history — file closed trades to permanent history (admin)",
        ]
    lines += [
        "",
        "<b>How it works</b>",
        "Tap “✅ Select my trades” on a Signal alert to log trades you took, then "
        "choose leverage &amp; size. Confirm records an OPEN trade.",
        "",
        "<b>Managing trades</b>",
        "/mytrades lists open trades. Tap Close, pick an exit price, and the bot "
        "calculates your profit/loss and files a CLOSED trade.",
        "",
        "⚠️ Only tap Confirm / Close for trades you really took — they create real records.",
        "",
        "Risk dots: 🟢 low · 🟡 medium · 🟠 elevated · 🔴 high",
        "",
        "⚠️ Educational only — not financial advice. High-risk markets; your "
        "decisions are your own. DYOR.",
    ]
    return "\n".join(lines)


# ── Maintenance notices (Phase 4.1B; admin-only, advisory broadcast) ────────
MAINT_START_TEXT = ("🔧 Maintenance started\n\n"
                    "A bot update or verification is in progress.\n"
                    "Signals and tracking may be briefly delayed.\n"
                    "Avoid tapping trade buttons until the all-clear message.")
MAINT_DONE_TEXT  = ("✅ Maintenance complete\n\n"
                    "Bot checks are complete.\n"
                    "Buttons, commands, scanner, and monitor are operating normally.")

def _broadcast(text: str) -> tuple[int, int]:
    """Send `text` to all approved users + admin (deduped by id). Returns
    (sent, failed). Display/message only — no trade writes."""
    ids = {str(uid) for uid, rec in (_allowlist() or {}).items()
           if rec.get("status") == "approved"}
    aid = _admin_id()
    if aid:
        ids.add(str(aid))
    sent = failed = 0
    for uid in ids:
        r = _tg("sendMessage", {"chat_id": int(uid), "text": text})
        if r and r.get("ok"):
            sent += 1
        else:
            failed += 1
    return sent, failed


# ── GitHub CSV history writer (Phase 2E-b1; token-safe, stdlib only) ────────
CSV_PATH   = "results/manual_trades/trades.csv"
CSV_HEADER = ["timestamp", "user_id", "username", "trade_id", "symbol", "direction",
              "entry_price", "exit_price", "leverage_suggested", "leverage_used",
              "size_usd", "risk_usd", "sl_price", "tp_price", "result", "pnl_pct",
              "pnl_usd", "signal_score", "close_reason", "opened_at", "closed_at",
              "message_id", "batch_id", "csv_synced"]

def _gh_get(path: str):
    """Read a repo file via Contents API. Returns (status, text, sha) where
    status is ok|absent|no_token|error. NEVER prints the token."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return ("no_token", None, None)
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_REF}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "varam-dynamics-webhook"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        text = base64.b64decode(data.get("content", "")).decode()
        return ("ok", text, data.get("sha"))
    except urllib.error.HTTPError as e:
        return ("absent", None, None) if e.code == 404 else ("error", None, None)
    except Exception:
        return ("error", None, None)

def _gh_put(path: str, content_b64: str, message: str, sha: str | None = None) -> str:
    """Create/update a repo file via Contents API. Returns
    ok|no_token|conflict|error. NEVER prints the token."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return "no_token"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    payload = {"message": message, "content": content_b64, "branch": GITHUB_REF}
    if sha:
        payload["sha"] = sha
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="PUT",
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "varam-dynamics-webhook"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return "ok"
    except urllib.error.HTTPError as e:
        return "conflict" if e.code in (409, 422) else "error"
    except Exception:
        return "error"

def _trade_to_row(c: dict) -> dict:
    return {
        "timestamp": c.get("closed_at"), "user_id": c.get("user_id"),
        "username": c.get("username"), "trade_id": c.get("trade_id"),
        "symbol": c.get("symbol"), "direction": c.get("direction"),
        "entry_price": c.get("entry_price"), "exit_price": c.get("exit_price"),
        "leverage_suggested": c.get("leverage_suggested"),
        "leverage_used": c.get("leverage_used"), "size_usd": c.get("size_usd"),
        "risk_usd": c.get("risk_usd"), "sl_price": c.get("sl_price"),
        "tp_price": c.get("tp_price"), "result": c.get("result"),
        "pnl_pct": c.get("pnl_pct"), "pnl_usd": c.get("pnl_usd"),
        "signal_score": c.get("signal_score"), "close_reason": c.get("close_reason"),
        "opened_at": c.get("opened_at"), "closed_at": c.get("closed_at"),
        "message_id": c.get("message_id"), "batch_id": c.get("batch_id"),
        "csv_synced": True,
    }

def _mark_synced(user_id, ids: set) -> None:
    closed = _kv_get(f"closed_trades:{user_id}") or []
    changed = False
    for c in closed:
        if c.get("trade_id") in ids and not c.get("csv_synced"):
            c["csv_synced"] = True
            changed = True
    if changed:
        _kv_set(f"closed_trades:{user_id}", closed, ttl_seconds=None)

def _sync_history(user_id) -> dict:
    """Sync this user's unsynced closed trades to the repo CSV. Upstash records
    are marked csv_synced only after a successful PUT. Returns a status dict."""
    closed = _kv_get(f"closed_trades:{user_id}") or []
    unsynced = [c for c in closed if not c.get("csv_synced")]
    if not unsynced:
        return {"status": "noop", "synced": 0}
    for _ in range(3):                      # retry on 409/sha-mismatch
        st, text, sha = _gh_get(CSV_PATH)
        if st == "no_token":
            return {"status": "no_token"}
        if st == "error":
            return {"status": "error"}
        existing_ids = set()
        if st == "ok" and (text or "").strip():
            for r in csv.DictReader(io.StringIO(text)):
                if r.get("trade_id"):
                    existing_ids.add(r.get("trade_id"))
            create = False
        else:
            create = True
        to_write = [c for c in unsynced if c.get("trade_id") not in existing_ids]
        if not to_write:                    # all already in CSV -> just mark synced
            _mark_synced(user_id, existing_ids)
            return {"status": "ok", "synced": 0, "already": len(unsynced)}
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_HEADER, lineterminator="\n",
                           extrasaction="ignore")
        if create:
            w.writeheader()
        for c in to_write:
            w.writerow(_trade_to_row(c))
        if create:
            new_text = buf.getvalue()
        else:
            base = text if (text == "" or text.endswith("\n")) else text + "\n"
            new_text = base + buf.getvalue()
        content_b64 = base64.b64encode(new_text.encode()).decode()
        ps = _gh_put(CSV_PATH, content_b64,
                     f"sync {len(to_write)} manual trade(s)",
                     sha=(None if create else sha))
        if ps == "ok":
            _mark_synced(user_id, existing_ids | {c["trade_id"] for c in to_write})
            return {"status": "ok", "synced": len(to_write)}
        if ps == "no_token":
            return {"status": "no_token"}
        if ps == "conflict":
            continue                        # re-GET sha and retry
        return {"status": "error"}
    return {"status": "conflict"}


# ── Routing ──────────────────────────────────────────────────────────────────

def _route(update: dict) -> str:
    """Handle one Telegram update. Returns a short tag (for tests/logs).
    Phase 2A: stateless replies + read-only SELECT resolution."""
    # ---- text commands ----
    msg = update.get("message") or update.get("edited_message")
    if isinstance(msg, dict):
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        frm  = msg.get("from") or {}
        chat_id = chat.get("id")
        user_id = frm.get("id")
        if text.startswith("/whoami"):
            _tg("sendMessage", {
                "chat_id": chat_id,
                "text": ("👤 <b>whoami</b>\n"
                         f"chat_id: <code>{chat_id}</code>\n"
                         f"user_id: <code>{frm.get('id')}</code>\n"
                         f"username: @{frm.get('username')}"),
                "parse_mode": "HTML",
            })
            return "whoami"
        if text.startswith("/mytrades"):
            if not _is_approved(user_id):
                _tg("sendMessage", {"chat_id": chat_id, "text": NOT_APPROVED,
                                    "reply_markup": _help_kb()})
                return "blocked"
            opens = [t for t in (_kv_get(f"trades:{user_id}") or [])
                     if t.get("status") == "open"]
            if not opens:
                _tg("sendMessage", {"chat_id": chat_id,
                    "text": ("📂 You have no open trades.\n\n"
                             "When you log a trade from a Signal alert (Select → Confirm), "
                             "it appears here so you can close it later.")})
                return "mytrades_empty"
            lines = ["📂 <b>Your open trades</b>",
                     "Tap a trade to close it — you'll pick the exit price; "
                     "profit/loss is calculated automatically."]
            for i, t in enumerate(opens):
                arrow = "📈" if t.get("direction") == "long" else "📉"
                lines.append(f"{i + 1}. {arrow} {t.get('symbol')} "
                             f"{str(t.get('direction', '')).upper()} "
                             f"@ {float(t.get('entry_price') or 0):.6g} "
                             f"· {_platform_label(_trade_source(t))}")
            _tg("sendMessage", {"chat_id": chat_id, "text": "\n".join(lines),
                                "parse_mode": "HTML", "reply_markup": _mytrades_kb(opens)})
            return "mytrades"
        if text.startswith("/start"):
            if _is_approved(user_id):
                _tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML",
                    "reply_markup": _help_kb(),
                    "text": ("👋 <b>Welcome back!</b> You're approved ✅\n\n"
                             "You'll receive ranked Signal alerts. Tap “✅ Select my trades” "
                             "on an alert to log a trade, or send /mytrades to manage open "
                             "ones. /help anytime.\n\n"
                             "⚠️ Educational only — not financial advice. High-risk markets; "
                             "your decisions are your own.")})
                return "start_approved"
            _tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML",
                "reply_markup": _help_kb(),
                "text": ("👋 <b>Welcome to Varam-Dynamics.</b>\n\n"
                         "This is a private, invite-only signal bot. You're not approved "
                         "yet ⏳.\nSend /request_access to ask the admin for access.\n\n"
                         "⚠️ Educational only — not financial advice. High-risk markets.")})
            return "start_unapproved"
        if text.startswith("/help"):
            _tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML",
                                "text": _help_text(_is_admin(user_id))})
            return "help"
        if text.startswith("/request_access"):
            if _is_approved(user_id):
                _tg("sendMessage", {"chat_id": chat_id, "text": "✅ You're already approved."})
                return "already_approved"
            pend = _pending()
            if str(user_id) in pend and pend[str(user_id)].get("status") == "pending":
                _tg("sendMessage", {"chat_id": chat_id, "text": "⏳ Your request is already pending."})
                return "already_pending"
            dn = " ".join(x for x in [frm.get("first_name"), frm.get("last_name")] if x)
            pend[str(user_id)] = {"user_id": user_id, "username": frm.get("username"),
                                  "display_name": dn, "status": "pending",
                                  "requested_at": _now_iso()}
            if not _kv_set("users:pending", pend, ttl_seconds=None):
                _tg("sendMessage", {"chat_id": chat_id,
                                    "text": "⚠️ Temporarily unavailable, please try again."})
                return "kv_unavailable"
            _tg("sendMessage", {"chat_id": chat_id,
                                "text": "✅ Request received — an admin will review it."})
            return "request_recorded"
        if text.startswith("/pending"):
            if not _is_admin(user_id):
                _tg("sendMessage", {"chat_id": chat_id, "text": "⛔ Not authorised."})
                return "denied"
            items = _pending_list(_pending())
            if not items:
                _tg("sendMessage", {"chat_id": chat_id, "text": "No pending requests."})
                return "pending_empty"
            lines, rows = ["🕓 <b>Pending access requests</b>:"], []
            for i, r in enumerate(items):
                lines.append(f"{i + 1}. id <code>{r.get('user_id')}</code> "
                             f"@{r.get('username')} {r.get('display_name') or ''}".rstrip())
                rows.append([{"text": f"✅ Approve {i + 1}", "callback_data": f"AP:{i}"},
                             {"text": f"🚫 Reject {i + 1}",  "callback_data": f"RJ:{i}"}])
            _tg("sendMessage", {"chat_id": chat_id, "text": "\n".join(lines),
                                "parse_mode": "HTML", "reply_markup": _kb(rows)})
            return "pending"
        if text.startswith("/sync_history"):
            if not _is_admin(user_id):
                _tg("sendMessage", {"chat_id": chat_id, "text": "⛔ Not authorised."})
                return "denied"
            res = _sync_history(user_id)
            st = res.get("status")
            if st == "no_token":
                _tg("sendMessage", {"chat_id": chat_id,
                                    "text": "⚠️ GitHub token missing; sync not run."})
                return "sync_no_token"
            if st == "noop":
                _tg("sendMessage", {"chat_id": chat_id, "text": "No unsynced closed trades."})
                return "sync_noop"
            if st == "ok":
                _tg("sendMessage", {"chat_id": chat_id,
                                    "text": f"✅ Synced {res.get('synced', 0)} trade(s) to history."})
                return "sync_ok"
            if st == "conflict":
                _tg("sendMessage", {"chat_id": chat_id,
                                    "text": "⚠️ History file is busy — please retry."})
                return "sync_conflict"
            _tg("sendMessage", {"chat_id": chat_id,
                                "text": "⚠️ Sync failed; trades remain pending."})
            return "sync_error"
        if text.startswith("/maintenance_start"):
            if not _is_admin(user_id):
                _tg("sendMessage", {"chat_id": chat_id, "text": "⛔ Not authorised."})
                return "denied"
            sent, failed = _broadcast(MAINT_START_TEXT)
            note = f"📣 Notice sent to {sent} approved user(s)."
            if failed:
                note += f" {failed} failed."
            _tg("sendMessage", {"chat_id": chat_id, "text": note})
            return "maintenance_start"
        if text.startswith("/maintenance_done"):
            if not _is_admin(user_id):
                _tg("sendMessage", {"chat_id": chat_id, "text": "⛔ Not authorised."})
                return "denied"
            sent, failed = _broadcast(MAINT_DONE_TEXT)
            note = f"📣 Notice sent to {sent} approved user(s)."
            if failed:
                note += f" {failed} failed."
            _tg("sendMessage", {"chat_id": chat_id, "text": note})
            return "maintenance_done"
        if text.startswith("/status"):
            if not _is_admin(user_id):
                _tg("sendMessage", {"chat_id": chat_id, "text": "⛔ Not authorised."})
                return "denied"
            csv_line = ("ready (token present)" if os.environ.get("GITHUB_TOKEN")
                        else "⚠️ token missing")
            open_n = len([t for t in (_kv_get(f"trades:{user_id}") or [])
                          if t.get("status") == "open"])
            _tg("sendMessage", {"chat_id": chat_id, "text": (
                "📊 Admin status\n"
                "• Webhook: live ✅\n"
                "• Callback handling: this Vercel webhook endpoint\n"
                "• Manual trade logging: enabled\n"
                f"• CSV history sync: {csv_line}\n"
                f"• Your open trades: {open_n}\n\n"
                "Reminder: Confirm / Close create real records.")})
            return "status"
        # ---- manual exit price (only while a close session awaits it) ----
        cs = _kv_get(_close_key(user_id))
        if cs and cs.get("awaiting_price") and cs.get("trade_id"):
            if not _is_approved(user_id):
                _tg("sendMessage", {"chat_id": chat_id, "text": NOT_APPROVED,
                                    "reply_markup": _help_kb()})
                return "blocked"
            try:
                exit_px = float(text.replace(",", "").strip())
            except ValueError:
                _tg("sendMessage", {"chat_id": chat_id,
                                    "text": "⚠️ Please send a valid number for the exit price."})
                return "manual_invalid"
            if exit_px <= 0:
                _tg("sendMessage", {"chat_id": chat_id,
                                    "text": "⚠️ Exit price must be a positive number."})
                return "manual_invalid"
            res = _close_trade(user_id, cs["trade_id"], exit_px, "manual")
            if res is None:
                _tg("sendMessage", {"chat_id": chat_id,
                                    "text": "⚠️ Temporarily unavailable, please try again."})
                return "kv_unavailable"
            if res == "not_found":
                _tg("sendMessage", {"chat_id": chat_id,
                                    "text": "⚠️ Already closed or not found."})
                return "close_not_found"
            _kv_set(_close_key(user_id), {"trade_id": None, "awaiting_price": False}, SESSION_TTL)
            _tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML",
                                "text": _close_summary(res, _is_admin(user_id))})
            return "manual_closed"
        return "ignored_message"

    # ---- button taps ----
    cb = update.get("callback_query")
    if isinstance(cb, dict):
        cb_id = cb.get("id")
        data  = cb.get("data", "")
        m     = cb.get("message") or {}
        mid   = m.get("message_id")
        chat_id = (m.get("chat") or {}).get("id")
        user_id = (cb.get("from") or {}).get("id")
        # Clear the spinner instantly.
        _tg("answerCallbackQuery", {"callback_query_id": cb_id})

        def _edit(text, markup=None):
            p = {"chat_id": chat_id, "message_id": mid,
                 "text": text, "parse_mode": "HTML"}
            if markup is not None:
                p["reply_markup"] = markup
            _tg("editMessageText", p)

        def _batch_for_mid():
            return (_load_remote_state().get("batches") or {}).get(str(mid))

        def _show_leverage(current):
            acct = _account_size()
            max_lev, dec = _asset_meta(_load_asset_cache(), current["symbol"])
            sug = calc_leverage(current.get("entry"), current.get("sl"),
                                acct, RISK_PCT, max_lev, dec)["leverage"]
            _edit(f"⚙️ <b>{current['symbol']} {current['direction'].upper()}</b>\n"
                  f"Entry {current['entry']:.6g} · SL {current['sl']:.6g} · "
                  f"TP {current['tp']:.6g}\n⭐ Suggested: {sug:g}x\nChoose leverage:",
                  leverage_kb(current["symbol"], current["direction"], sug, max_lev))

        # ---- Access gate: trading callbacks require an approved user ----
        _TRADE_CB     = {"SELECT", "SKIP", "DONE", "CONFIRM"}
        _TRADE_PREFIX = ("TOGGLE_", "LEV_", "SZ_", "CLOSE:", "PX:")
        if (data in _TRADE_CB or any(data.startswith(p) for p in _TRADE_PREFIX)) \
                and not _is_approved(user_id):
            _edit(NOT_APPROVED, _help_kb())
            return "blocked"

        # ---- SKIP: dismiss the alert with context (logs nothing) ----
        if data == "SKIP":
            _edit(_skip_text(_batch_for_mid()))
            return "skipped"

        # ---- SELECT: show multi-select + start an empty session ----
        if data == "SELECT":
            batch = _batch_for_mid()
            if not batch or not batch.get("sigs"):
                _edit("⚠️ This alert is old. Please use the latest signal message.")
                return "select_old"
            if not _kv_set(_sess_key(user_id, mid), _new_session(user_id, mid)):
                _edit("⚠️ Selection temporarily unavailable, please try again.")
                return "kv_unavailable"
            _edit("Which trades did you take?\nTap to select, then Done ✅",
                  multiselect_kb(batch["sigs"], set()))
            return "select_shown"

        # ---- TOGGLE: add/remove an asset (cap 5) ----
        if data.startswith("TOGGLE_"):
            _, sym, direction = data.split("_", 2)
            batch = _batch_for_mid()
            if not batch or not batch.get("sigs"):
                _edit("⚠️ This alert is old. Please use the latest signal message.")
                return "select_old"
            sess = _load_session(user_id, mid) or _new_session(user_id, mid)
            sel  = sess.get("selected", [])
            syms = [it["symbol"] for it in sel]
            if sym in syms:
                sel = [it for it in sel if it["symbol"] != sym]          # remove
            elif len(sel) < MAX_SELECT:
                sel.append({"symbol": sym, "direction": direction})      # add
            # else: at cap -> ignore the add
            sess["selected"]   = sel
            sess["updated_at"] = _now_iso()
            if not _kv_set(_sess_key(user_id, mid), sess):
                _edit("⚠️ Selection temporarily unavailable, please try again.")
                return "kv_unavailable"
            _edit("Which trades did you take?\nTap to select, then Done ✅",
                  multiselect_kb(batch["sigs"], set(it["symbol"] for it in sel)))
            return "toggle"

        # ---- DONE: build queue from picks, start leverage selection ----
        if data == "DONE":
            sess = _load_session(user_id, mid)
            sel  = (sess or {}).get("selected", [])
            if not sel:
                _edit("⏭ Nothing selected.")
                return "done_empty"
            batch = _batch_for_mid()
            if not batch or not batch.get("sigs"):
                _edit("⚠️ This alert is old. Please use the latest signal message.")
                return "select_old"
            by_sym = {s["symbol"]: s for s in batch["sigs"]}
            queue = []
            for it in sel:
                s = by_sym.get(it["symbol"])
                if not s:
                    continue
                queue.append({"symbol": it["symbol"], "direction": it["direction"],
                              "entry": s.get("entry"), "sl": s.get("sl"), "tp": s.get("tp"),
                              "score": s.get("score")})
            if not queue:
                _edit("⏭ Nothing selected.")
                return "done_empty"
            sess["current"]    = queue[0]
            sess["queue"]      = queue[1:]
            sess["configured"] = []
            sess["updated_at"] = _now_iso()
            if not _kv_set(_sess_key(user_id, mid), sess):
                _edit("⚠️ Selection temporarily unavailable, please try again.")
                return "kv_unavailable"
            _show_leverage(sess["current"])
            return "lev_prompt"

        # ---- LEV: store leverage for current asset, show size ----
        if data.startswith("LEV_"):
            parts = data.split("_", 3)
            if len(parts) < 4:
                return "ignored"
            _, sym, direction, n = parts
            if n == "custom":
                _edit("✏️ Custom leverage coming soon — please pick a preset value.")
                return "coming_soon"
            sess = _load_session(user_id, mid)
            cur  = (sess or {}).get("current")
            if not sess or not cur:
                _edit("⚠️ Session expired — tap Select on the latest alert again.")
                return "session_expired"
            try:
                lev = float(n)
            except ValueError:
                return "ignored"
            cur["leverage"]    = lev
            sess["current"]    = cur
            sess["updated_at"] = _now_iso()
            if not _kv_set(_sess_key(user_id, mid), sess):
                _edit("⚠️ Selection temporarily unavailable, please try again.")
                return "kv_unavailable"
            _edit(f"⚙️ <b>{sym} {direction.upper()}</b> — {lev:g}x\nChoose position size:",
                  size_kb(sym, direction, lev, _account_size()))
            return "size_prompt"

        # ---- SZ: store size, advance queue, then PREVIEW (no logging) ----
        if data.startswith("SZ_"):
            parts = data.split("_", 4)
            if len(parts) < 5:
                return "ignored"
            _, sym, direction, lv, amt = parts
            if amt == "custom":
                _edit("✏️ Custom size coming soon — please pick a preset value.")
                return "coming_soon"
            sess = _load_session(user_id, mid)
            cur  = (sess or {}).get("current")
            if not sess or not cur:
                _edit("⚠️ Session expired — tap Select on the latest alert again.")
                return "session_expired"
            try:
                lev, size = float(lv), float(amt)
            except ValueError:
                return "ignored"
            entry, sl = cur.get("entry"), cur.get("sl")
            risk_usd = round(size * abs(entry - sl) / entry, 2) if entry else 0.0
            configured = sess.get("configured", [])
            configured.append({"symbol": sym, "direction": direction,
                               "entry": entry, "sl": sl, "tp": cur.get("tp"),
                               "leverage": lev, "size_usd": size, "risk_usd": risk_usd,
                               "score": cur.get("score")})
            sess["configured"] = configured
            q = sess.get("queue", [])
            if q:
                sess["current"]    = q[0]
                sess["queue"]      = q[1:]
                sess["updated_at"] = _now_iso()
                if not _kv_set(_sess_key(user_id, mid), sess):
                    _edit("⚠️ Selection temporarily unavailable, please try again.")
                    return "kv_unavailable"
                _show_leverage(sess["current"])
                return "lev_prompt"
            # queue empty -> final preview (logs nothing)
            sess["current"]    = None
            sess["updated_at"] = _now_iso()
            _kv_set(_sess_key(user_id, mid), sess)   # best-effort
            _edit(_preview_text(configured), _confirm_kb())
            return "preview"

        # ---- CONFIRM: record OPEN trades to Upstash (idempotent) ----
        if data == "CONFIRM":
            sess = _load_session(user_id, mid)
            if not sess:
                _edit("⚠️ Session expired — tap Select on the latest alert again.")
                return "session_expired"
            if sess.get("logged"):
                _edit("ℹ️ Already logged — these trades were recorded.")
                return "already_logged"
            configured = sess.get("configured", [])
            if not configured:
                _edit("Nothing to log.")
                return "nothing_to_log"
            acct  = _account_size()
            cache = _load_asset_cache()
            now   = _now_iso()
            uname = (cb.get("from") or {}).get("username")   # display only, NOT identity
            records = []
            for c in configured:
                max_lev, dec = _asset_meta(cache, c["symbol"])
                sug = calc_leverage(c.get("entry"), c.get("sl"),
                                    acct, RISK_PCT, max_lev, dec)["leverage"]
                records.append({
                    "trade_id":           f"{user_id}:{mid}:{c['symbol']}",
                    "user_id":            user_id,          # identity = numeric id
                    "username":           uname,            # display only
                    "source":             _trade_source(c),  # D4B: 'hyperliquid' here
                    "symbol":             c["symbol"],
                    "direction":          c["direction"],
                    "entry_price":        c.get("entry"),
                    "sl_price":           c.get("sl"),
                    "tp_price":           c.get("tp"),
                    "leverage_used":      c.get("leverage"),
                    "leverage_suggested": sug,
                    "size_usd":           c.get("size_usd"),
                    "risk_usd":           c.get("risk_usd"),
                    "signal_score":       c.get("score"),
                    "status":             "open",
                    "opened_at":          now,
                    "last_updated_at":    now,
                    "last_reminded_at":   None,
                    "stale_after_days":   7,
                    "message_id":         mid,
                    "batch_id":           str(mid),
                })
            # Append to trades:<user_id>, de-duplicating by trade_id (defensive).
            # NO TTL: open trades must persist until explicitly closed later.
            key = f"trades:{user_id}"
            existing = _kv_get(key) or []
            have = {t.get("trade_id") for t in existing}
            to_add = [r for r in records if r["trade_id"] not in have]
            if not _kv_set(key, existing + to_add, ttl_seconds=None):
                _edit("⚠️ Temporarily unavailable, please try again.")
                return "kv_unavailable"
            sess["logged"] = True
            sess["updated_at"] = _now_iso()
            _kv_set(_sess_key(user_id, mid), sess)   # best-effort; dedupe still guards
            _edit(_logged_summary(records))
            return "logged"

        # ---- CLOSE:<index> -> pick an open trade, show exit-price options ----
        if data.startswith("CLOSE:"):
            try:
                idx = int(data.split(":", 1)[1])
            except ValueError:
                return "ignored"
            opens = [t for t in (_kv_get(f"trades:{user_id}") or [])
                     if t.get("status") == "open"]
            if idx < 0 or idx >= len(opens):
                _edit("⚠️ Already closed or not found.")
                return "close_not_found"
            tr = opens[idx]
            if not _kv_set(_close_key(user_id),
                           {"trade_id": tr["trade_id"], "awaiting_price": False}, SESSION_TTL):
                _edit("⚠️ Temporarily unavailable, please try again.")
                return "kv_unavailable"
            _edit(f"Close <b>{tr['symbol']} {tr['direction'].upper()}</b> "
                  f"· {_platform_label(_trade_source(tr))} — choose exit price:",
                  _px_kb())
            return "close_selected"

        # ---- PX:<kind> -> resolve exit price and close (or await manual) ----
        if data.startswith("PX:"):
            kind = data.split(":", 1)[1]
            cs = _kv_get(_close_key(user_id))
            if not cs or not cs.get("trade_id"):
                _edit("⚠️ Session expired — send /mytrades to start again.")
                return "session_expired"
            if kind == "manual":
                cs["awaiting_price"] = True
                _kv_set(_close_key(user_id), cs, SESSION_TTL)
                _edit("✏️ Send the exit price as a number (e.g. 1650.5).")
                return "await_manual"
            opens = [t for t in (_kv_get(f"trades:{user_id}") or [])
                     if t.get("status") == "open"]
            tr = next((t for t in opens if t.get("trade_id") == cs["trade_id"]), None)
            if not tr:
                _edit("⚠️ Already closed or not found.")
                return "close_not_found"
            px = {"target": tr.get("tp_price"), "stop": tr.get("sl_price"),
                  "be": tr.get("entry_price")}.get(kind)
            reason = {"target": "target", "stop": "stop", "be": "breakeven"}.get(kind)
            if px is None or reason is None:
                _edit("⚠️ Price unavailable for this option.")
                return "px_unavailable"
            res = _close_trade(user_id, cs["trade_id"], float(px), reason)
            if res is None:
                _edit("⚠️ Temporarily unavailable, please try again.")
                return "kv_unavailable"
            if res == "not_found":
                _edit("⚠️ Already closed or not found.")
                return "close_not_found"
            _kv_set(_close_key(user_id), {"trade_id": None, "awaiting_price": False}, SESSION_TTL)
            _edit(_close_summary(res, _is_admin(user_id)))
            return "closed"

        # ---- AP:/RJ:<index> — admin approve / reject (admin only) ----
        if data.startswith("AP:") or data.startswith("RJ:"):
            if not _is_admin(user_id):
                _edit("⛔ Not authorised.")
                return "denied"
            try:
                idx = int(data.split(":", 1)[1])
            except ValueError:
                return "ignored"
            pend = _pending()
            items = _pending_list(pend)
            if idx < 0 or idx >= len(items):
                _edit("⚠️ Request not found (already handled?).")
                return "pending_not_found"
            rec   = items[idx]
            uid_s = str(rec.get("user_id"))
            now   = _now_iso()
            allow = _allowlist()
            if data.startswith("AP:"):
                allow[uid_s] = {**rec, "status": "approved",
                                "approved_at": now, "approved_by": user_id}
                verdict, tag = "✅ Approved", "approved"
            else:
                allow[uid_s] = {**rec, "status": "rejected",
                                "rejected_at": now, "rejected_by": user_id}
                verdict, tag = "🚫 Rejected", "rejected"
            if not _kv_set("users:allowlist", allow, ttl_seconds=None):
                _edit("⚠️ Temporarily unavailable, please try again.")
                return "kv_unavailable"
            pend.pop(uid_s, None)
            _kv_set("users:pending", pend, ttl_seconds=None)
            _edit(f"{verdict} user <code>{uid_s}</code>.")
            return tag

        # ---- Help button -> real help text (admin-aware) ----
        if data == "HELP" and chat_id is not None:
            _tg("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML",
                                "text": _help_text(_is_admin(user_id))})
            return "callback_help"
        if data in ("TEST", "PING") and chat_id is not None:
            _tg("sendMessage", {"chat_id": chat_id, "text": "✅ ok"})
            return "callback_ack"
        return "callback_ack"

    return "ignored"


def process_request(body_bytes: bytes, secret_header: str | None) -> tuple[int, dict]:
    """Verify secret, parse, route. Returns (http_status, json_body)."""
    if not _verify_secret(secret_header):
        return 401, {"ok": False, "error": "unauthorized"}
    try:
        update = json.loads(body_bytes.decode() or "{}")
    except Exception:
        return 400, {"ok": False, "error": "bad json"}
    if not isinstance(update, dict):
        return 400, {"ok": False, "error": "bad update"}
    return 200, {"ok": True, "action": _route(update)}


# ── Vercel Python handler (BaseHTTPRequestHandler) ──────────────────────────

class handler(BaseHTTPRequestHandler):
    def _send(self, status: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # health check — no secrets exposed
        self._send(200, {"ok": True, "service": "varam-dynamics-webhook", "phase": "2E-b1"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        status, resp = process_request(body, secret)
        self._send(status, resp)

    def log_message(self, *args):  # silence default request logging
        return
