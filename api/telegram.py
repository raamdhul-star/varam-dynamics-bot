"""
api/telegram.py — Varam-Dynamics Telegram webhook (Phase 2B, Vercel Python BETA)
==============================================================================
SCOPE (Phase 1 + 2A + 2B):
  • verify the Telegram secret header (X-Telegram-Bot-Api-Secret-Token)
  • /whoami  → reply caller's chat/user id
  • Help/TEST/PING callback → instant acknowledgement
  • SELECT callback → read state (read-only from GitHub), show the multi-select
    screen, and start an empty per-user session in Upstash Redis
  • TOGGLE_<sym>_<dir> → add/remove an asset (cap 5), persist, re-render ticks
  • DONE → summarise the selection (END of Phase 2B)

Server-side session state lives in Upstash Redis (per user+message), NOT in the
repo. NOT YET (Phase 2C+): leverage / size / confirm / close / invite-only /
trade logging. This endpoint NEVER writes to the repo / results/.

Self-contained: standard library only; does NOT import telegram/bot.py (whose
import-time mkdir() would be fragile on Vercel's read-only filesystem). The two
tiny pure keyboard builders below mirror telegram/bot.py exactly.

Secrets are read from environment variables and NEVER printed.
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import json
import os
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
SESSION_TTL = 1800     # 30 minutes
MAX_SELECT  = 5

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

def _kv_set(key: str, value: dict, ttl_seconds: int = SESSION_TTL) -> bool:
    return _kv_cmd(["SET", key, json.dumps(value), "EX", str(int(ttl_seconds))]) == "OK"

def _sess_key(user_id, message_id) -> str:
    return f"sess:{user_id}:{message_id}"

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _new_session(user_id, message_id) -> dict:
    return {"user_id": user_id, "message_id": message_id, "selected": [],
            "created_at": _now_iso(), "updated_at": _now_iso()}

def _load_session(user_id, message_id):
    return _kv_get(_sess_key(user_id, message_id))


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
        if text.startswith("/help") or text.startswith("/start"):
            _tg("sendMessage", {
                "chat_id": chat_id,
                "text": ("🧪 Varam-Dynamics webhook is online.\n"
                         "Commands: /whoami\n"
                         "This is a system endpoint — not financial advice."),
            })
            return "help"
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

        # ---- DONE: summarise selection (Phase 2B endpoint) ----
        if data == "DONE":
            sess = _load_session(user_id, mid)
            sel  = (sess or {}).get("selected", [])
            if not sel:
                _edit("⏭ Nothing selected.")
                return "done_empty"
            lines = ["✅ <b>Selected trades</b>:"]
            for it in sel:
                arrow = "📈" if it["direction"] == "long" else "📉"
                lines.append(f"{arrow} {it['symbol']} {it['direction'].upper()}")
            lines.append("\n(Next steps coming soon.)")
            _edit("\n".join(lines))
            return "done"

        # ---- Help / test acknowledgement ----
        if data in ("HELP", "TEST", "PING") and chat_id is not None:
            _tg("sendMessage", {
                "chat_id": chat_id,
                "text": "🧪 Webhook test OK — your button tap was handled instantly.",
            })
            return "callback_help"
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
        self._send(200, {"ok": True, "service": "varam-dynamics-webhook", "phase": "2B"})

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
