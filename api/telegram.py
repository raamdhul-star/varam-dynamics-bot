"""
api/telegram.py — Varam-Dynamics Telegram webhook (Phase 2A, Vercel Python BETA)
==============================================================================
SCOPE (Phase 1 + Phase 2A only):
  • verify the Telegram secret header (X-Telegram-Bot-Api-Secret-Token)
  • /whoami  → reply caller's chat/user id
  • Help/TEST/PING callback → instant acknowledgement
  • SELECT callback → read state (read-only from GitHub), resolve the tapped
    alert's batch snapshot, and show the multi-select screen (empty selection)

NOT YET (Phase 2B+): TOGGLE / DONE / leverage / size / confirm / close /
invite-only / trade logging / any state WRITE. This endpoint performs NO writes
to the repo and keeps NO server-side session state in Phase 2A.

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
        # Clear the spinner instantly.
        _tg("answerCallbackQuery", {"callback_query_id": cb_id})

        # ---- Phase 2A: SELECT -> show multi-select (read-only) ----
        if data == "SELECT":
            state = _load_remote_state()
            batch = (state.get("batches") or {}).get(str(mid))
            if not batch or not batch.get("sigs"):
                _tg("editMessageText", {
                    "chat_id": chat_id, "message_id": mid,
                    "text": "⚠️ This alert is old. Please use the latest signal message.",
                    "parse_mode": "HTML",
                })
                return "select_old"
            _tg("editMessageText", {
                "chat_id": chat_id, "message_id": mid,
                "text": "Which trades did you take?\nTap to select, then Done ✅",
                "parse_mode": "HTML",
                "reply_markup": multiselect_kb(batch["sigs"], set()),
            })
            return "select_shown"

        # ---- Phase 1: Help / test acknowledgement ----
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
        self._send(200, {"ok": True, "service": "varam-dynamics-webhook", "phase": "2A"})

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
